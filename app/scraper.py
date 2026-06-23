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
from urllib.parse import quote

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


def _preco_via_browser(url: str, seletor: str | None = None) -> float | None:
    """Renderiza a página num navegador real e extrai o preço. Funciona em sites 100% JS
    e protegidos. Requer Chromium instalado (veja DEPLOY_SCRAPER.md)."""
    from playwright.sync_api import sync_playwright  # import tardio
    from .config import settings

    with sync_playwright() as p:
        browser = p.chromium.launch(**_browser_launch_kwargs())
        try:
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"], locale="pt-BR",
                                      viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=settings.scraper_timeout_ms)
            page.wait_for_timeout(2500)
            # 1) seletor explícito (ex.: ML), se passado
            if seletor:
                try:
                    page.wait_for_selector(seletor, timeout=5000)
                    v = _parse_preco(page.inner_text(seletor))
                    if v:
                        return v
                except Exception:  # noqa: BLE001
                    pass
            # 2) extração genérica do HTML já renderizado (JSON-LD -> meta -> seletores)
            return _extrair_preco_html(page.content())
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


# --------------------------------------------------------------------------- #
# CANAIS MONITORÁVEIS — o que cada marketplace deixa enxergar, de verdade.
#   descoberta_termo: 'nativo' (busca pública leve) | 'proprio' (nosso scraper headless)
#   rastreio_url: dá pra acompanhar a URL de um concorrente no Radar (snapshots)
#   dados_proprios: como você lê SEUS dados (preço/estoque/venda) naquele canal
# --------------------------------------------------------------------------- #
CANAIS_MONITORAVEIS = {
    "mercado_livre": {
        "nome": "Mercado Livre", "descoberta_termo": "nativo", "rastreio_url": True,
        "dados_proprios": "api",
        "nota": "Busca pública de concorrentes funciona direto. Rastreio por URL no Radar. Seus dados pela API oficial.",
    },
    "shopee": {
        "nome": "Shopee", "descoberta_termo": "proprio", "rastreio_url": True,
        "dados_proprios": "api",
        "nota": "Seus dados pela API oficial (loja já conectada). Concorrentes: nosso scraper próprio busca por termo (API interna da Shopee via navegador) e o Radar rastreia por URL. IP de datacenter pode pedir proxy.",
    },
    "tiktok": {
        "nome": "TikTok Shop", "descoberta_termo": "proprio", "rastreio_url": True,
        "dados_proprios": "api_onboarding",
        "nota": "Seus dados pela TikTok Shop Partner API (requer cadastro de seller/app). Concorrentes: scraper próprio (mais difícil — proteção forte) e Radar por URL.",
    },
    "shein": {
        "nome": "Shein", "descoberta_termo": "proprio", "rastreio_url": True,
        "dados_proprios": "limitado",
        "nota": "Acesso de seller mais fechado. Concorrentes: scraper próprio (páginas muito protegidas, costuma exigir proxy) e Radar por URL.",
    },
}


def _navegador_disponivel() -> bool:
    from .config import settings
    if not settings.scraper_browser:
        return False
    try:
        import playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def capacidades() -> dict:
    """Matriz do que está disponível por canal — alimenta a tela e é a fonte da verdade."""
    nav = _navegador_disponivel()
    canais = []
    for canal, c in CANAIS_MONITORAVEIS.items():
        modo = c["descoberta_termo"]
        status = "ativo" if modo == "nativo" else ("ativo" if (modo == "proprio" and nav) else "sem_navegador")
        canais.append({"canal": canal, **c, "descoberta_status": status})
    return {"navegador_pronto": nav, "scraper": "proprio", "canais": canais}


def _browser_launch_kwargs() -> dict:
    from .config import settings
    kw = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox",
                                     "--disable-dev-shm-usage", "--disable-gpu",
                                     "--disable-blink-features=AutomationControlled"]}
    if settings.scraper_proxy:
        kw["proxy"] = {"server": settings.scraper_proxy}
    return kw


