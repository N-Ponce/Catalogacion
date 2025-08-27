import re
import time
import csv
import io
import random
from typing import List, Dict, Optional

import streamlit as st
import requests
from bs4 import BeautifulSoup

# ===== Config =====
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

def get_html_with_playwright(url: str, wait_selector: Optional[str] = None) -> Optional[str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=35000)
            sel = wait_selector or "li.breadcrumbs"
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

def extract_breadcrumb_from_html(html: str) -> List[str]:
    """
    Extrae los niveles del breadcrumb para simple.ripley.cl
    Busca el <li class="breadcrumbs"> y luego los <a class="breadcrumb"> con <span>.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("li", class_="breadcrumbs")
    if root:
        crumbs = []
        for a in root.find_all("a", class_="breadcrumb"):
            span = a.find("span")
            if span:
                text = span.get_text(strip=True)
                if text:
                    crumbs.append(text)
        return crumbs
    return []

def best_effort_pdp_for_sku(sku: str, use_playwright: bool, session: requests.Session) -> tuple:
    """
    Busca el SKU en simple.ripley.cl y retorna el breadcrumb desde la PDP
    """
    search_url = DOMAIN.rstrip("/") + SEARCH_PATH.format(q=sku)
    r = session_get(search_url, session=session)
    if r and r.status_code == 200:
        html = r.text
        crumbs = extract_breadcrumb_from_html(html)
        if crumbs:
            return r.url, html, crumbs, "requests"
        if use_playwright:
            html_play = get_html_with_playwright(search_url)
            if html_play:
                crumbs_play = extract_breadcrumb_from_html(html_play)
                if crumbs_play:
                    return search_url, html_play, crumbs_play, "playwright"
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
st.caption("Pega SKUs, abrimos búsqueda → PDP y leemos breadcrumb. Regla: ≥2 niveles y sin 'Otros/Misceláneos/Var.*' = Catalogado.")

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
    if no_list:
        st.download_button("Descargar CSV (todos)", data=to_csv(results), file_name="catalogacion_simple_ripley.csv", mime="text/csv")
        st.download_button("Descargar SOLO no catalogados (CSV)", data=to_csv([r for r in results if r["Catalogado"] != "Sí"]), file_name="no_catalogados.csv", mime="text/csv")
        st.text_area("SKUs NO catalogados (copiar/pegar)", value="\n".join(no_list), height=120)
    else:
        st.info("🎉 No se encontraron SKUs no catalogados.")

    with st.expander("Diagnóstico (avanzado)"):
        st.write("Si algo marcó 'No' por error, revisa 'Modo' y 'HTML_len': si 'requests' y HTML_len muy bajo → probablemente requería JS, usa Playwright.")
        st.dataframe([{k: r.get(k, "") for k in ("SKU", "Modo", "HTML_len", "URL")} for r in results], use_container_width=True)
