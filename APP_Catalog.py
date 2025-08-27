# app.py — Validador de catalogación por breadcrumb (Ripley)
# v2: Fallback a Playwright para páginas renderizadas con JS + mejores selectores y headers

import re
import time
import json
import csv
import io
import random
from typing import List, Tuple, Optional, Dict, Any

import streamlit as st
import requests
from bs4 import BeautifulSoup

# ===== Config =====
DOMAINS = [
    "https://www.ripley.com",      # global
    "https://simple.ripley.cl",    # Chile
    "https://www.ripley.com.pe",   # Perú
]
SEARCH_PATH = "/busca?Ntt={q}"
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
MISC_PAT = re.compile(r"(otros|miscel|varios|variedad|otros productos)", re.IGNORECASE)

# ===== Utilities =====
@st.cache_data(show_spinner=False)
def _install_playwright_once() -> None:
    """Instala Chromium para Playwright solo la primera vez (si está disponible)."""
    try:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
    except Exception:
        pass


def sleep_jitter(base: float = 0.4, spread: float = 0.8) -> None:
    time.sleep(base + random.random() * spread)


def candidate_skus(s: str) -> List[str]:
    s = s.strip()
    cands = [s]
    if "-" in s:
        base = s.split("-")[0].strip()
        if base and base not in cands:
            cands.append(base)
    return cands


