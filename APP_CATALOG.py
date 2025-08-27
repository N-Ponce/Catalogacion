# -*- coding: utf-8 -*-
"""
Validador de Catalogación (simple.ripley.cl) con soporte de COOKIES
-------------------------------------------------------------------
- Carga cookies desde:
    1) Variable de entorno COOKIE_HEADER (formato: "k1=v1; k2=v2; ...")
    2) Variable de entorno COOKIES_JSON (formato JSON: {"k1":"v1","k2":"v2"})
    3) Campo de texto en la UI (no se guarda; útil para pruebas)
- Realiza búsqueda PLP -> encuentra primer PDP -> extrae taxonomía (breadcrumb)
  desde JSON-LD, microdatos, dataLayer o DOM.
- Regla: ≥2 niveles útiles y sin "miscel/otros/varios" = "Sí, catalogado".
- Si el breadcrumb trae solo "Home/Inicio", marca "No catalogado" con observación.

IMPORTANTE:
- No subas tus cookies al repo. Usa .env (local/colab) o pega el header en la UI.
"""

import os
import re
import io
import csv
import json
import time
import random
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
import streamlit as st

# ===== Configuración dominio =====
DOMAIN = "https://simple.ripley.cl"
SEARCH_PATH = "/busca?Ntt={q}"
TIMEOUT = 25

# ===== Headers base =====
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
}

MISC_PAT = re.compile(r"(otros|miscel|varios|variedad|otros productos)", re.IGNORECASE)
HOME_NOISE = {"home", "inicio", "búsqueda", "busqueda", "resultados", "search", "results"}

# ===== Helpers COOKIES =====
def load_dotenv_if_available() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        pass  # opcional

def parse_cookie_header(cookie_header: str) -> Dict[str, str]:
    """
    Convierte: 'k1=v1; k2=v2; k3=v3' -> {"k1":"v1","k2":"v2","k3":"v3"}
    Ignora espacios/pares vacíos.
    """
    out = {}
    for part in cookie_header.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                out[k] = v
    return out

def get_cookies_from_env() -> Dict[str, str]:
    """
    Carga cookies desde:
      - COOKIE_HEADER (string tipo header)
      - COOKIES_JSON (JSON dict)
    Se pueden usar ambas; COOKIES_JSON sobreescribe claves de COOKIE_HEADER.
    """
    cookies: Dict[str, str] = {}
    header = os.getenv("COOKIE_HEADER", "").strip()
    if header:
        cookies.update(parse_cookie_header(header))
    js = os.getenv("COOKIES_JSON", "").strip()
    if js:
        try:
            obj = json.loads(js)
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(k, str) and isinstance(v, str):
                        cookies[k] = v
        except Exception:
            pass
    return cookies

# ===== Utilidades =====
def candidate_skus(s: str) -> List[str]:
    s = s.strip()
    cands = [s]
    if "-" in s:
        base = s.split("-")[0].strip()
        if base and base not in cands:
            cands.append(base)
    return cands

def new_session(cookies: Optional[Dict[str, str]]) -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    if cookies:
        s.cookies.update(cookies)
    return s

