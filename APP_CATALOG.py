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


def extract_breadcrumb_from_html(html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")

    # 1) DOM típico
    selectors = [
        '[aria-label="breadcrumb"] li',
        'nav[aria-label="breadcrumb"] li',
        '.breadcrumb li',
        '.breadcrumbs li',
        'ol[aria-label="Breadcrumb"] li',
        'nav.breadcrumb li',
    ]
    for sel in selectors:
        els = soup.select(sel)
        crumbs = [e.get_text(strip=True) for e in els if e.get_text(strip=True)]
        crumbs = [c for c in crumbs if c != "/"]
        if crumbs:
            return crumbs

    # 2) JSON-LD BreadcrumbList
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
            if isinstance(blk, dict) and blk.get("@type") in ("BreadcrumbList", "ItemList"):
                items = blk.get("itemListElement") or []
                names = []
                for it in items:
                    if isinstance(it, dict):
                        name = it.get("name")
                        if not name and isinstance(it.get("item"), dict):
                            name = it["item"].get("name")
                        if name:
                            names.append(str(name).strip())
                if names:
                    return names

    # 3) Next.js __NEXT_DATA__ (por si almacenan breadcrumbs en estado)
    next_data = soup.find("script", id="__NEXT_DATA__")
    if next_data and next_data.string:
        try:
            j = json.loads(next_data.string)
            # Búsqueda recursiva de claves típicas
            def walk(x: Any, acc: List[str]):
                if isinstance(x, dict):
                    for k, v in x.items():
                        if k.lower() in ("breadcrumb", "breadcrumbs") and isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict):
                                    n = item.get("name") or item.get("label")
                                    if n:
                                        acc.append(str(n))
                                elif isinstance(item, str):
                                    acc.append(item)
                        else:
                            walk(v, acc)
                elif isinstance(x, list):
                    for i in x:
                        walk(i, acc)
            acc: List[str] = []
            walk(j, acc)
            if acc:
                # limpia duplicados manteniendo orden
                seen: set = set()
                uniq = []
                for c in acc:
                    if c not in seen:
                        seen.add(c)
                        uniq.append(c)
                return uniq
        except Exception:
            pass

    return []


def is_catalogado(crumbs: List[str]) -> bool:
    if len(crumbs) < 2:
        return False
    joined = " > ".join(crumbs)
    if MISC_PAT.search(joined):
        return False
    return True


def find_first_pdp_url_from_search(html: str, base_url: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")

    # Selectores más específicos para tarjetas de producto
    candidates = []

    # Enlaces con href que parezcan PDP
    for a in soup.find_all("a", href=True):
        href = a["href"]
        full = href if href.startswith("http") else base_url.rstrip("/") + "/" + href.lstrip("/")
        if any(x in full for x in ["/p/", "/product", "/producto", "/prod/"]):
            candidates.append(full)

    # Algunos frontends usan data-attributes en tarjetas
    for a in soup.select('a[data-product], a[data-testid*="product"], a[aria-label*="producto" i]'):
        href = a.get("href")
        if href:
            full = href if href.startswith("http") else base_url.rstrip("/") + "/" + href.lstrip("/")
            candidates.append(full)

    # Devolver el primero único
    for url in candidates:
        if url.startswith(base_url):
            return url
    return candidates[0] if candidates else None


def best_effort_pdp_via_requests(sku: str, base_url: str, session: requests.Session) -> Tuple[Optional[str], Optional[str]]:
    search_url = base_url.rstrip("/") + SEARCH_PATH.format(q=sku)
    r = session_get(search_url, session=session)
    if not r:
        return None, None

    # Si casualmente ya es PDP (algunas búsquedas redirigen)
    crumbs = extract_breadcrumb_from_html(r.text)
    if crumbs:
        return r.url, r.text

    pdp_url = find_first_pdp_url_from_search(r.text, base_url)
    if not pdp_url:
        return None, None

    p = session_get(pdp_url, session=session)
    if not p:
        return None, None

    return p.url, p.text


def get_html_with_playwright(url: str, wait_selector: Optional[str] = None) -> Optional[str]:
    try:
        _install_playwright_once()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ])
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                locale="es-CL",
                extra_http_headers=HEADERS,
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=35000)
            # Espera heurística de breadcrumb o título
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
                return search_url, html_search, "playwright(search->pdp)"  # cayó directo
            pdp_url = find_first_pdp_url_from_search(html_search, base_url)
            if pdp_url:
                html_pdp = get_html_with_playwright(pdp_url)
                if html_pdp:
                    return pdp_url, html_pdp, "playwright(pdp)"
    return None, None, "none"


def analyze_sku(sku: str, base_url: str, use_playwright: bool) -> Dict[str, str]:
    sess = requests.Session()
    # cookies iniciales (mejoran compatibilidad, opcional)
    sess.headers.update(HEADERS)

    for cand in candidate_skus(sku):
        url, html, mode = best_effort_pdp_for_sku(cand, base_url, use_playwright, sess)
        if html:
            crumbs = extract_breadcrumb_from_html(html)
            joined = " > ".join(crumbs) if crumbs else ""
            ok = is_catalogado(crumbs)
            return {
                "SKU": sku,
                "URL": url or "",
                "Breadcrumb": joined,
                "Catalogado": "Sí" if ok else "No",
                "Observación": "" if ok else ("Sin breadcrumb/1 nivel/Misceláneos" if crumbs else "No se detectó breadcrumb"),
                "Modo": mode,
                "HTML_len": str(len(html) if isinstance(html, str) else 0),
            }
        sleep_jitter()

    return {
        "SKU": sku,
        "URL": "",
        "Breadcrumb": "",
        "Catalogado": "No",
        "Observación": "No encontrado en búsqueda",
        "Modo": "none",
        "HTML_len": "0",
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

