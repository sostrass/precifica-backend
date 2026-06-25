"""Integração com a API v3 do Bling — agora multi-tenant (token por usuário).

Fatos oficiais (developer.bling.com.br):
- Base: https://api.bling.com.br/Api/v3
- OAuth 2.0 authorization_code -> POST /oauth/token (o 'code' expira em ~1 min)
- Renovação via refresh_token
- Limites: 3 req/s e 120.000/dia
- Listagem traz dados resumidos; detalhe completo via GET individual
"""

import base64
import secrets
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import jwt
import requests

from .config import settings
from .db import SessionLocal
from .models import OAuthToken, OAuthState

API_BASE = "https://api.bling.com.br/Api/v3"
AUTHORIZE_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
TOKEN_URL = f"{API_BASE}/oauth/token"


class BlingAuthError(Exception):
    pass


class BlingNotFound(Exception):
    """Recurso não encontrado no Bling (HTTP 404)."""
    pass


class BlingError(Exception):
    """Erro de API do Bling com a mensagem detalhada (validação de schema etc.)."""
    pass


# --------------------------------------------------------------------------- #
# Rate limiter global (3 req/s do Bling). Em multi-instância, mover p/ Redis.
# --------------------------------------------------------------------------- #
class _RateLimiter:
    def __init__(self, rate_per_sec: int = 3):
        self.min_interval = 1.0 / rate_per_sec
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


_limiter = _RateLimiter(rate_per_sec=3)


def _basic_auth_header() -> str:
    raw = f"{settings.bling_client_id}:{settings.bling_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# --------------------------------------------------------------------------- #
# OAuth — o 'state' (anti-CSRF) é guardado no BANCO: token curto, uso único,
# TTL de 30 min. Imune a redeploy e a troca de JWT_SECRET (não depende deles).
# --------------------------------------------------------------------------- #
STATE_TTL_MIN = 30


def get_authorize_url(user_id: int) -> str:
    state = secrets.token_urlsafe(24)
    with SessionLocal() as db:
        db.add(OAuthState(state=state, user_id=int(user_id)))
        db.commit()
    params = {"response_type": "code", "client_id": settings.bling_client_id, "state": state}
    if settings.bling_redirect_uri:
        params["redirect_uri"] = settings.bling_redirect_uri
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def resolver_state(state: str):
    """Identifica o tenant a partir do state do callback.

    O ideal é casar o state exato (CSRF). Mas o fluxo de consentimento do Bling
    NÃO preserva o state que enviamos — devolve um próprio. Então, se o exato não
    bater, caímos para a única conexão pendente recente (inequívoca). Devolve
    (user_id, state_para_consumir).
    """
    agora = datetime.utcnow()
    limite = agora - timedelta(minutes=STATE_TTL_MIN)
    with SessionLocal() as db:
        row = db.get(OAuthState, state) if state else None
        origem = "exato"
        if row is None:
            recentes = (db.query(OAuthState)
                        .filter(OAuthState.criado_em >= limite)
                        .order_by(OAuthState.criado_em.desc()).all())
            if len(recentes) == 1:
                row = recentes[0]      # único pendente: o Bling trocou o state, mas é este
                origem = "fallback"
            elif len(recentes) > 1:
                raise BlingAuthError("Várias conexões pendentes. Tente conectar novamente (uma de cada vez).")
        if row is None:
            raise BlingAuthError("State inválido ou expirado. Clique em Conectar Bling no app e conclua em até 30 min.")
        idade = agora - (row.criado_em or agora)
        uid, st = int(row.user_id), row.state
    if idade > timedelta(minutes=STATE_TTL_MIN):
        consume_state(st)
        raise BlingAuthError("State expirado. Tente conectar novamente.")
    return uid, st


def user_id_from_state(state: str) -> int:
    """Compat: valida e devolve só o user_id (sem consumir)."""
    uid, _ = resolver_state(state)
    return uid


def consume_state(state: str) -> None:
    """Apaga o state (uso único) — só depois que a troca do code deu certo."""
    with SessionLocal() as db:
        row = db.get(OAuthState, state)
        if row is not None:
            db.delete(row)
            db.commit()


