import re
import time
import json
import csv
import io
from typing import List, Tuple, Optional

import streamlit as st
import requests
from bs4 import BeautifulSoup

# ---------- Config ----------
DEFAULT_BASE_URL = "https://www.ripley.com"  # cambia a .cl / .pe según necesites
SEARCH_PATH = "/busca?Ntt={q}"               # ruta de búsqueda
TIMEOUT = 12
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
}
# Palabras que consideramos "misceláneas" (mal catalogado)
MISC_PAT = re.compile(r"(otros|miscel|varios|variedad)", re.IGNORECASE)

# ---------- Helpers ----------
def normalize_sku(raw: str) -> str:
    """Limpia y remueve sufijos de Offer SKU (p.ej., -4) para acercarnos al Product SKU."""
    s = raw.strip()
    # si es "MPM....-algo", intenta quitar sufijo después del último "-"
    # pero conserva el original por si el PDP existe con sufijo
    return s

def candidate_skus(s: str) -> List[str]:
    """
    Genera candidatos de búsqueda:
    - El SKU tal cual.
    - Si tiene guion, también versión recortada (posible Product SKU).
    """
    cands = [s]
    if "-" in s:
        base = s.split("-")[0].strip()
        if base and base not in cands:
            cands.append(base)
    return cands

def get(url: str, params=None) -> Optional[requests.Response]:
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200 and r.text.strip():
            return r
    except requests.RequestException:
        return None
    return None

def extract_breadcrumb_from_html(html: str) -> List[str]:
    """
    Intenta extraer migas de pan desde varias estructuras:
    - DOM con aria-label="breadcrumb" o clases comunes
    - JSON-LD (BreadcrumbList)
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) DOM típico
    selectors = [
        '[aria-label="breadcrumb"] li',
        'nav[aria-label="breadcrumb"] li',
        '.breadcrumb li',
        '.breadcrumbs li'
    ]
    for sel in selectors:
        els = soup.select(sel)
        crumbs = [e.get_text(strip=True) for e in els if e.get_text(strip=True)]
        crumbs = [c for c in crumbs if c != "/"]  # limpiar separadores
        if len(crumbs) >= 1:
            return crumbs

    # 2) JSON-LD con BreadcrumbList
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue
        # puede venir como dict o lista
        blocks = data if isinstance(data, list) else [data]
        for blk in blocks:
            if isinstance(blk, dict) and blk.get("@type") in ["BreadcrumbList", "ItemList"]:
                items = blk.get("itemListElement") or []
                names = []
                for it in items:
                    if isinstance(it, dict):
                        # Caso Microdata expandido
                        name = it.get("name")
                        if not name and isinstance(it.get("item"), dict):
                            name = it["item"].get("name")
                        if name:
                            names.append(str(name).strip())
                if names:
                    return names
    return []

def is_catalogado(crumbs: List[str]) -> bool:
    """
    Catálogo válido si:
    - ≥ 2 niveles (ej. Departamento > Categoría)
    - No cae en 'Otros/Misceláneos/Variados'
    """
    if len(crumbs) < 2:
        return False
    joined = " > ".join(crumbs)
    if MISC_PAT.search(joined):
        return False
    return True

def find_first_pdp_url_from_search(html: str, base_url: str) -> Optional[str]:
    """
    Desde resultados de búsqueda, intenta capturar el primer link a PDP.
    Ajusta selectores si cambia el frontend.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Heurísticas: enlaces a tarjetas de producto
    # Busca <a> con href que parezca PDP (contenga /p/ ó /product ó slug del producto)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = base_url.rstrip("/") + "/" + href.lstrip("/")
        # Filtros básicos (ajustables)
        if any(x in href for x in ["/p/", "/product", "/producto", "/prod/"]):
            return href
    # fallback: primer anchor con data-product
    a = soup.find("a", attrs={"data-product": True})
    if a and a.get("href"):
        href = a["href"]
        if not href.startswith("http"):
            href = base_url.rstrip("/") + "/" + href.lstrip("/")
        return href
    return None

