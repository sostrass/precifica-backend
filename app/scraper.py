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


def preco_ml_por_id(mlb_id: str) -> dict | None:
    """Preço/status ao vivo de um anúncio do Mercado Livre pelo ID (ex.: 'MLB4525774643')."""
    digitos = re.sub(r"\D", "", str(mlb_id or ""))
    if not digitos:
        return None
    try:
        r = requests.get(f"https://api.mercadolibre.com/items/MLB{digitos}", timeout=20)
        if r.status_code != 200:
            return None
        d = r.json()
        return {"preco": float(d["price"]) if d.get("price") is not None else None,
                "status": d.get("status"), "estoque": d.get("available_quantity"),
                "vendidos": d.get("sold_quantity"), "link": d.get("permalink")}
    except (requests.RequestException, ValueError, KeyError):
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


# --------------------------------------------------------------------------- #
# VARREDURA DE POSICIONAMENTO — acha o produto no marketplace pela descrição,
# coleta os preços dos concorrentes e classifica onde seu preço cai.
# Mercado Livre: API pública de busca. Shopee/Amazon: stub honesto (exigem
# API/credencial do seller). Nada é alterado aqui — só leitura.
# --------------------------------------------------------------------------- #
import statistics

_DIM_RE = re.compile(r"\b\d+(?:[.,]\d+)?(?:\s*[x×]\s*\d+(?:[.,]\d+)?)+\s*(?:cm|mm|m)?\b", re.IGNORECASE)
_UNIT_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:cm|mm|kg|g|ml|l)\b", re.IGNORECASE)


def _termo_busca(nome: str) -> str:
    """Limpa medidas/ruído do nome para uma busca mais relevante (mantém números úteis, ex.: '30 caixas')."""
    t = _DIM_RE.sub(" ", nome or "")    # clusters tipo 22.5x17.5x6cm
    t = _UNIT_RE.sub(" ", t)            # número+unidade soltos (6cm, 0.3kg)
    t = re.sub(r"\s+", " ", t).strip()
    return (t or (nome or "")).strip()


def buscar_mercadolivre(termo: str, limite: int = 30) -> list[dict]:
    """Anúncios concorrentes no Mercado Livre (API pública de busca, sem auth)."""
    if not termo:
        return []
    try:
        r = requests.get("https://api.mercadolibre.com/sites/MLB/search",
                         params={"q": termo, "limit": min(int(limite), 50)},
                         headers=_HEADERS, timeout=20)
        r.raise_for_status()
        dados = r.json()
    except (requests.RequestException, ValueError):
        return []
    out = []
    for it in dados.get("results", []) or []:
        seller = it.get("seller") or {}
        out.append({
            "titulo": it.get("title"),
            "preco": it.get("price"),
            "link": it.get("permalink"),
            "vendedor": seller.get("nickname") or seller.get("id"),
            "vendidos": it.get("sold_quantity"),
            "thumb": it.get("thumbnail"),
        })
    return out


def posicionar(meu_preco: float, precos: list) -> dict:
    """Classifica meu preço frente aos concorrentes (min, mediana, max, percentil)."""
    validos = sorted(float(p) for p in precos if p and float(p) > 0)
    if not validos:
        return {"posicao": "sem_dados", "concorrentes": 0}
    mn, mx, med, n = validos[0], validos[-1], statistics.median(validos), len(validos)
    mp = float(meu_preco or 0)
    mais_baratos = sum(1 for p in validos if p < mp)
    percentil = round(mais_baratos / n * 100)
    if mp <= 0:
        posicao = "sem_preco"
    elif mp < mn:
        posicao = "mais_barato"
    elif mp > mx:
        posicao = "acima_mercado"
    elif mp > med * 1.03:
        posicao = "acima_media"
    else:
        posicao = "competitivo"
    return {
        "posicao": posicao, "meu_preco": round(mp, 2),
        "min": round(mn, 2), "mediana": round(med, 2), "max": round(mx, 2),
        "concorrentes": n, "percentil_mais_barato_que_voce": percentil,
        "dif_mediana_pct": round((mp - med) / med * 100, 1) if med else 0.0,
    }


def posicionamento(nome: str, meu_preco: float, canal: str = "mercado_livre", limite: int = 30) -> dict:
    """Varredura completa: termo -> busca no canal -> posicionamento + link sugerido."""
    termo = _termo_busca(nome)
    if canal == "mercado_livre":
        anuncios = buscar_mercadolivre(termo, limite)
    elif canal in ("shopee", "amazon"):
        return {"canal": canal, "termo": termo, "indisponivel": True,
                "motivo": f"Busca no {canal.title()} exige API/credencial do seller — adapter ainda é stub."}
    else:
        anuncios = []
    precos = [a["preco"] for a in anuncios if a.get("preco")]
    return {
        "canal": canal, "termo": termo, "meu_preco": round(float(meu_preco or 0), 2),
        "link_sugerido": anuncios[0]["link"] if anuncios else None,
        "posicionamento": posicionar(meu_preco, precos),
        "concorrentes": anuncios[:8],
    }