def _save_token(user_id: int, data: dict):
    expires_in = int(data.get("expires_in", 21600))  # 6h padrão do Bling
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None:
            tok = OAuthToken(user_id=user_id)
            db.add(tok)
        tok.access_token = data["access_token"]
        novo_refresh = data.get("refresh_token")
        if novo_refresh:
            tok.refresh_token = novo_refresh
        elif not tok.refresh_token:
            tok.refresh_token = ""
        tok.expires_at = expires_at
        db.commit()


def exchange_code(user_id: int, code: str):
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "1.0",
    }
    r = requests.post(TOKEN_URL, headers=headers,
                      data={"grant_type": "authorization_code", "code": code}, timeout=30)
    if r.status_code != 200:
        raise BlingAuthError(f"Falha ao trocar o code ({r.status_code}): {r.text}")
    _save_token(user_id, r.json())


def refresh_token(user_id: int):
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None or not tok.refresh_token:
            raise BlingAuthError("Sem refresh_token. Refaça a autorização do Bling.")
        rt = tok.refresh_token
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "1.0",
    }
    r = requests.post(TOKEN_URL, headers=headers,
                      data={"grant_type": "refresh_token", "refresh_token": rt}, timeout=30)
    if r.status_code != 200:
        raise BlingAuthError(f"Falha no refresh ({r.status_code}): {r.text}")
    _save_token(user_id, r.json())


def _access_token(user_id: int) -> str:
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None:
            raise BlingAuthError("Conta Bling ainda não autorizada para este usuário.")
        expirado = tok.expires_at <= datetime.utcnow()
        at = tok.access_token
    if expirado:
        refresh_token(user_id)
        with SessionLocal() as db:
            at = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first().access_token
    return at


def token_status(user_id: int) -> dict:
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None:
            return {"autorizado": False}
        return {
            "autorizado": True,
            "expira_em": tok.expires_at.isoformat() + "Z",
            "expirado": tok.expires_at <= datetime.utcnow(),
        }


# --------------------------------------------------------------------------- #
# Requisições à API (sempre escopadas no token do usuário)
# --------------------------------------------------------------------------- #
def _request(user_id: int, method: str, path: str, **kwargs) -> requests.Response:
    _limiter.wait()
    url = f"{API_BASE}{path}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_access_token(user_id)}"
    headers.setdefault("Accept", "application/json")
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if r.status_code == 401:
        refresh_token(user_id)
        _limiter.wait()
        headers["Authorization"] = f"Bearer {_access_token(user_id)}"
        r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    return r


def listar_produtos(user_id: int, pagina: int = 1, limite: int = 100,
                    criterio: int | None = None) -> dict:
    params = {"pagina": pagina, "limite": limite}
    if criterio is not None:
        params["criterio"] = criterio
    r = _request(user_id, "GET", "/produtos", params=params)
    r.raise_for_status()
    return r.json()


def listar_todos_produtos(user_id: int, limite: int = 100, max_paginas: int = 300):
    """Gera todos os produtos do catálogo, página por página, até esgotar.
    Yield (pagina, lista_de_produtos) para o chamador gravar incrementalmente."""
    pagina = 1
    while pagina <= max_paginas:
        try:
            dados = (listar_produtos(user_id, pagina=pagina, limite=limite) or {}).get("data") or []
        except BlingAuthError:
            raise
        except requests.RequestException:
            break
        if not dados:
            break
        yield pagina, dados
        if len(dados) < limite:
            break
        pagina += 1
        time.sleep(0.34)  # respeita o rate limit do Bling (~3 req/s)


def obter_produto(user_id: int, produto_id: int, id_loja=None) -> dict:
    params = {"idLoja": id_loja} if id_loja else None
    r = _request(user_id, "GET", f"/produtos/{produto_id}", params=params)
    r.raise_for_status()
    return r.json()


