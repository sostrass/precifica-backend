"""Monitor de concorrência — leve (requests + BeautifulSoup), no estilo Cheerio/JSON-LD.

Ordem de tentativa por URL:
1. Mercado Livre: API pública /items/{id} (à prova de bloqueio).
2. HTTP + BeautifulSoup: JSON-LD (offers.price) -> meta tags -> seletores visuais.
   Cobre Nuvemshop, Shopify, Tray, Loja Integrada, ML, etc.
3. (opcional) Playwright: só para sites 100% renderizados em JS. Mantido para não
   perder capacidade, mas não é usado por padrão (precisa de navegador instalado).
"""

import json
import re
import time

import requests

_ML_ITEM_RE = re.compile(r"MLB-?(\d+)", re.IGNORECASE)
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _parse_preco(txt) -> float | None:
    """Texto BR ('R$ 1.234,56') ou número -> float. Trata separador de milhar."""
    if txt is None:
        return None
    s = str(txt).strip()
    if not s:
        return None
    # se já vier no padrão da API (ponto decimal, sem milhar), respeita
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    limpo = re.sub(r"[^0-9,.]", "", s).replace(".", "").replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return None


def _preco_via_ml_api(url: str) -> float | None:
    m = _ML_ITEM_RE.search(url)
    if not m:
        return None
    try:
        r = requests.get(f"https://api.mercadolibre.com/items/MLB{m.group(1)}", timeout=20)
        if r.status_code != 200:
            return None
        preco = r.json().get("price")
        return float(preco) if preco is not None else None
    except requests.RequestException:
        return None


def _preco_de_jsonld(soup) -> float | None:
    """O 'santo graal': structured data do Google Shopping (offers.price)."""
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidatos = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidatos = data["@graph"]
        for item in candidatos:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("price") is not None:
                p = _parse_preco(offers["price"])
                if p:
                    return p
    return None


def _extrair_preco_html(html: str) -> float | None:
    from bs4 import BeautifulSoup  # import tardio

    soup = BeautifulSoup(html, "lxml")
    preco = _preco_de_jsonld(soup)
    if preco:
        return preco
    # meta tags oficiais
    for attrs in ({"itemprop": "price"}, {"property": "product:price:amount"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            p = _parse_preco(meta["content"])
            if p:
                return p
    # seletores visuais (plano B)
    for sel in (".andes-money-amount__fraction", ".price", ".preco-por", "[itemprop=price]"):
        el = soup.select_one(sel)
        if el and el.get_text(strip=True):
            p = _parse_preco(el.get_text())
            if p:
                return p
    return None


def _preco_via_http(url: str) -> float | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=15)
        if r.status_code != 200:
            return None
        return _extrair_preco_html(r.text)
    except requests.RequestException:
        return None


def _preco_via_browser(url: str, seletor: str | None) -> float | None:
    """Último recurso para sites 100% JS. Requer `playwright install chromium`."""
    from playwright.sync_api import sync_playwright  # import tardio

    seletor = seletor or ".andes-money-amount__fraction"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            page = browser.new_page(user_agent=_HEADERS["User-Agent"])
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector(seletor, timeout=10000)
            return _parse_preco(page.inner_text(seletor))
        finally:
            browser.close()


def buscar_preco(url: str, seletor: str | None = None,
                 usar_browser: bool = False) -> dict:
    """Um concorrente. Retorna {preco: float|None, fonte: str, erro?: str}."""
    p = _preco_via_ml_api(url)
    if p is not None:
        return {"preco": p, "fonte": "api_ml"}
    p = _preco_via_http(url)
    if p is not None:
        return {"preco": p, "fonte": "html"}
    if usar_browser:
        try:
            p = _preco_via_browser(url, seletor)
            if p is not None:
                return {"preco": p, "fonte": "browser"}
        except Exception as e:  # noqa: BLE001
            return {"preco": None, "fonte": "browser", "erro": str(e)}
    return {"preco": None, "fonte": "html", "erro": "Preço não encontrado ou site bloqueou."}


def buscar_precos(urls: list[str], pausa_seg: float = 1.0) -> list[dict]:
    """Radar multi-concorrentes. Pausa entre sites para não parecer ataque."""
    resultados = []
    for i, url in enumerate(urls):
        achado = buscar_preco(url)
        achado["url"] = url
        resultados.append(achado)
        if i < len(urls) - 1 and pausa_seg:
            time.sleep(pausa_seg)
    return resultados
