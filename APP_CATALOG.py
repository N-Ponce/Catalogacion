import re
import time
import csv
import io
import json
import random
from typing import List, Dict, Optional
from urllib.parse import urljoin

import streamlit as st
import requests
from bs4 import BeautifulSoup

# ===== Configuración solo para simple.ripley.cl =====
DOMAIN = "https://simple.ripley.cl"
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
    "Pragma": "no-cache",
}
MISC_PAT = re.compile(r"(otros|miscel|varios|variedad|otros productos)", re.IGNORECASE)

def sleep_jitter(base: float = 0.3, spread: float = 0.6) -> None:
    time.sleep(base + random.random() * spread)

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
            page.goto(url, wait_until="networkidle", timeout=45000)
            html = page.content()
            context.close()
            browser.close()
            return html
    except Exception:
        return None

def get_pdp_html_via_playwright_from_search(search_url: str) -> (Optional[str], Optional[str]):
    """
    Abre la PLP con Playwright, localiza el primer enlace a PDP y navega a esa PDP,
    retornando (pdp_url, pdp_html). Si no encuentra enlace, retorna (None, None).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(search_url, wait_until="networkidle", timeout=45000)

            # Busca un enlace con patrón típico de PDP
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
                page.goto(pdp_url, wait_until="networkidle", timeout=45000)
                html = page.content()
                context.close()
                browser.close()
                return pdp_url, html

            context.close()
            browser.close()
            return None, None
    except Exception:
        return None, None

# ====== Breadcrumb extractors ======
def extract_breadcrumb_from_jsonld(html: str) -> List[str]:
    """
    Intenta parsear JSON-LD con @type BreadcrumbList.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        for s in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(s.string or "")
            except Exception:
                continue
            # Puede venir como objeto o lista
            candidates = data if isinstance(data, list) else [data]
            for obj in candidates:
                if not isinstance(obj, dict):
                    continue
                if obj.get("@type") == "BreadcrumbList" and isinstance(obj.get("itemListElement"), list):
                    names = []
                    for it in obj["itemListElement"]:
                        # Puede venir como dict con item/name o directamente name
                        name = (
                            (it.get("item") or {}).get("name")
                            if isinstance(it.get("item"), dict)
                            else it.get("name")
                        )
                        if isinstance(name, str):
                            name = name.strip()
                            if name and name.lower() not in {"home", "inicio"}:
                                names.append(name)
                    if names:
                        return names
    except Exception:
        pass
    return []