def probe_multiloja(user_id: int, produto_id: int, lojas: list) -> dict:
    """Testa se a API pública expõe preço por canal via ?idLoja=. Para cada loja, lê o produto
    naquele contexto e compara o preço com a base. Se diferir, achamos a fonte por canal."""
    base = (obter_produto(user_id, produto_id) or {}).get("data", {}) or {}
    base_preco = _preco_br(base.get("preco")) if isinstance(base.get("preco"), str) else float(base.get("preco") or 0)
    resultados = []
    for lj in lojas:
        item = {"id_loja": lj, "ok": False}
        try:
            d = (obter_produto(user_id, produto_id, id_loja=lj) or {}).get("data", {}) or {}
            p = _preco_br(d.get("preco")) if isinstance(d.get("preco"), str) else float(d.get("preco") or 0)
            item.update({
                "ok": True, "preco": p, "difere_da_base": abs(p - base_preco) > 0.001,
                "tem_loja": "loja" in d or "vinculo" in d or "idProdutoLoja" in d,
                "campos_extras": [k for k in d.keys() if k not in base.keys()][:10],
            })
        except Exception as e:
            item["erro"] = str(e)[:120]
        resultados.append(item)
    achou = any(r.get("difere_da_base") for r in resultados)
    return {"produto_id": produto_id, "base_preco": round(base_preco, 2),
            "expoe_preco_por_canal": achou, "lojas": resultados}


def atualizar_preco(user_id: int, produto_id: int, preco: float) -> dict:
    """PATCH parcial só do preço. Se a conta exigir PUT completo, ver README."""
    r = _request(user_id, "PATCH", f"/produtos/{produto_id}",
                 json={"preco": round(float(preco), 2)})
    r.raise_for_status()
    return r.json()


# Campos editáveis aceitos no PATCH parcial de produto (nomes da API v3 do Bling).
_CAMPOS_PRODUTO = {"nome", "preco", "precoCusto", "ncm", "pesoBruto",
                   "pesoLiquido", "descricaoCurta", "descricaoComplementar", "gtin"}


def atualizar_produto(user_id: int, produto_id: int, campos: dict) -> dict:
    """PATCH parcial dos campos editáveis do produto (só envia o que mudou)."""
    corpo = {k: v for k, v in campos.items() if k in _CAMPOS_PRODUTO and v is not None}
    for k in ("preco", "precoCusto", "pesoBruto", "pesoLiquido"):
        if k in corpo and corpo[k] != "":
            corpo[k] = round(float(corpo[k]), 3 if "peso" in k else 2)
    if not corpo:
        return {}
    r = _request(user_id, "PATCH", f"/produtos/{produto_id}", json=corpo)
    r.raise_for_status()
    return r.json()


def atualizar_preco_canal(user_id: int, produto_id: int, id_loja, preco: float) -> dict:
    """Grava o preço NO CANAL (loja) específico, sem mexer no preço-base.
    Usa o contexto da loja (idLoja) — o mesmo mecanismo da leitura por canal.
    Tenta o endpoint de produto-loja e, em fallback, o PATCH com idLoja."""
    preco = round(float(preco), 2)
    # 1) endpoint dedicado de produto-loja (quando a conta expõe)
    try:
        r = _request(user_id, "PATCH", f"/produtos/lojas/{produto_id}",
                     params={"idLoja": id_loja}, json={"preco": preco})
        if r.status_code < 400:
            return {"ok": True, "via": "produtos/lojas", "preco": preco}
    except requests.RequestException:
        pass
    # 2) fallback: PATCH do produto no contexto da loja
    r = _request(user_id, "PATCH", f"/produtos/{produto_id}",
                 params={"idLoja": id_loja}, json={"preco": preco})
    r.raise_for_status()
    return {"ok": True, "via": "produtos?idLoja", "preco": preco}


def listar_nfe(user_id: int, pagina: int = 1, limite: int = 100,
               situacao: int | None = None) -> dict:
    """Lista NF-e (resumidas). Filtra por situação quando informado (ex.: pendente)."""
    params = {"pagina": pagina, "limite": limite}
    if situacao is not None:
        params["situacao"] = situacao
    r = _request(user_id, "GET", "/nfe", params=params)
    r.raise_for_status()
    return r.json()


# Vínculos multiloja: o Bling guarda o preço POR CANAL em "vinculosLojas"
# (cada loja/marketplace com seu próprio preço). Mapa tipoIntegracao -> canal da config.
MAPA_INTEGRACAO = {
    "mercadolivre": "mercadolivre", "shopee": "shopee", "amazon": "amazon",
    "magalu": "magalu", "americanas": "americanas", "via": "via",
}


