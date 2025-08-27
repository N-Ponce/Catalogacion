import re
import time
import csv
import io
import json
import random
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin

import streamlit as st
import requests
from bs4 import BeautifulSoup

# ===== Configuración solo para simple.ripley.cl =====
DOMAIN = "https://simple.ripley.cl"
SEARCH_PATH = "/busca?Ntt={q}"
TIMEOUT = 25
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

def candidate_skus(s: str) -> List[str]:
    s = s.strip()
    cands = [s]
    if "-" in s:
        base = s.split("-")[0].strip()
        if base and base not in cands:
            cands.append(base)
    return cands

def session_get(url: str, session: Optional[requests.Session] = None) -> Optional[requests.Response]:
    sess = session or requests.Session()
    try:
        r = sess.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r
    except requests.RequestException:
        return None
    return None

# ====== Playwright helpers ======
def pw_available() -> bool:
    try:
        import importlib
        importlib.import_module("playwright.sync_api")
        return True
    except Exception:
        return False

def get_html_with_playwright(url: str) -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=50000)
            html = page.content()
            context.close()
            browser.close()
            return html
    except Exception:
        return None

def get_pdp_html_via_playwright_from_search(search_url: str) -> Tuple[Optional[str], Optional[str]]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(search_url, wait_until="networkidle", timeout=50000)
            sel = 'a[href*="-p"], a[href*="/p/"]'
            el = page.query_selector(sel)
            if not el:
                html_plp = page.content()
                context.close()
                browser.close()
                return None, None
            href = el.get_attribute("href")
            if href:
                pdp_url = urljoin(DOMAIN, href)
                page.goto(pdp_url, wait_until="networkidle", timeout=50000)
                html = page.content()
                context.close()
                browser.close()
                return pdp_url, html
            context.close()
            browser.close()
            return None, None
    except Exception:
        return None, None

# ====== Extractores ======
def extract_breadcrumb_from_jsonld(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        txt = s.string or ""
        if not txt.strip():
            continue
        # A veces vienen múltiples JSON pegados, intenta línea por línea
        candidates = []
        try:
            data = json.loads(txt)
            candidates = data if isinstance(data, list) else [data]
        except Exception:
            for line in txt.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    candidates.append(json.loads(line))
                except Exception:
                    pass
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            # BreadcrumbList directo
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
                        name = name.strip()
                        if name and name.lower() not in {"home", "inicio"}:
                            names.append(name)
                if names:
                    return names
            # BreadcrumbList embebido en "@graph"
            if "@graph" in obj and isinstance(obj["@graph"], list):
                for g in obj["@graph"]:
                    if isinstance(g, dict) and g.get("@type") == "BreadcrumbList":
                        names = []
                        for it in g.get("itemListElement", []):
                            name = (isinstance(it.get("item"), dict) and it["item"].get("name")) or it.get("name")
                            if isinstance(name, str):
                                name = name.strip()
                                if name and name.lower() not in {"home", "inicio"}:
                                    names.append(name)
                        if names:
                            return names
    return []

def extract_product_category_from_jsonld(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", type="application/ld+json"):
        txt = s.string or ""
        if not txt.strip():
            continue
        candidates = []
        try:
            data = json.loads(txt)
            candidates = data if isinstance(data, list) else [data]
        except Exception:
            for line in txt.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    candidates.append(json.loads(line))
                except Exception:
                    pass
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
    # Busca dataLayer / vtex variables con category/department
    m = re.search(r"dataLayer\s*=\s*(\[[\s\S]*?\])", html)
    if not m:
        m = re.search(r"vtex[\s\S]{0,50}=\s*(\{[\s\S]*?\});", html)
    if m:
        txt = m.group(1)
        try:
            data = json.loads(txt)
            # Puede ser lista de dicts
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
    """
    Devuelve (niveles, fuente) donde fuente ∈ {jsonld_breadcrumb, jsonld_product, microdata, datalayer, dom, none}
    """
    crumbs = extract_breadcrumb_from_jsonld(html)
    if crumbs:
        return crumbs, "jsonld_breadcrumb"
    cat = extract_product_category_from_jsonld(html)
    if cat:
        # separar por separadores comunes
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

# ====== PLP -> PDP ======
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

def best_effort_pdp_for_sku(sku: str, use_playwright: bool, session: requests.Session) -> tuple:
    search_url = DOMAIN.rstrip("/") + SEARCH_PATH.format(q=sku)
    r = session_get(search_url, session=session)
    if r and r.status_code == 200 and r.text:
        if ("-p" in r.url) or ("/p/" in r.url):
            crumbs, source = extract_any_taxonomy(r.text)
            if crumbs:
                return r.url, r.text, crumbs, source, "requests"
        pdp_url = resolve_first_pdp_url_from_search_html(r.text)
        if pdp_url:
            r2 = session_get(pdp_url, session=session)
            if r2 and r2.status_code == 200 and r2.text:
                crumbs, source = extract_any_taxonomy(r2.text)
                if crumbs:
                    return pdp_url, r2.text, crumbs, source, "requests"

    if use_playwright and pw_available():
        pdp_url, pdp_html = get_pdp_html_via_playwright_from_search(search_url)
        if pdp_url and pdp_html:
            crumbs, source = extract_any_taxonomy(pdp_html)
            if crumbs:
                return pdp_url, pdp_html, crumbs, source, "playwright"
        html_play = get_html_with_playwright(search_url)
        if html_play:
            crumbs, source = extract_any_taxonomy(html_play)
            if crumbs:
                return search_url, html_play, crumbs, source, "playwright"

    return None, None, [], "none", "none"

def is_catalogado(crumbs: List[str]) -> bool:
    if len(crumbs) < 2:
        return False
    if any(MISC_PAT.search(c) for c in crumbs):
        return False
    return True

def analyze_sku(sku: str, use_playwright: bool, reveal_html_preview: bool=False) -> Dict[str, str]:
    sess = requests.Session()
    sess.headers.update(HEADERS)

    for cand in candidate_skus(sku):
        url, html, crumbs, source, mode = best_effort_pdp_for_sku(cand, use_playwright, sess)
        if html:
            cat = "Sí" if is_catalogado(crumbs) else "No"
            obs = "" if cat == "Sí" else "Faltan niveles/misc o category no clara"
            row = {
                "SKU": sku,
                "Catalogado": cat,
                "Breadcrumb/Category": " > ".join(crumbs),
                "FuenteTaxonomía": source,
                "URL": url or "",
                "Modo": mode,
                "HTML_len": str(len(html))
            }
            if reveal_html_preview:
                row["HTML_preview"] = (html[:1000] + "…") if len(html) > 1000 else html
            return row

    return {
        "SKU": sku,
        "Catalogado": "No",
        "Breadcrumb/Category": "",
        "FuenteTaxonomía": "none",
        "URL": "",
        "Modo": "none",
        "HTML_len": "0",
        "HTML_preview": "" if not reveal_html_preview else ""
    }

def to_csv(rows: List[Dict[str, str]]) -> bytes:
    buf = io.StringIO()
    cols = ["SKU", "Catalogado", "Breadcrumb/Category", "FuenteTaxonomía", "URL", "Modo", "HTML_len"]
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in cols})
    return buf.getvalue().encode("utf-8")