def extract_breadcrumb_from_dom(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")

    # Selectores comunes
    candidates = [
        'nav[aria-label="breadcrumb"]',
        "ol.breadcrumb, ul.breadcrumb, div.breadcrumb, div.breadcrumbs, li.breadcrumbs, nav.breadcrumb, nav.breadcrumbs",
    ]
    for sel in candidates:
        node = soup.select_one(sel)
        if node:
            crumbs = []
            for tag in node.find_all(["a", "span", "li"]):
                txt = tag.get_text(" ", strip=True)
                if txt and txt not in {">", "/", "|", "›", "»", "•"} and txt.lower() not in {"home", "inicio"}:
                    crumbs.append(txt)
            # De-dup consecutivos
            result = []
            for c in crumbs:
                if not result or result[-1] != c:
                    result.append(c)
            if result:
                return result
    return []

def extract_breadcrumb_from_html(html: str) -> List[str]:
    """
    Primero JSON-LD, luego DOM.
    """
    crumbs = extract_breadcrumb_from_jsonld(html)
    if crumbs:
        return crumbs
    return extract_breadcrumb_from_dom(html)

# ====== Navegación PLP -> PDP ======
def resolve_first_pdp_url_from_search_html(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    # 1) enlaces con "-p" o "/p/"
    a = soup.select_one('a[href*="-p"], a[href*="/p/"]')
    if a and a.get("href"):
        return urljoin(DOMAIN, a["href"].strip())
    # 2) fallback: canonical u og:url (por si ya estamos en PDP)
    link = soup.find("link", rel="canonical")
    if link and link.get("href") and ("-p" in link["href"] or "/p/" in link["href"]):
        return link["href"]
    meta = soup.find("meta", property="og:url")
    if meta and meta.get("content") and ("-p" in meta["content"] or "/p/" in meta["content"]):
        return meta["content"]
    return None

def best_effort_pdp_for_sku(sku: str, use_playwright: bool, session: requests.Session) -> tuple:
    """
    Busca el SKU, localiza la PDP y retorna (pdp_url, pdp_html, crumbs, mode)
    mode ∈ {"requests", "playwright", "none"}
    """
    search_url = DOMAIN.rstrip("/") + SEARCH_PATH.format(q=sku)

    # 1) Intento con requests
    r = session_get(search_url, session=session)
    if r and r.status_code == 200 and r.text:
        # ¿Ya estamos en PDP por redirect server-side?
        if ("-p" in r.url) or ("/p/" in r.url):
            crumbs = extract_breadcrumb_from_html(r.text)
            if crumbs:
                return r.url, r.text, crumbs, "requests"

        # Si no, parseo la PLP para extraer el primer PDP
        pdp_url = resolve_first_pdp_url_from_search_html(r.text)
        if pdp_url:
            r2 = session_get(pdp_url, session=session)
            if r2 and r2.status_code == 200 and r2.text:
                crumbs = extract_breadcrumb_from_html(r2.text)
                if crumbs:
                    return pdp_url, r2.text, crumbs, "requests"

    # 2) Fallback con Playwright (PLP -> click a PDP)
    if use_playwright:
        pdp_url, pdp_html = get_pdp_html_via_playwright_from_search(search_url)
        if pdp_url and pdp_html:
            crumbs = extract_breadcrumb_from_html(pdp_html)
            if crumbs:
                return pdp_url, pdp_html, crumbs, "playwright"

        # último intento: quizá la PLP directamente trae JSON-LD útil
        html_play = get_html_with_playwright(search_url)
        if html_play:
            crumbs = extract_breadcrumb_from_html(html_play)
            if crumbs:
                return search_url, html_play, crumbs, "playwright"

    return None, None, [], "none"

def analyze_sku(sku: str, use_playwright: bool) -> Dict[str, str]:
    sess = requests.Session()
    sess.headers.update(HEADERS)

    for cand in candidate_skus(sku):
        url, html, crumbs, mode = best_effort_pdp_for_sku(cand, use_playwright, sess)
        if html:
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
st.set_page_config(page_title="Validador Catalogación simple.ripley.cl", layout="wide")

st.title("Validador de Catalogación por Breadcrumb (simple.ripley.cl)")
st.caption("Pega SKUs ➜ buscamos PDP ➜ leemos breadcrumb. Regla: ≥2 niveles y sin 'Otros/Misceláneos/Var.*' = Catalogado.")

colA, colB = st.columns([3,2], gap="large")
with colA:
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
        res = analyze_sku(sku, use_playwright)
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
    st.download_button("Descargar CSV (todos)", data=to_csv(results), file_name="catalogacion_simple_ripley.csv", mime="text/csv")
    if no_list:
        st.download_button("Descargar SOLO no catalogados (CSV)", data=to_csv([r for r in results if r["Catalogado"] != "Sí"]), file_name="no_catalogados.csv", mime="text/csv")
        st.text_area("SKUs NO catalogados (copiar/pegar)", value="\n".join(no_list), height=120)
    else:
        st.info("🎉 No se encontraron SKUs no catalogados.")

    with st.expander("Diagnóstico (avanzado)"):
        st.write("Si algo marcó 'No' por error, revisa 'Modo' y 'HTML_len'. 'requests' con HTML_len muy bajo suele indicar que la PLP/PDP se renderiza por JS y conviene Playwright.")
        st.dataframe([{k: r.get(k, "") for k in ("SKU", "Modo", "HTML_len", "URL")} for r in results], use_container_width=True)
