# -*- coding: utf-8 -*-
"""
Validador de Catalogación (simple.ripley.cl) — Vía API pública VTEX (sin cookies)
-------------------------------------------------------------------
Estrategia:
1) Para cada SKU (y su variante base sin sufijo después de '-'):
   - Consultar endpoints VTEX públicos (JSON):
       /api/catalog_system/pub/products/search/?fq=alternateIds_RefId:{sku}
       /api/catalog_system/pub/products/search/?fq=skuId:{sku}
       /api/catalog_system/pub/products/search/?ft={sku}
   - Si llega JSON, leer categorías (categories / categoryTree) y decidir "Catalogado".
2) Reglas:
   - Limpiar ruido (Home/Inicio, separadores)
   - "Sí" si hay ≥ 2 niveles útiles y no contiene misc/otros/varios.
3) Sin cookies, sin Playwright.

Diagnóstico:
- FuenteTaxonomía = vtex_api
- EndpointVTEX = cuál funcionó
- JSON_count = cuántos productos devolvió
"""

import re
import io
import csv
import cloudscraper
import time
from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin
from json import JSONDecodeError

import requests
import streamlit as st

# ---------- Config ----------
DOMAIN = "https://simple.ripley.cl"
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
}

MISC_PAT = re.compile(r"(otros|miscel|varios|variedad|otros productos)", re.IGNORECASE)
HOME_NOISE = {"home", "inicio", "búsqueda", "busqueda", "resultados", "search", "results"}

# ---------- Utils ----------
def candidate_skus(s: str) -> List[str]:
    s = s.strip()
    cands = [s]
    if "-" in s:
        base = s.split("-")[0].strip()
        if base and base not in cands:
            cands.append(base)
    return cands

def new_session() -> requests.Session:
    s = cloudscraper.create_scraper()
    s.headers.update(HEADERS)
    return s

def session_get_json(url: str, session: requests.Session) -> Optional[object]:
    try:
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 403:
            st.error(
                f"Cloudflare bloqueó la solicitud ({r.status_code}) para {url}. "
                "Revisa IP o cookies."
            )
        elif r.status_code == 200 and r.text:
            # En VTEX, siempre es JSON (lista o dict); si no, puede venir HTML de error
            try:
                return r.json()
            except JSONDecodeError as e:
                st.warning(f"Error al decodificar JSON ({r.status_code}) {url}: {e}")
        else:
            st.warning(f"Solicitud falló ({r.status_code}) para {url}")
    except requests.RequestException as e:
        st.warning(f"Error de red al solicitar {url}: {e}")
    return None

def normalize_crumbs(raw_crumbs: List[str]) -> Tuple[List[str], bool]:
    cleaned, had_any = [], False
    for c in raw_crumbs:
        if c is None:
            continue
        t = str(c).strip()
        if not t:
            continue
        had_any = True
        # separadores típicos
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

# ---------- VTEX parsing ----------
def _split_catpath(catpath: str) -> List[str]:
    """
    Catpath típico de VTEX: '/Moda/Mujer/Bottoms/'
    """
    parts = [p.strip() for p in catpath.split("/") if p.strip()]
    return parts

def extract_categories_from_vtex_product(prod: dict) -> List[str]:
    """
    Intenta extraer la ruta de categoría más profunda disponible.
    Preferencias:
      - prod['categories'] (lista de strings '/A/B/C/')
      - prod['categoryTree'] (lista de dicts [{'id','name'}] en algunos catálogos)
    Devuelve una lista de nombres en orden jerárquico.
    """
    # 1) categories (lista de paths)
    cats = prod.get("categories")
    best: List[str] = []
    if isinstance(cats, list) and cats:
        # tomar el path más largo (más niveles)
        paths = []
        for c in cats:
            if isinstance(c, str) and c.strip():
                parts = _split_catpath(c)
                if parts:
                    paths.append(parts)
        if paths:
            best = max(paths, key=lambda p: len(p))

    # 2) categoryTree (algunos VTEX exponen esto)
    if not best:
        tree = prod.get("categoryTree")
        if isinstance(tree, list) and tree:
            names = []
            for node in tree:
                name = None
                if isinstance(node, dict):
                    name = node.get("name") or node.get("Title") or node.get("title")
                if isinstance(name, str) and name.strip():
                    names.append(name.strip())
            if names:
                best = names

    return best

def build_pdp_url_from_vtex(prod: dict) -> Optional[str]:
    """
    Muchos VTEX exponen 'link' o 'linkText' para construir la PDP:
      - link (URL absoluta o relativa)
      - linkText -> '/{linkText}-p' en la mayoría de tiendas VTEX
    """
    link = prod.get("link")
    if isinstance(link, str) and link.strip():
        return urljoin(DOMAIN, link.strip())
    link_text = prod.get("linkText")
    if isinstance(link_text, str) and link_text.strip():
        # Asegurar sufijo '-p'
        path = link_text.strip()
        if not path.endswith("-p"):
            path = f"{path}-p"
        if not path.startswith("/"):
            path = "/" + path
        return urljoin(DOMAIN, path)
    return None