def session_get(url: str, params=None, session: Optional[requests.Session] = None) -> Optional[requests.Response]:
    sess = session or requests.Session()
    try:
        r = sess.get(url, params=params, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200 and r.text:
            return r
    except requests.RequestException:
        return None
    return None


def extract_breadcrumb_from_html(html: str) -> list[str]:
    """
    Extrae migas de pan desde el DOM (li/a/span), JSON-LD y __NEXT_DATA__.
    Limpia 'Inicio/Home' y separadores.
    """
    from bs4 import BeautifulSoup
    import json, re

    soup = BeautifulSoup(html, "html.parser")

    def norm_text(x: str) -> str:
        return re.sub(r"\s+", " ", (x or "").strip())

    def clean(items: list[str]) -> list[str]:
        # quita separadores y 'inicio'
        bad_tokens = {">", "/", "|", "›", "»", "•"}
        bad_words = {"home", "inicio"}
        out = []
        for t in (norm_text(i) for i in items):
            if not t or t in bad_tokens or t.lower() in bad_words:
                continue
            if not out or out[-1] != t:  # dedupe consecutivo
                out.append(t)
        return out

    # ----- 1) DOM: soporta li/a/span -----
    root = soup.select_one(
        'nav[aria-label="breadcrumb"], nav.breadcrumb, ol.breadcrumb, ul.breadcrumb, div.breadcrumb'
    )
    if root:
        els = root.select("li, a, span, [itemprop='name']")
        crumbs = clean([e.get_text(" ", strip=True) for e in els])
        if crumbs:
            return crumbs

    # ----- 2) JSON-LD -----
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        raw = script.string or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        blocks = data if isinstance(data, list) else [data]
        for blk in blocks:
            # BreadcrumbList
            if isinstance(blk, dict) and blk.get("@type") in ("BreadcrumbList", "ItemList"):
                items = blk.get("itemListElement") or []
                names = []
                for it in items:
                    if isinstance(it, dict):
                        name = it.get("name")
                        if not name and isinstance(it.get("item"), dict):
                            name = it["item"].get("name")
                        if name:
                            names.append(str(name))
                names = clean(names)
                if names:
                    return names
            # Product.category como fallback
            if isinstance(blk, dict) and blk.get("@type") == "Product":
                cat = blk.get("category")
                if isinstance(cat, str) and cat.strip():
                    parts = re.split(r"\s*>\s*|/|\\|›|»|,", cat)
                    parts = clean(parts)
                    if parts:
                        return parts

    # ----- 3) __NEXT_DATA__ (estado de Next.js) -----
    nd = soup.find("script", id="__NEXT_DATA__")
    if nd and nd.string:
        try:
            j = json.loads(nd.string)
            acc = []
            def walk(x):
                if isinstance(x, dict):
                    for k, v in x.items():
                        if isinstance(v, list) and k.lower() in ("breadcrumb", "breadcrumbs"):
                            for it in v:
                                if isinstance(it, dict):
                                    n = it.get("name") or it.get("label") or it.get("title")
                                    if n: acc.append(str(n))
                                elif isinstance(it, str):
                                    acc.append(it)
                        else:
                            walk(v)
                elif isinstance(x, list):
                    for i in x:
                        walk(i)
            walk(j)
            acc = clean(acc)
            if acc:
                return acc
        except Exception:
            pass

    return []

def get_html_with_playwright(url: str, wait_selector: Optional[str] = None) -> Optional[str]:
    """Intenta obtener el HTML usando Playwright (solo si está instalado)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        _install_playwright_once()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=35000)
            sel = wait_selector or "nav[aria-label='breadcrumb'], [aria-label='breadcrumb'], .breadcrumb, .breadcrumbs, h1"
            try:
                page.wait_for_selector(sel, timeout=8000)
            except Exception:
                pass
            html = page.content()
            context.close()
            browser.close()
            return html
    except Exception:
        return None

def best_effort_pdp_via_requests(sku: str, base_url: str, session: requests.Session) -> Tuple[Optional[str], Optional[str]]:
    """Intenta obtener el PDP usando requests."""
    pdp_url = base_url.rstrip("/") + "/p/" + sku
    r = session_get(pdp_url, session=session)
    if r and r.status_code == 200:
        return pdp_url, r.text
    return None, None

def best_effort_pdp_for_sku(sku: str, base_url: str, use_playwright: bool, session: requests.Session) -> Tuple[Optional[str], Optional[str], str]:
    # 1) requests — rápido
    url, html = best_effort_pdp_via_requests(sku, base_url, session)
    if html:
        return url, html, "requests"

    # 2) Playwright — si se pidió o si la página parece client-side
    if use_playwright:
        search_url = base_url.rstrip("/") + SEARCH_PATH.format(q=sku)
        html_search = get_html_with_playwright(search_url)
        if html_search:
            crumbs = extract_breadcrumb_from_html(html_search)
            if crumbs:
                # Intentar obtener la página del producto desde la búsqueda
                soup = BeautifulSoup(html_search, "html.parser")
                links = soup.select("a[href*='/p/']")
                for a in links:
                    href = a.get("href")
                    if href and "/p/" in href:
                        pdp_url = base_url.rstrip("/") + href
                        html_pdp = get_html_with_playwright(pdp_url)
                        if html_pdp:
                            return pdp_url, html_pdp, "playwright(pdp)"
    return None, None, "none"

def analyze_sku(sku: str, base_url: str, use_playwright: bool) -> Dict[str, str]:
    sess = requests.Session()
    sess.headers.update(HEADERS)

    for cand in candidate_skus(sku):
        url, html, mode = best_effort_pdp_for_sku(cand, base_url, use_playwright, sess)
        if html:
            crumbs = extract_breadcrumb_from_html(html)
            catalogado = "No"
            obs = ""
            if len(crumbs) >= 2 and not any(MISC_PAT.search(c) for c in crumbs):
                catalogado = "Sí"
            else:
                obs = "Faltan niveles o hay misc."
            return {
                "SKU": sku,
                "Catalogado": catalogado,
                "Breadcrumb": " > ".join(crumbs),
                "URL": url or "",
                "Observación": obs,
                "Modo": mode,
                "HTML_len": str(len(html) if html else 0)
            }
    return {
        "SKU": sku,
        "Catalogado": "No",
        "Breadcrumb": "",
        "URL": "",
        "Observación": "No encontrado / sin HTML",
        "Modo": "none",
        "HTML_len": "0"
    }

def to_csv(rows: List[Dict[str, str]]) -> bytes:
    buf = io.StringIO()
    cols = ["SKU", "Catalogado", "Breadcrumb", "URL", "Observación", "Modo", "HTML_len"]
    writer = csv.DictWriter(buf, fieldnames=cols)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in cols})
    return buf.getvalue().encode("utf-8")

# ===== UI =====
st.set_page_config(page_title="Validador de Catalogación Ripley", layout="wide")

st.title("Validador de Catalogación por Breadcrumb (Ripley)")
st.caption("Pega SKUs, abrimos búsqueda → PDP y leemos breadcrumb. Regla: ≥2 niveles y sin 'Otros/Misceláneos/Var.*' = Catalogado. v2 con Playwright fallback.")

colA, colB = st.columns([3,2], gap="large")
with colA:
    domain = st.selectbox("Dominio de Ripley", DOMAINS, index=0)
    raw = st.text_area("Pega SKUs (uno por línea)", height=220, placeholder="MPM10002913810-4\nMPM10002913810\n7808774708749")
    run = st.button("Validar catalogación", type="primary")

with colB:
    st.markdown("**Parámetros**")
    use_playwright = st.toggle("Usar Playwright si hace falta (render JS)", value=True)
    delay = st.slider("Retardo entre SKUs (seg.)", 0.0, 2.0, 0.3, 0.1, help="Sé amable con el sitio y evita bloqueos.")
    st.toggle("Mostrar sólo NO catalogados en la tabla", value=False, key="only_no")

if run and raw.strip():
    skus = [s.strip() for s in raw.splitlines() if s.strip()]
    results = []
    progress = st.progress(0)
    status = st.empty()

    for i, sku in enumerate(skus, start=1):
        status.info(f"Procesando {i}/{len(skus)}: {sku}")
        res = analyze_sku(sku, domain, use_playwright)
        results.append(res)
        progress.progress(i/len(skus))
        if delay:
            time.sleep(delay)

    status.success("Listo ✅")

    rows = results
    if st.session_state.get("only_no"):
        rows = [r for r in results if r["Catalogado"] != "Sí"]

    st.subheader("Resultados")
    st.dataframe(rows, use_container_width=True)

    total = len(results)
    si = sum(1 for r in results if r["Catalogado"] == "Sí")
    no = total - si
    c1, c2, c3 = st.columns(3)
    c1.metric("Total SKUs", total)
    c2.metric("Catalogados", si)
    c3.metric("No catalogados", no)

    no_list = [r["SKU"] for r in results if r["Catalogado"] != "Sí"]
    if no_list:
        st.download_button("Descargar CSV (todos)", data=to_csv(results), file_name="catalogacion_ripley_v2.csv", mime="text/csv")
        st.download_button("Descargar SOLO no catalogados (CSV)", data=to_csv([r for r in results if r["Catalogado"] != "Sí"]), file_name="no_catalogados_v2.csv", mime="text/csv")
        st.text_area("SKUs NO catalogados (copiar/pegar)", value="\n".join(no_list), height=120)
    else:
        st.info("🎉 No se encontraron SKUs no catalogados.")

    with st.expander("Diagnóstico (avanzado)"):
        st.write("Si algo marcó 'No' por error, revisa 'Modo' y 'HTML_len': si 'requests' y HTML_len muy bajo → probablemente requería JS, usa Playwright.")
        st.dataframe([{k: r.get(k, "") for k in ("SKU", "Modo", "HTML_len", "URL")} for r in results], use_container_width=True)