def _buscar_shopee(termo: str, limite: int = 20) -> list:
    """Concorrentes na Shopee BR pela API interna /api/v4/search, via navegador headless.
    O navegador resolve cookies/anti-bot; depois fazemos fetch da API no contexto da página."""
    from .config import settings
    from playwright.sync_api import sync_playwright
    api_url = ("https://shopee.com.br/api/v4/search/search_items"
               f"?by=relevancy&keyword={quote(termo)}&limit={int(limite)}"
               "&newest=0&order=desc&page_type=search&scenario=PAGE_GLOBAL_SEARCH&version=2")
    txt = None
    with sync_playwright() as p:
        browser = p.chromium.launch(**_browser_launch_kwargs())
        try:
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"], locale="pt-BR",
                                      viewport={"width": 1280, "height": 800})
            page = ctx.new_page()
            page.goto(f"https://shopee.com.br/search?keyword={quote(termo)}",
                      wait_until="domcontentloaded", timeout=settings.scraper_timeout_ms)
            page.wait_for_timeout(2500)  # deixa cookies/anti-bot assentarem
            txt = page.evaluate(
                """async (u) => {
                    try {
                        const r = await fetch(u, {headers: {'x-api-source':'pc','x-shopee-language':'pt-BR'}, credentials:'include'});
                        return await r.text();
                    } catch (e) { return null; }
                }""", api_url)
        finally:
            browser.close()
    if not txt:
        return []
    try:
        data = json.loads(txt)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in (data.get("items") or [])[:limite]:
        b = it.get("item_basic") or it
        preco_raw = b.get("price")
        preco = round(preco_raw / 100000.0, 2) if preco_raw else None
        shopid, itemid = b.get("shopid"), b.get("itemid")
        out.append({"nome": b.get("name"), "preco": preco,
                    "vendas": b.get("historical_sold") or b.get("sold"),
                    "link": f"https://shopee.com.br/product/{shopid}/{itemid}" if shopid and itemid else None})
    return out


_PRECO_RE = re.compile(r"R\$\s*([\d.]+,\d{2})")


def _buscar_dom_generico(url: str, limite: int = 20) -> list:
    """Scraper genérico por DOM: abre a página e extrai âncoras com preço (R$ ...).
    Best-effort — usado para Shein/TikTok (experimental; o DOM muda e pode pedir proxy)."""
    from .config import settings
    from playwright.sync_api import sync_playwright
    achados = []
    with sync_playwright() as p:
        browser = p.chromium.launch(**_browser_launch_kwargs())
        try:
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"], locale="pt-BR",
                                      viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=settings.scraper_timeout_ms)
            page.wait_for_timeout(3500)
            for _ in range(3):  # rola para carregar mais cards (lazy load)
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)
            achados = page.evaluate(
                """() => {
                    const out = [], seen = new Set();
                    const re = /R\\$\\s*([\\d.]+,\\d{2})/;
                    document.querySelectorAll('a[href]').forEach(a => {
                        const t = (a.innerText||'').trim();
                        const m = t.match(re);
                        if (!m) return;
                        const nome = t.replace(re,'').replace(/\\s+/g,' ').trim().slice(0,120);
                        if (!nome || seen.has(a.href)) return;
                        seen.add(a.href);
                        out.push({nome, precoTxt: m[1], link: a.href});
                    });
                    return out.slice(0, 60);
                }""")
        finally:
            browser.close()
    out = []
    for x in (achados or [])[:limite]:
        out.append({"nome": x.get("nome"), "preco": _parse_preco(x.get("precoTxt")),
                    "vendas": None, "link": x.get("link")})
    return out


def _buscar_proprio(canal: str, termo: str, limite: int = 20) -> list | None:
    """Descoberta de concorrentes por termo com NOSSO scraper (sem terceiros).
    Retorna None se o navegador não estiver disponível no deploy."""
    if not _navegador_disponivel():
        return None
    try:
        if canal == "shopee":
            return _buscar_shopee(termo, limite)
        if canal == "shein":
            return _buscar_dom_generico(f"https://www.shein.com.br/pdsearch/{quote(termo)}/", limite)
        if canal == "tiktok":
            return _buscar_dom_generico(f"https://www.tiktok.com/search/shop?q={quote(termo)}", limite)
    except Exception:  # noqa: BLE001 — bloqueio/timeout/anti-bot: devolve vazio (não derruba)
        return []
    return []


# --------------------------------------------------------------------------- #
# PREÇO DE UMA URL (usado pelo Radar) — ciente do canal.
# --------------------------------------------------------------------------- #
_SHOPEE_IDS_RE = re.compile(r"i\.(\d+)\.(\d+)|/product/(\d+)/(\d+)")


def _ids_shopee(url: str):
    m = _SHOPEE_IDS_RE.search(url or "")
    if not m:
        return None
    return (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))