def vtex_lookup_for_sku(sku: str, session: requests.Session) -> Tuple[Optional[str], List[str], str, int]:
    """
    Prueba varios endpoints VTEX para obtener el producto y categorías.
    Devuelve: (pdp_url, crumbs_raw, endpoint_usado, json_count)
    """
    endpoints = [
        f"/api/catalog_system/pub/products/search/?fq=alternateIds_RefId:{sku}",
        f"/api/catalog_system/pub/products/search/?fq=skuId:{sku}",
        f"/api/catalog_system/pub/products/search/?ft={sku}",
    ]
    for ep in endpoints:
        url = urljoin(DOMAIN, ep)
        data = session_get_json(url, session)
        if isinstance(data, list) and len(data) > 0:
            prod = data[0]
            crumbs = extract_categories_from_vtex_product(prod)
            pdp = build_pdp_url_from_vtex(prod) or ""
            return pdp, crumbs, ep, len(data)
    return None, [], "none", 0

# ---------- Main logic ----------
def analyze_sku(sku: str, sess: requests.Session) -> Dict[str, str]:
    for cand in candidate_skus(sku):
        pdp_url, crumbs_raw, endpoint, n = vtex_lookup_for_sku(cand, sess)
        if crumbs_raw:
            crumbs_limpios, solo_home = normalize_crumbs(crumbs_raw)
            if is_catalogado_from_limpios(crumbs_limpios):
                catalogado, obs = "Sí", ""
            else:
                if solo_home:
                    obs = "Sólo Home/Inicio en categorías"
                elif len(crumbs_limpios) == 1:
                    obs = "Sólo 1 nivel útil en categorías"
                else:
                    obs = "Faltan niveles o hay misc."
                catalogado = "No"
            return {
                "SKU": sku,
                "Catalogado": catalogado,
                "Breadcrumb_crudo": " > ".join(crumbs_raw),
                "Breadcrumb_limpio": " > ".join(crumbs_limpios),
                "FuenteTaxonomía": "vtex_api",
                "EndpointVTEX": endpoint,
                "URL": pdp_url or "",
                "Observación": obs,
                "Modo": "vtex",
                "JSON_count": str(n),
                "HTML_len": "-"  # no usamos HTML
            }
        # si no hubo crumbs pero hubo respuesta, igual reportamos
        if endpoint != "none" and n > 0:
            return {
                "SKU": sku,
                "Catalogado": "No",
                "Breadcrumb_crudo": "",
                "Breadcrumb_limpio": "",
                "FuenteTaxonomía": "vtex_api",
                "EndpointVTEX": endpoint,
                "URL": pdp_url or "",
                "Observación": "Respuesta sin categorías en JSON",
                "Modo": "vtex",
                "JSON_count": str(n),
                "HTML_len": "-"
            }

    # ningún endpoint devolvió producto
    return {
        "SKU": sku,
        "Catalogado": "No",
        "Breadcrumb_crudo": "",
        "Breadcrumb_limpio": "",
        "FuenteTaxonomía": "none",
        "EndpointVTEX": "none",
        "URL": "",
        "Observación": "No encontrado / sin datos",
        "Modo": "vtex",
        "JSON_count": "0",
        "HTML_len": "-"
    }

def to_csv(rows: List[Dict[str, str]]) -> bytes:
    buf = io.StringIO()
    cols = ["SKU","Catalogado","Breadcrumb_crudo","Breadcrumb_limpio","FuenteTaxonomía",
            "EndpointVTEX","URL","Observación","Modo","JSON_count","HTML_len"]
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in cols})
    return buf.getvalue().encode("utf-8")

# ---------- UI ----------
st.set_page_config(page_title="Validador Catalogación (VTEX API)", layout="wide")
st.title("Validador de Catalogación — usando API pública (sin cookies)")
st.caption("Consultamos endpoints públicos VTEX para extraer categorías. "
           "Regla: ≥2 niveles útiles y sin 'Otros/Miscel*' = Catalogado.")

colA, colB = st.columns([3,2], gap="large")
with colA:
    raw = st.text_area("Pega SKUs (uno por línea)", height=220,
                       placeholder="MPM10002913810-4\nMPM10002913810\n7808774708749")
    run = st.button("Validar catalogación", type="primary")
with colB:
    delay = st.slider("Retardo entre SKUs (seg.)", 0.0, 2.0, 0.3, 0.1,
                      help="Evita rate-limit de la API pública.")
    only_no = st.toggle("Mostrar sólo NO catalogados", value=False)

if run and raw.strip():
    skus = [s.strip() for s in raw.splitlines() if s.strip()]
    results: List[Dict[str, str]] = []
    progress = st.progress(0)
    status = st.empty()

    sess = new_session()
    try:
        for i, sku in enumerate(skus, start=1):
            status.info(f"Procesando {i}/{len(skus)}: {sku}")
            res = analyze_sku(sku, sess)
            results.append(res)
            progress.progress(i/len(skus))
            if delay:
                time.sleep(delay)
    finally:
        sess.close()

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
                       file_name="catalogacion_vtex.csv", mime="text/csv")

    with st.expander("Diagnóstico (avanzado)"):
        st.write("Si EndpointVTEX='none' → la API no devolvió datos para ese SKU "
                 "(prueba sin sufijo después de '-' o aumenta delay).")
        diag_cols = ["SKU","Modo","FuenteTaxonomía","EndpointVTEX","JSON_count","URL","Observación"]
        st.dataframe([{k: r.get(k, "") for k in diag_cols} for r in results], use_container_width=True)