def session_get(url: str, session: requests.Session) -> Optional[requests.Response]:
    try:
        r = session.get(url, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r
    except requests.RequestException:
        return None
    return None

# ===== Parsers JSON-LD/Microdata/DOM/DataLayer =====
def _iter_jsonld_blocks(soup: BeautifulSoup):
    for s in soup.find_all("script", type="application/ld+json"):
        txt = s.string or ""
        if not txt.strip():
            continue
        try:
            data = json.loads(txt)
            yield data
            continue
        except Exception:
            pass
        # varias líneas con JSON sueltos
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue

def extract_breadcrumb_from_jsonld(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    for data in _iter_jsonld_blocks(soup):
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") == "BreadcrumbList" and isinstance(obj.get("itemListElement"), list):
                names = []
                for it in obj["itemListElement"]:
                    name = None
                    if isinstance(it, dict):
                        if isinstance(it.get("item"), dict):
                            name = it["item"].get("name")
                        if not name:
                            name = it.get("name")
                    if isinstance(name, str):
                        nm = name.strip()
                        if nm and nm.lower() not in {"home", "inicio"}:
                            names.append(nm)
                if names:
                    return names
            if "@graph" in obj and isinstance(obj["@graph"], list):
                for g in obj["@graph"]:
                    if isinstance(g, dict) and g.get("@type") == "BreadcrumbList":
                        names = []
                        for it in g.get("itemListElement", []):
                            name = (isinstance(it.get("item"), dict) and it["item"].get("name")) or it.get("name")
                            if isinstance(name, str):
                                nm = name.strip()
                                if nm and nm.lower() not in {"home", "inicio"}:
                                    names.append(nm)
                        if names:
                            return names
    return []

def extract_product_category_from_jsonld(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for data in _iter_jsonld_blocks(soup):
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            if obj.get("@type") in ("Product", "IndividualProduct"):
                cat = obj.get("category") or (obj.get("brand") or {}).get("category")
                if isinstance(cat, str) and cat.strip():
                    return cat.strip()
            if "@graph" in obj and isinstance(obj["@graph"], list):
                for g in obj["@graph"]:
                    if isinstance(g, dict) and g.get("@type") in ("Product", "IndividualProduct"):
                        cat = g.get("category")
                        if isinstance(cat, str) and cat.strip():
                            return cat.strip()
    return None

def extract_breadcrumb_from_microdata(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    items = soup.select('[itemtype*="BreadcrumbList"] [itemprop="itemListElement"] [itemprop="name"]')
    names = [x.get_text(strip=True) for x in items if x.get_text(strip=True)]
    return [n for n in names if n.lower() not in {"home", "inicio"}]

def extract_category_from_datalayer(html: str) -> Optional[str]:
    m = re.search(r"dataLayer\s*=\s*(\[[\s\S]*?\])", html)
    if not m:
        m = re.search(r"vtex[\s\S]{0,50}=\s*(\{[\s\S]*?\});", html)
    if m:
        txt = m.group(1)
        try:
            data = json.loads(txt)
            if isinstance(data, list):
                for ev in data:
                    if isinstance(ev, dict):
                        cat = ev.get("category") or ev.get("department") or ev.get("pageCategory")
                        if isinstance(cat, str) and cat.strip():
                            return cat.strip()
            if isinstance(data, dict):
                cat = data.get("category") or data.get("department") or data.get("pageCategory")
                if isinstance(cat, str) and cat.strip():
                    return cat.strip()
        except Exception:
            pass
    return None

def extract_breadcrumb_from_dom(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    selectors = [
        'nav[aria-label="breadcrumb"]',
        "ol.breadcrumb, ul.breadcrumb, div.breadcrumb, div.breadcrumbs, li.breadcrumbs, nav.breadcrumb, nav.breadcrumbs",
    ]
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            parts = []
            for tag in node.find_all(["a", "span", "li"]):
                t = tag.get_text(" ", strip=True)
                if t and t not in {">", "/", "|", "›", "»", "•"} and t.lower() not in {"home", "inicio"}:
                    parts.append(t)
            out = []
            for p in parts:
                if not out or out[-1] != p:
                    out.append(p)
            if out:
                return out
    return []

def extract_any_taxonomy(html: str) -> Tuple[List[str], str]:
    crumbs = extract_breadcrumb_from_jsonld(html)
    if crumbs:
        return crumbs, "jsonld_breadcrumb"
    cat = extract_product_category_from_jsonld(html)
    if cat:
        parts = re.split(r"\s*[>/\|›»]+\s*|\s*>\s*|\s*/\s*|\s*-\s*", cat)
        parts = [p.strip() for p in parts if p.strip()]
        return parts if parts else [cat], "jsonld_product"
    micro = extract_breadcrumb_from_microdata(html)
    if micro:
        return micro, "microdata"
    dl = extract_category_from_datalayer(html)
    if dl:
        parts = re.split(r"\s*[>/\|›»]+\s*|\s*>\s*|\s*/\s*|\s*-\s*", dl)
        parts = [p.strip() for p in parts if p.strip()]
        return parts if parts else [dl], "datalayer"
    dom = extract_breadcrumb_from_dom(html)
    if dom:
        return dom, "dom"
    return [], "none"

# ===== Normalización / Regla =====
def normalize_crumbs(raw_crumbs: List[str]) -> Tuple[List[str], bool]:
    cleaned, had_any = [], False
    for c in raw_crumbs:
        if c is None:
            continue
        t = str(c).strip()
        if not t:
            continue
        had_any = True
        if t in {">", "/", "|", "›", "»", "•"}:
            continue
        if t.lower() in HOME_NOISE:
            continue
        if not cleaned or cleaned[-1] != t:
            cleaned.append(t)
    only_noise = (len(cleaned) == 0 and had_any)
    return cleaned, only_noise

def is_catalogado_from_limpios(crumbs_limpios: List[str]) -> bool:
    if len(crumbs_limpios) < 2:
        return False
    if any(MISC_PAT.search(c) for c in crumbs_limpios):
        return False
    return True

# ===== PLP -> PDP =====
def resolve_first_pdp_url_from_search_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    a = soup.select_one('a[href*="-p"], a[href*="/p/"]')
    if a and a.get("href"):
        return urljoin(DOMAIN, a["href"].strip())
    # canonical / og:url si ya es PDP
    link = soup.find("link", rel="canonical")
    if link and link.get("href") and ("-p" in link["href"] or "/p/" in link["href"]):
        return link["href"]
    meta = soup.find("meta", property="og:url")
    if meta and meta.get("content") and ("-p" in meta["content"] or "/p/" in meta["content"]):
        return meta["content"]
    return None

def best_effort_pdp_for_sku_with_cookies(sku: str, cookies: Dict[str, str]) -> tuple:
    """
    Busca con SESSION (que lleva tus cookies):
    - /busca?Ntt=SKU
    - si PLP: toma primer PDP y la abre
    Devuelve (pdp_url, html, crumbs_crudos, fuente, modo)
    """
    sess = new_session(cookies)
    search_url = DOMAIN.rstrip("/") + SEARCH_PATH.format(q=sku)

    r = session_get(search_url, session=sess)
    if r and r.status_code == 200 and r.text:
        # ¿redirigió a PDP?
        if ("-p" in r.url) or ("/p/" in r.url):
            crumbs, source = extract_any_taxonomy(r.text)
            if crumbs:
                return r.url, r.text, crumbs, source, "requests+cookies"
        # seguir en PLP -> capturar primer PDP
        pdp_url = resolve_first_pdp_url_from_search_html(r.text)
        if pdp_url:
            r2 = session_get(pdp_url, session=sess)
            if r2 and r2.status_code == 200 and r2.text:
                crumbs, source = extract_any_taxonomy(r2.text)
                if crumbs:
                    return pdp_url, r2.text, crumbs, source, "requests+cookies"

    return None, None, [], "none", "none"

def analyze_sku(sku: str, cookies: Dict[str, str]) -> Dict[str, str]:
    # Probar variantes de SKU (con y sin sufijo)
    for cand in candidate_skus(sku):
        url, html, crumbs_raw, source, mode = best_effort_pdp_for_sku_with_cookies(cand, cookies)
        if html:
            crumbs_limpios, solo_home = normalize_crumbs(crumbs_raw)
            if is_catalogado_from_limpios(crumbs_limpios):
                catalogado, obs = "Sí", ""
            else:
                if solo_home:
                    obs = "Breadcrumb indica solo Home/Inicio → NO catalogado"
                elif len(crumbs_limpios) == 1:
                    obs = "Solo 1 nivel útil en breadcrumb/categoría"
                else:
                    obs = "Faltan niveles o hay misc."
                catalogado = "No"

            return {
                "SKU": sku,
                "Catalogado": catalogado,
                "Breadcrumb_crudo": " > ".join(crumbs_raw),
                "Breadcrumb_limpio": " > ".join(crumbs_limpios),
                "FuenteTaxonomía": source,
                "URL": url or "",
                "Observación": obs,
                "Modo": mode,
                "HTML_len": str(len(html))
            }

    return {
        "SKU": sku,
        "Catalogado": "No",
        "Breadcrumb_crudo": "",
        "Breadcrumb_limpio": "",
        "FuenteTaxonomía": "none",
        "URL": "",
        "Observación": "No encontrado / sin HTML",
        "Modo": "none",
        "HTML_len": "0"
    }

def to_csv(rows: List[Dict[str, str]]) -> bytes:
    buf = io.StringIO()
    cols = ["SKU", "Catalogado", "Breadcrumb_crudo", "Breadcrumb_limpio",
            "FuenteTaxonomía", "URL", "Observación", "Modo", "HTML_len"]
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in cols})
    return buf.getvalue().encode("utf-8")

# ======= UI =======
st.set_page_config(page_title="Validador Catalogación simple.ripley.cl", layout="wide")
st.title("Validador de Catalogación (simple.ripley.cl) — Sesión con Cookies")
st.caption("Usa tus cookies de navegador para evitar bloqueos. NO subas cookies al repo. \
Regla: ≥2 niveles útiles y sin 'Otros/Miscel*' = Catalogado. 'Home' solo → NO catalogado.")

load_dotenv_if_available()  # opcional

with st.expander("🔐 Cargar cookies (elige un método)"):
    st.markdown("- **.env / variables de entorno**: `COOKIE_HEADER` o `COOKIES_JSON`.")
    st.markdown("- **Pegado manual**: pega el header completo `cookie: ...` (solo durante la sesión).")
    cookie_header_ui = st.text_area("Pega tu 'cookie:' header (opcional, se parsea en dict). No se guarda.", height=100)

# 1) cookies desde env
cookies_env = get_cookies_from_env()
# 2) cookies desde UI (si el usuario pega un header)
if cookie_header_ui.strip():
    try:
        cookies_from_ui = parse_cookie_header(cookie_header_ui.strip())
        cookies_env.update(cookies_from_ui)
    except Exception:
        st.warning("No se pudo parsear el cookie header pegado.")

cookie_status = "✅ cargadas" if cookies_env else "⚠️ no cargadas"
st.info(f"Estado cookies: {cookie_status}. (Consejo: usa .env o pega el header arriba)")

colA, colB = st.columns([3,2], gap="large")
with colA:
    raw = st.text_area("Pega SKUs (uno por línea)", height=220,
                       placeholder="MPM10002913810-4\nMPM10002913810\n7808774708749")
    run = st.button("Validar catalogación", type="primary")
with colB:
    delay = st.slider("Retardo entre SKUs (seg.)", 0.0, 2.0, 0.5, 0.1,
                      help="Evita bloqueos. Recomendado 0.5–1.0s.")
    only_no = st.toggle("Mostrar sólo NO catalogados", value=False)

if run and raw.strip():
    if not cookies_env:
        st.warning("No hay cookies cargadas. Es probable que el sitio bloquee el scraping. "
                   "Carga COOKIE_HEADER/COOKIES_JSON o pega el header en el expander de arriba.")
    skus = [s.strip() for s in raw.splitlines() if s.strip()]
    results: List[Dict[str, str]] = []
    progress = st.progress(0)
    status = st.empty()

    for i, sku in enumerate(skus, start=1):
        status.info(f"Procesando {i}/{len(skus)}: {sku}")
        res = analyze_sku(sku, cookies_env)
        results.append(res)
        progress.progress(i/len(skus))
        if delay:
            time.sleep(delay)

    status.success("Listo ✅")

    rows = results if not only_no else [r for r in results if r["Catalogado"] != "Sí"]
    st.subheader("Resultados")
    st.dataframe(rows, use_container_width=True)

    total = len(results)
    si = sum(1 for r in results if r["Catalogado"] == "Sí")
    no = total - si
    c1, c2, c3 = st.columns(3)
    c1.metric("Total SKUs", total)
    c2.metric("Catalogados", si)
    c3.metric("No catalogados", no)

    st.download_button("Descargar CSV (todos)", data=to_csv(results),
                       file_name="catalogacion_simple_ripley.csv", mime="text/csv")

    with st.expander("Diagnóstico (avanzado)"):
        st.write("Revisa Modo, FuenteTaxonomía y HTML_len. Si HTML_len≈0 → bloqueo. "
                 "Actualiza tus cookies (cf_clearance/JSESSIONID) o aumenta el delay.")
        diag_cols = ["SKU","Modo","FuenteTaxonomía","HTML_len","URL","Observación"]
        st.dataframe([{k: r.get(k, "") for k in diag_cols} for r in results], use_container_width=True)