def best_effort_pdp_for_sku(sku: str, base_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Dado un SKU, intenta:
    1) Buscar directamente en /busca?Ntt=SKU
    2) Tomar el primer PDP candidato
    Retorna (pdp_url, html_pdp) o (None, None) si no encuentra.
    """
    search_url = base_url.rstrip("/") + SEARCH_PATH.format(q=sku)
    r = get(search_url)
    if not r:
        return None, None

    # ¿la propia búsqueda ya redirigió a PDP?
    # Heurística: si el HTML contiene breadcrumb, ya es PDP.
    crumbs = extract_breadcrumb_from_html(r.text)
    if crumbs:
        return search_url, r.text  # cayó directo

    # Si es SERP, toma primer PDP
    pdp_url = find_first_pdp_url_from_search(r.text, base_url)
    if not pdp_url:
        return None, None

    pdp = get(pdp_url)
    if not pdp:
        return None, None

    return pdp_url, pdp.text

def analyze_sku(sku: str, base_url: str) -> dict:
    """
    Lógica principal por SKU:
    - Prueba el SKU tal cual y variantes (sin sufijo) si hay '-'
    """
    for cand in candidate_skus(sku):
        url, html = best_effort_pdp_for_sku(cand, base_url)
        if html:
            crumbs = extract_breadcrumb_from_html(html)
            return {
                "SKU": sku,
                "URL": url,
                "Breadcrumb": " > ".join(crumbs) if crumbs else "",
                "Catalogado": "Sí" if is_catalogado(crumbs) else "No",
                "Observación": "" if is_catalogado(crumbs) else (
                    "Sin breadcrumb/1 nivel/Misceláneos"
                    if crumbs else "No se detectó breadcrumb"
                )
            }
    return {
        "SKU": sku,
        "URL": "",
        "Breadcrumb": "",
        "Catalogado": "No",
        "Observación": "No encontrado en búsqueda"
    }

def to_csv(rows: List[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["SKU", "Catalogado", "Breadcrumb", "URL", "Observación"])
    writer.writeheader()
    for r in rows:
        writer.writerow({
            "SKU": r.get("SKU",""),
            "Catalogado": r.get("Catalogado",""),
            "Breadcrumb": r.get("Breadcrumb",""),
            "URL": r.get("URL",""),
            "Observación": r.get("Observación",""),
        })
    return buf.getvalue().encode("utf-8")

# ---------- UI ----------
st.set_page_config(page_title="Validador de Catalogación Ripley", layout="wide")

st.title("Validador de Catalogación por Breadcrumb (Ripley)")
st.caption("Pega SKUs, la app abre resultados de búsqueda, entra al PDP y lee las migas de pan. Reglas: ≥2 niveles y sin 'Otros/Misceláneos' = Catalogado.")

colA, colB = st.columns([3,2], gap="large")
with colA:
    domain = st.selectbox(
        "Dominio de Ripley",
        ["https://www.ripley.com", "https://simple.ripley.cl", "https://www.ripley.com.pe"],
        index=0
    )
    raw = st.text_area(
        "Pega SKUs (uno por línea)",
        height=220,
        placeholder="MPM10002913810-4\nMPM10002913810\n7808774708749"
    )
    run = st.button("Validar catalogación", type="primary")

with colB:
    st.markdown("**Parámetros (opcional)**")
    delay = st.slider("Pequeño retardo entre SKUs (seg.)", 0.0, 2.0, 0.2, 0.1,
                      help="Para ser amables con el sitio.")
    st.toggle("Mostrar sólo NO catalogados en la tabla", value=False, key="only_no")

if run and raw.strip():
    skus = [normalize_sku(s) for s in raw.splitlines() if s.strip()]
    results = []
    progress = st.progress(0)
    status = st.empty()

    for i, sku in enumerate(skus, start=1):
        status.info(f"Procesando {i}/{len(skus)}: {sku}")
        res = analyze_sku(sku, domain)
        results.append(res)
        progress.progress(i/len(skus))
        if delay:
            time.sleep(delay)

    status.success("Listo ✅")

    # Tabla
    rows = results
    if st.session_state.get("only_no"):
        rows = [r for r in results if r["Catalogado"] != "Sí"]

    st.subheader("Resultados")
    st.dataframe(rows, use_container_width=True)

    # KPIs
    total = len(results)
    si = sum(1 for r in results if r["Catalogado"] == "Sí")
    no = total - si
    col1, col2, col3 = st.columns(3)
    col1.metric("Total SKUs", total)
    col2.metric("Catalogados", si)
    col3.metric("No catalogados", no)

    # Copiar sólo NO catalogados (simple)
    no_list = [r["SKU"] for r in results if r["Catalogado"] != "Sí"]
    if no_list:
        no_blob = "\n".join(no_list)
        st.download_button(
            "Descargar CSV (todos)",
            data=to_csv(results),
            file_name="catalogacion_ripley.csv",
            mime="text/csv"
        )
        st.download_button(
            "Descargar SOLO no catalogados (CSV)",
            data=to_csv([r for r in results if r["Catalogado"] != "Sí"]),
            file_name="no_catalogados.csv",
            mime="text/csv"
        )
        st.text_area("SKUs NO catalogados (copiar/pegar)", value=no_blob, height=120)
    else:
        st.info("🎉 No se encontraron SKUs no catalogados.")