def _preco_br(v) -> float:
    """'38,54' -> 38.54 ; '1.234,50' -> 1234.50."""
    if v in (None, "", 0):
        return 0.0
    try:
        return float(str(v).replace(".", "").replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def parse_vinculos_multiloja(arr: list) -> list:
    """Normaliza vinculosLojas em [{idLoja, nome, integracao, canal, id_anuncio, preco, publicado, ...}]."""
    out = []
    for v in arr or []:
        integ = (v.get("tipoIntegracao") or "").strip()
        preco = _preco_br(v.get("preco"))
        id_anuncio = v.get("idProdutoLoja") or None
        if id_anuncio in (0, "0"):
            id_anuncio = None
        ad = str(v.get("adStatus") or "").strip()
        out.append({
            "id_loja": v.get("idLoja"),
            "nome": v.get("nomeLoja") or integ,
            "integracao": integ,
            "canal": MAPA_INTEGRACAO.get(integ.lower()),
            "id_anuncio": id_anuncio,
            "preco": preco,
            "preco_promocional": _preco_br(v.get("precoPromocional")),
            "link": v.get("linkExterno") or None,
            "ad_status": ad or None,
            "publicado": bool(id_anuncio),          # tem anúncio nesse canal
            "ativo": bool(id_anuncio) and preco > 0,  # anúncio + preço = ativo de fato
        })
    return out


# Lojas desta conta (id -> canal), descobertas no painel. Usadas pra ler preço por idLoja.
LOJAS_CONTA = {
    "203414926": {"nome": "Mercado Livre", "integracao": "MercadoLivre"},
    "204884434": {"nome": "Shein", "integracao": "Shein"},
    "203923623": {"nome": "Shopee", "integracao": "Shopee"},
    "205946980": {"nome": "Shopee - NOVO", "integracao": "Shopee"},
    "205916963": {"nome": "TikTok Shop", "integracao": "TikTok"},
    "205693668": {"nome": "Nuvemshop", "integracao": "Nuvemshop"},
}


def vinculos_multiloja(user_id: int, produto_id) -> list:
    """Preço/status por canal. 1) tenta o payload do produto (vinculosLojas);
    2) fallback: lê por ?idLoja= (auto-validável — só inclui lojas com preço próprio ≠ base)."""
    try:
        raw = (obter_produto(user_id, produto_id) or {}).get("data", {}) or {}
    except BlingAuthError:
        return []
    for chave in ("vinculosLojas", "lojas", "produtosLojas"):
        if isinstance(raw.get(chave), list) and raw[chave]:
            return parse_vinculos_multiloja(raw[chave])
    # fallback por idLoja
    base_preco = _preco_br(raw.get("preco")) if isinstance(raw.get("preco"), str) else float(raw.get("preco") or 0)
    out = []
    for lj, meta in lojas_da_conta(user_id).items():
        try:
            d = (obter_produto(user_id, produto_id, id_loja=lj) or {}).get("data", {}) or {}
        except Exception:
            continue
        p = _preco_br(d.get("preco")) if isinstance(d.get("preco"), str) else float(d.get("preco") or 0)
        if p > 0 and abs(p - base_preco) > 0.001:  # preço próprio do canal
            integ = meta["integracao"]
            out.append({"id_loja": lj, "nome": meta["nome"], "integracao": integ,
                        "canal": MAPA_INTEGRACAO.get(integ.lower()), "id_anuncio": meta.get("id_anuncio"),
                        "preco": p, "preco_promocional": 0.0, "link": None,
                        "ad_status": None, "publicado": True, "ativo": True})
    return out


_CACHE_LOJAS = {}  # user_id -> (timestamp, {id_loja: meta})


def descobrir_lojas(user_id: int) -> dict:
    """Tenta listar as lojas/canais da conta pela API pública. Best-effort: testa
    endpoints candidatos e devolve {id_loja: {nome, integracao}} ou {} se nenhum existir."""
    for path in ("/canais-de-venda", "/lojas", "/produtos/lojas"):
        try:
            r = _request(user_id, "GET", path, params={"limite": 100})
            if r.status_code >= 400:
                continue
            dados = (r.json() or {}).get("data") or []
            achadas = {}
            for it in dados if isinstance(dados, list) else []:
                idl = str(it.get("id") or it.get("idLoja") or "")
                if not idl:
                    continue
                achadas[idl] = {"nome": it.get("nome") or it.get("descricao") or f"Loja {idl}",
                                "integracao": it.get("tipoIntegracao") or it.get("tipo") or ""}
            if achadas:
                return achadas
        except (requests.RequestException, ValueError):
            continue
    return {}


def lojas_da_conta(user_id: int) -> dict:
    """Lojas da conta para leitura por canal: descobertas (cache 1h) ou as conhecidas."""
    import time as _t
    c = _CACHE_LOJAS.get(user_id)
    if c and _t.time() - c[0] < 3600:
        return c[1] or LOJAS_CONTA
    achadas = descobrir_lojas(user_id)
    _CACHE_LOJAS[user_id] = (_t.time(), achadas)
    return achadas or LOJAS_CONTA


def listar_tabelas_precos(user_id: int) -> dict:
    """Tabelas de preço do Bling — onde, normalmente, ficam os preços por canal."""
    r = _request(user_id, "GET", "/tabelas-de-precos")
    r.raise_for_status()
    return r.json()


def listar_pedidos(user_id: int, pagina: int = 1, limite: int = 100,
                   data_inicial: str | None = None, data_final: str | None = None) -> dict:
    params = {"pagina": pagina, "limite": limite}
    if data_inicial:
        params["dataInicial"] = data_inicial
    if data_final:
        params["dataFinal"] = data_final
    r = _request(user_id, "GET", "/pedidos/vendas", params=params)
    r.raise_for_status()
    return r.json()


def obter_pedido(user_id: int, pedido_id) -> dict:
    r = _request(user_id, "GET", f"/pedidos/vendas/{pedido_id}")
    r.raise_for_status()
    return r.json()


def listar_pedidos_periodo(user_id: int, dias: int = 30, max_paginas: int = 8) -> list:
    """Todos os pedidos de venda dos últimos N dias (pagina até esvaziar)."""
    from datetime import date, timedelta
    fim = date.today()
    ini = fim - timedelta(days=dias)
    todos = []
    for p in range(1, max_paginas + 1):
        data = listar_pedidos(user_id, pagina=p, limite=100,
                              data_inicial=ini.isoformat(), data_final=fim.isoformat())
        lote = data.get("data", []) or []
        todos.extend(lote)
        if len(lote) < 100:
            break
    return todos


def obter_nfe(user_id: int, nfe_id) -> dict:
    """Detalhe completo de uma NF-e (com itens e transporte)."""
    r = _request(user_id, "GET", f"/nfe/{nfe_id}")
    if r.status_code == 404:
        raise BlingNotFound(
            f"NF-e {nfe_id} não encontrada no Bling (404). O ID pode ser de outro recurso "
            "— alguns eventos do Bling trazem o ID do pedido, não o da nota — ou a nota foi removida.")
    r.raise_for_status()
    return r.json()


def _resumir_erro_bling(corpo: dict) -> str:
    """Extrai uma mensagem legível do corpo de erro do Bling v3."""
    if not isinstance(corpo, dict):
        return str(corpo)[:300]
    err = corpo.get("error") or corpo
    partes = []
    msg = err.get("message") or err.get("description")
    if msg:
        partes.append(str(msg))
    campos = err.get("fields") or err.get("errors") or []
    if isinstance(campos, list):
        for f in campos[:6]:
            if isinstance(f, dict):
                el = f.get("element") or f.get("field") or f.get("name") or ""
                fmsg = f.get("msg") or f.get("message") or f.get("description") or ""
                partes.append(f"{el}: {fmsg}".strip(" :"))
            else:
                partes.append(str(f))
    return " | ".join(p for p in partes if p) or str(corpo)[:300]


def atualizar_nfe(user_id: int, nfe_id, payload: dict) -> dict:
    """Altera uma NF-e existente (PUT). Só funciona em nota Pendente/Rejeitada.

    O envio ao Sefaz é feito no painel/automação do Bling (certificado A1 lá), não aqui.
    Em erro, levanta BlingError com a mensagem EXATA do Bling (pra diagnosticar o schema).
    """
    r = _request(user_id, "PUT", f"/nfe/{nfe_id}", json=payload)
    if r.status_code == 404:
        raise BlingNotFound(f"NF-e {nfe_id} não encontrada no Bling (404) ao tentar alterar.")
    if r.status_code >= 400:
        try:
            corpo = r.json()
        except Exception:  # noqa: BLE001
            corpo = {"raw": (r.text or "")[:400]}
        raise BlingError(f"Bling recusou a alteração da NF-e {nfe_id} ({r.status_code}): {_resumir_erro_bling(corpo)}")
    return r.json()