# ===== UI =====
st.set_page_config(page_title="Validador Catalogación simple.ripley.cl", layout="wide")
st.title("Validador de Catalogación (simple.ripley.cl)")
st.caption("Buscamos PDP, extraemos breadcrumb/categoría (JSON-LD, microdatos, dataLayer o DOM). Regla: ≥2 niveles y sin 'Otros/Miscel*' = Catalogado.")

colA, colB = st.columns([3,2], gap="large")
with colA:
    raw = st.text_area("Pega SKUs (uno por línea)", height=220, placeholder="MPM10002913810-4\nMPM10002913810\n7808774708749")
    run = st.button("Validar catalogación", type="primary")
with colB:
    st.markdown("**Parámetros**")
    use_playwright = st.toggle("Usar Playwright si hace falta (render JS)", value=True)
    delay = st.slider("Retardo entre SKUs (seg.)", 0.0, 2.0, 0.5, 0.1, help="Evita bloqueos del sitio.")
    reveal = st.toggle("Mostrar vista previa HTML (diagnóstico)", value=False, help="Incluye las primeras ~1000 letras del HTML para depurar.")

if run and raw.strip():
    skus = [s.strip() for s in raw.splitlines() if s.strip()]
    results = []
    progress = st.progress(0)
    status = st.empty()

    for i, sku in enumerate(skus, start=1):
        status.info(f"Procesando {i}/{len(skus)}: {sku}")
        res = analyze_sku(sku, use_playwright, reveal_html_preview=reveal)
        results.append(res)
        progress.progress(i/len(skus))
        if delay:
            time.sleep(delay)

    status.success("Listo ✅")
    rows = results

    st.subheader("Resultados")
    st.dataframe(rows, use_container_width=True)

    total = len(results)
    si = sum(1 for r in results if r["Catalogado"] == "Sí")
    no = total - si
    c1, c2, c3 = st.columns(3)
    c1.metric("Total SKUs", total)
    c2.metric("Catalogados", si)
    c3.metric("No catalogados", no)

    st.download_button("Descargar CSV (todos)", data=to_csv(results), file_name="catalogacion_simple_ripley.csv", mime="text/csv")

    with st.expander("Diagnóstico (avanzado)"):
        st.write("Revisa Modo, FuenteTaxonomía y HTML_len. Si 'requests' + HTML_len bajo → probablemente contenido via JS (usa Playwright). Si FuenteTaxonomía='none' en PDP, puede que el sitio o el bot-block esté ocultando los datos estructurados.")
        diag_cols = ["SKU","Modo","FuenteTaxonomía","HTML_len","URL"] + (["HTML_preview"] if reveal else [])
        st.dataframe([{k: r.get(k, "") for k in diag_cols} for r in results], use_container_width=True)