def _preco_shopee_url(url: str) -> float | None:
    """Preço de um anúncio Shopee pela API interna do item, via navegador (resolve anti-bot)."""
    ids = _ids_shopee(url)
    if not ids:
        return None
    shopid, itemid = ids
    api = f"https://shopee.com.br/api/v4/item/get?itemid={itemid}&shopid={shopid}"
    from playwright.sync_api import sync_playwright
    from .config import settings
    txt = None
    with sync_playwright() as p:
        browser = p.chromium.launch(**_browser_launch_kwargs())
        try:
            ctx = browser.new_context(user_agent=_HEADERS["User-Agent"], locale="pt-BR")
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=settings.scraper_timeout_ms)
            page.wait_for_timeout(2000)
            txt = page.evaluate(
                """async (u) => { try {
                    const r = await fetch(u, {headers:{'x-api-source':'pc','x-shopee-language':'pt-BR'}, credentials:'include'});
                    return await r.text();
                } catch (e) { return null; } }""", api)
        finally:
            browser.close()
    if not txt:
        return None
    try:
        data = json.loads(txt)
        item = data.get("data") or data.get("item") or {}
        if item.get("price"):
            return round(item["price"] / 100000.0, 2)
        precos = [m.get("price") / 100000.0 for m in (item.get("models") or []) if m.get("price")]
        return round(min(precos), 2) if precos else None
    except Exception:  # noqa: BLE001
        return None


def preco_de_url(url: str, marketplace: str | None = None) -> dict:
    """Extrai o preço de UMA URL de concorrente, ciente do canal. Para o Radar.
    Retorna {preco, fonte, erro?}. Usa navegador para Shopee/Shein/sites protegidos."""
    mk = (marketplace or "").lower()
    url = url or ""
    # Shopee: API interna do item (mais confiável)
    if "shopee" in mk or "shopee.com" in url:
        if _navegador_disponivel():
            try:
                v = _preco_shopee_url(url)
                if v is not None:
                    return {"preco": v, "fonte": "shopee_api"}
            except Exception as e:  # noqa: BLE001
                return {"preco": None, "fonte": "shopee_api", "erro": str(e)}
    # Mercado Livre / genérico leve
    v = _preco_via_ml_api(url)
    if v is not None:
        return {"preco": v, "fonte": "api_ml"}
    v = _preco_via_http(url)
    if v is not None:
        return {"preco": v, "fonte": "html"}
    # Navegador para o resto (Shein/TikTok/qualquer página JS protegida)
    if _navegador_disponivel():
        try:
            v = _preco_via_browser(url, None)
            if v is not None:
                return {"preco": v, "fonte": "browser"}
        except Exception as e:  # noqa: BLE001
            return {"preco": None, "fonte": "browser", "erro": str(e)}
    return {"preco": None, "fonte": "html", "erro": "Preço não encontrado ou site bloqueou (tente um proxy)."}


def posicionamento(nome: str, meu_preco: float, canal: str = "mercado_livre", limite: int = 30) -> dict:
    """Descoberta de concorrentes por termo, ciente do canal.
    ML: busca pública. Shopee/TikTok/Shein: NOSSO scraper headless (sem terceiros).
    Se não houver navegador no deploy, devolve o estado real (rastreio por URL no Radar segue)."""
    termo = _termo_busca(nome)
    canal = canal or "mercado_livre"
    if canal == "mercado_livre":
        anuncios = buscar_mercadolivre(termo, limite)
    elif canal in ("shopee", "tiktok", "shein"):
        via = _buscar_proprio(canal, termo, min(limite, 20))
        if via is None:
            meta = CANAIS_MONITORAVEIS.get(canal, {})
            return {
                "canal": canal, "nome_canal": meta.get("nome", canal.title()), "termo": termo,
                "modo": "sem_navegador", "rastreio_url": True,
                "motivo": "O navegador headless (Chromium) não está disponível neste deploy.",
                "como_ativar": ("Suba o backend com o Chromium instalado (nixpacks.toml já incluso roda "
                                "'playwright install chromium' no build) — aí o scraper próprio busca "
                                "concorrentes por termo. Enquanto isso, cadastre a URL do concorrente no Radar."),
            }
        anuncios = via
        if not anuncios:  # navegador ok, mas sem resultados (bloqueio/anti-bot ou nada encontrado)
            meta = CANAIS_MONITORAVEIS.get(canal, {})
            return {
                "canal": canal, "nome_canal": meta.get("nome", canal.title()), "termo": termo,
                "modo": "vazio", "rastreio_url": True,
                "motivo": f"Não consegui resultados na {meta.get('nome', canal)} para “{termo}”. "
                          "Pode ser bloqueio de anti-bot (IP de datacenter) ou nada encontrado.",
                "como_ativar": "Se persistir, configure SCRAPER_PROXY (proxy residencial) — datacenter costuma ser barrado.",
            }
    else:
        anuncios = []
    precos = [a["preco"] for a in anuncios if a.get("preco")]
    return {
        "canal": canal, "termo": termo, "meu_preco": round(float(meu_preco or 0), 2),
        "link_sugerido": anuncios[0]["link"] if anuncios else None,
        "posicionamento": posicionar(meu_preco, precos),
        "concorrentes": anuncios[:8],
    }
