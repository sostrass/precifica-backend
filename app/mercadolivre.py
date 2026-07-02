"""Integração direta com a API do Mercado Livre (api.mercadolibre.com) — nível Enterprise.

Espelha (e estende) o papel do módulo da Shopee. Multi-tenant:
  • Credenciais do APP (client_id/secret) vêm do ambiente: ML_CLIENT_ID, ML_CLIENT_SECRET.
  • Tokens da CONTA (refresh_token/seller_id) ficam por usuário em MLConta (banco).
  • Fallback single-tenant: se não houver linha em MLConta, usa ML_REFRESH_TOKEN/ML_SELLER_ID
    do ambiente — assim a configuração atual via Railway continua funcionando.

Toda função pública aceita `user_id` opcional. Sem ele (None), usa as credenciais do
ambiente (compatível com as chamadas antigas). Com user_id, usa a conta daquele tenant.

Domínios cobertos: conta/auth, catálogo/itens, preço+líquido (listing_prices), radar
(benchmarks), pedidos, envios+etiquetas, perguntas, avaliações, visitas/funil, promoções v2,
qualidade, cache+sync e processamento de webhooks. Site padrão: MLB (Brasil).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import jwt
import requests

from .config import settings
from .db import SessionLocal
from .models import MLConta, MLItemCache, MLSync

API = "https://api.mercadolibre.com"
AUTH = "https://auth.mercadolivre.com.br"
SITE = "MLB"
TIMEOUT = 25


class MLNaoConfigurado(RuntimeError):
    """Credenciais do Mercado Livre ausentes/incompletas."""


class MLErro(RuntimeError):
    """Falha em chamada à API do Mercado Livre."""


# cache do access_token em memória: {refresh_token[:12]: (expira_em_ts, token)}
_TOKENS: dict[str, tuple[float, str]] = {}


# =========================================================================== #
# Credenciais, conta e tokens
# =========================================================================== #
def _app() -> dict:
    return {"client_id": os.environ.get("ML_CLIENT_ID"),
            "client_secret": os.environ.get("ML_CLIENT_SECRET")}


def app_configurado() -> bool:
    a = _app()
    return bool(a["client_id"] and a["client_secret"])


def _conta(db, user_id):
    """Conta do tenant (DB) ou fallback do ambiente (single-tenant)."""
    if user_id is not None:
        c = db.query(MLConta).filter_by(user_id=user_id).first()
        if c and c.refresh_token:
            return c
    rt = os.environ.get("ML_REFRESH_TOKEN")
    if rt:
        return MLConta(user_id=user_id or 0, seller_id=os.environ.get("ML_SELLER_ID"),
                       refresh_token=rt, site_id=SITE)
    return None


def configurado(user_id=None) -> bool:
    if not app_configurado():
        return False
    if user_id is None:
        return bool(os.environ.get("ML_REFRESH_TOKEN"))
    db = SessionLocal()
    try:
        return _conta(db, user_id) is not None
    finally:
        db.close()


def status_conexao(user_id=None) -> dict:
    if not app_configurado():
        return {"app": False, "conta": False,
                "msg": "Faltam ML_CLIENT_ID e ML_CLIENT_SECRET no servidor."}
    db = SessionLocal()
    try:
        c = _conta(db, user_id)
        if not c:
            return {"app": True, "conta": False,
                    "msg": "App pronto. Falta conectar a conta (refresh_token)."}
        return {"app": True, "conta": True, "seller_id": c.seller_id, "nickname": c.nickname,
                "site_id": c.site_id or SITE,
                "expira_em": c.expira_em.isoformat() if c.expira_em else None}
    finally:
        db.close()


def url_autorizacao(redirect_uri: str, state: str | None = None) -> str:
    """URL pra iniciar o OAuth. Os escopos (read/write/offline_access) vêm da config do app."""
    a = _app()
    if not a["client_id"]:
        raise MLNaoConfigurado("ML_CLIENT_ID ausente")
    url = (f"{AUTH}/authorization?response_type=code"
           f"&client_id={a['client_id']}&redirect_uri={redirect_uri}")
    if state:
        url += f"&state={state}"
    return url


def state_token(user_id: int) -> str:
    """Token curto (10min) que carrega o user_id pelo redirect do OAuth (multi-tenant)."""
    payload = {"uid": user_id, "exp": datetime.utcnow() + timedelta(minutes=10), "scp": "ml_oauth"}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def ler_state(token: str):
    try:
        d = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return d.get("uid") if d.get("scp") == "ml_oauth" else None
    except jwt.PyJWTError:
        return None


def trocar_code_por_token(code: str, redirect_uri: str) -> dict:
    """Troca o 'code' do callback por access_token + refresh_token."""
    a = _app()
    if not (a["client_id"] and a["client_secret"]):
        raise MLNaoConfigurado("client_id/secret ausentes")
    r = requests.post(f"{API}/oauth/token", timeout=TIMEOUT, data={
        "grant_type": "authorization_code", "client_id": a["client_id"],
        "client_secret": a["client_secret"], "code": code, "redirect_uri": redirect_uri,
    })
    if r.status_code >= 400:
        raise MLErro(f"oauth: {r.status_code} {r.text[:200]}")
    return r.json()


def salvar_conta(user_id: int, refresh_token: str, access_token: str | None = None,
                 expires_in: int = 21600, seller_id=None, nickname=None, site_id: str = SITE):
    """Guarda/atualiza a conta ML de um tenant (usado no callback OAuth multi-tenant)."""
    db = SessionLocal()
    try:
        c = db.query(MLConta).filter_by(user_id=user_id).first()
        if not c:
            c = MLConta(user_id=user_id)
            db.add(c)
        c.refresh_token = refresh_token
        if access_token:
            c.access_token = access_token
        c.expira_em = datetime.utcnow() + timedelta(seconds=int(expires_in) - 300)
        if seller_id:
            c.seller_id = str(seller_id)
        if nickname:
            c.nickname = nickname
        c.site_id = site_id or SITE
        if not c.conectado_em:
            c.conectado_em = datetime.utcnow()
        c.ativo = True
        db.commit()
    finally:
        db.close()


def _salvar_token(user_id: int, access_token: str, refresh_token: str, expires_in):
    """Persiste o access_token renovado (e refresh rotativo) — só se já existe linha do tenant."""
    db = SessionLocal()
    try:
        c = db.query(MLConta).filter_by(user_id=user_id).first()
        if not c:
            return
        c.access_token = access_token
        c.refresh_token = refresh_token
        c.expira_em = datetime.utcnow() + timedelta(seconds=int(expires_in) - 300)
        db.commit()
    finally:
        db.close()


def conta_do_token(access_token: str) -> dict:
    """Lê /users/me com um access_token recém-obtido (callback do OAuth, antes do refresh no DB)."""
    r = requests.get(f"{API}/users/me", timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {access_token}"})
    if r.status_code >= 400:
        raise MLErro(f"GET /users/me: {r.status_code} {r.text[:200]}")
    return r.json()


def _access_token(user_id=None) -> str:
    """Renova (ou reusa) o access_token via refresh_token. Persiste no DB p/ tenant real."""
    if not app_configurado():
        raise MLNaoConfigurado("defina ML_CLIENT_ID e ML_CLIENT_SECRET")
    db = SessionLocal()
    try:
        c = _conta(db, user_id)
        if not c or not c.refresh_token:
            raise MLNaoConfigurado("conta Mercado Livre não conectada (refresh_token ausente)")
        refresh = c.refresh_token
    finally:
        db.close()
    chave = refresh[:12]
    cached = _TOKENS.get(chave)
    if cached and time.time() < cached[0] - 60:
        return cached[1]
    a = _app()
    r = requests.post(f"{API}/oauth/token", timeout=TIMEOUT, data={
        "grant_type": "refresh_token", "client_id": a["client_id"],
        "client_secret": a["client_secret"], "refresh_token": refresh,
    })
    if r.status_code >= 400:
        raise MLErro(f"refresh: {r.status_code} {r.text[:200]}")
    d = r.json()
    tok = d.get("access_token")
    novo_refresh = d.get("refresh_token") or refresh
    expira = time.time() + float(d.get("expires_in") or 21600)
    _TOKENS[chave] = (expira, tok)
    if novo_refresh != refresh:
        _TOKENS[novo_refresh[:12]] = (expira, tok)
    if user_id is not None:
        _salvar_token(user_id, tok, novo_refresh, d.get("expires_in") or 21600)
    return tok


def _seller_id(user_id=None) -> str:
    db = SessionLocal()
    try:
        c = _conta(db, user_id)
        sid = c.seller_id if c else None
    finally:
        db.close()
    if sid:
        return str(sid)
    me = _get("/users/me", user_id=user_id)
    sid = str(me.get("id"))
    if user_id is not None:
        db = SessionLocal()
        try:
            c = db.query(MLConta).filter_by(user_id=user_id).first()
            if c and not c.seller_id:
                c.seller_id = sid
                db.commit()
        finally:
            db.close()
    return sid


# =========================================================================== #
# Chamada base (com proteção de rate-limit 429)
# =========================================================================== #
def _req(metodo, path, user_id=None, params=None, json=None, headers=None, base=API, raw=False):
    tok = _access_token(user_id)
    h = {"Authorization": f"Bearer {tok}"}
    if json is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    url = f"{base}{path}"
    for tentativa in range(3):
        r = requests.request(metodo, url, params=params, json=json, headers=h, timeout=TIMEOUT)
        if r.status_code == 429:           # rate limit (1500/min por seller): backoff e tenta de novo
            time.sleep(2 * (tentativa + 1))
            continue
        if r.status_code >= 400:
            raise MLErro(f"{metodo} {path}: {r.status_code} {r.text[:200]}")
        if raw:
            return r
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}
    raise MLErro(f"{metodo} {path}: 429 (limite de chamadas) após retries")


def _get(path, params=None, user_id=None):
    return _req("GET", path, user_id, params=params)


def _put(path, json=None, user_id=None):
    return _req("PUT", path, user_id, json=json)


def _post(path, json=None, user_id=None, headers=None, params=None):
    return _req("POST", path, user_id, json=json, headers=headers, params=params)


# =========================================================================== #
# Domínio A — Conta
# =========================================================================== #
def conta(user_id=None) -> dict:
    return _get("/users/me", user_id=user_id)


def limites_publicacao(user_id=None) -> dict:
    """Quota de anúncios por site, baseada na reputação (GET /marketplace/users/cap)."""
    return _get("/marketplace/users/cap", user_id=user_id)


def reputacao(user_id=None) -> dict:
    sid = _seller_id(user_id)
    u = _get(f"/users/{sid}", user_id=user_id)
    rep = u.get("seller_reputation") or {}
    return {"nivel": rep.get("level_id"), "status": rep.get("power_seller_status"),
            "transacoes": rep.get("transactions"), "metricas": rep.get("metrics"),
            "experiencia": u.get("seller_experience")}


def grants(user_id=None) -> dict:
    a = _app()
    return _get(f"/applications/{a['client_id']}/grants", user_id=user_id)


# =========================================================================== #
# Domínio B — Catálogo / Anúncios
# =========================================================================== #
def _norm_item(it: dict) -> dict:
    sku = None
    for a in (it.get("attributes") or []):
        if a.get("id") == "SELLER_SKU" and a.get("value_name"):
            sku = a["value_name"]
            break
    if not sku:
        sku = it.get("seller_custom_field")
    fotos = it.get("pictures") or []
    ship = it.get("shipping") or {}
    return {
        "item_id": it.get("id"), "titulo": it.get("title"),
        "preco": float(it.get("price") or 0),
        "preco_original": float(it.get("original_price") or it.get("price") or 0),
        "moeda": it.get("currency_id"),
        "status": it.get("status"), "sub_status": it.get("sub_status"),
        "permalink": it.get("permalink"), "sku": sku,
        "estoque": it.get("available_quantity"), "vendidos": it.get("sold_quantity"),
        "category_id": it.get("category_id"),
        "listing_type_id": it.get("listing_type_id"),
        "logistic_type": ship.get("logistic_type"),
        "frete_gratis": bool(ship.get("free_shipping")),
        "fotos": [p.get("secure_url") or p.get("url") for p in fotos],
        "n_fotos": len(fotos),
        "tem_video": bool(it.get("video_id")),
        "atributos": it.get("attributes") or [],
        "health": it.get("health"),
        "variacoes": it.get("variations") or [],
        "imagem": (fotos[0].get("secure_url") if fotos else it.get("thumbnail")),
    }


def buscar_item_por_sku(sku: str, user_id=None) -> dict | None:
    """Acha o anúncio do vendedor que tem este SKU (seller_sku ou seller_custom_field)."""
    if not sku:
        return None
    sid = _seller_id(user_id)
    try:
        d = _get(f"/users/{sid}/items/search", params={"seller_sku": sku}, user_id=user_id)
        ids = d.get("results") or []
        if not ids:
            d = _get(f"/users/{sid}/items/search", params={"sku": sku}, user_id=user_id)
            ids = d.get("results") or []
        if ids:
            return obter_item(ids[0], user_id=user_id)
    except MLErro:
        pass
    return None


def obter_item(item_id: str, user_id=None) -> dict:
    return _norm_item(_get(f"/items/{item_id}", user_id=user_id))


def obter_itens(ids, user_id=None, attributes=None):
    """Multiget: GET /items?ids= (lotes de 20). Respeita o rate-limit lendo em bloco."""
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 20):
        lote = ids[i:i + 20]
        params = {"ids": ",".join(lote)}
        if attributes:
            params["attributes"] = attributes
        d = _get("/items", params=params, user_id=user_id)
        for entry in (d if isinstance(d, list) else []):
            if isinstance(entry, dict) and entry.get("code") == 200 and entry.get("body"):
                out.append(_norm_item(entry["body"]))
    return out


def listar_ids(user_id=None, filtros=None, limite=None):
    """Lista todos os item_ids do vendedor (scan/scroll, passa dos 1.000)."""
    sid = _seller_id(user_id)
    ids = []
    base = {"search_type": "scan", "limit": 100}
    if filtros:
        base.update(filtros)
    scroll = None
    while True:
        p = dict(base)
        if scroll:
            p["scroll_id"] = scroll
        d = _get(f"/users/{sid}/items/search", params=p, user_id=user_id)
        res = d.get("results") or []
        if not res:
            break
        ids.extend(res)
        scroll = d.get("scroll_id")
        if not scroll or (limite and len(ids) >= limite):
            break
    return ids[:limite] if limite else ids


def descricao_item(item_id: str, user_id=None) -> str:
    try:
        d = _get(f"/items/{item_id}/description", user_id=user_id)
        return d.get("plain_text") or d.get("text") or ""
    except MLErro:
        return ""


def itens_publicos(seller_id=None, user_id=None, limit=50, offset=0):
    sid = seller_id or _seller_id(user_id)
    return _get(f"/sites/{SITE}/search",
                params={"seller_id": sid, "limit": limit, "offset": offset}, user_id=user_id)


def atualizar_status(item_id: str, status: str, user_id=None):
    _put(f"/items/{item_id}", json={"status": status}, user_id=user_id)
    return {"ok": True, "item_id": item_id, "status": status}


def atualizar_estoque(item_id: str, qtd: int, user_id=None):
    _put(f"/items/{item_id}", json={"available_quantity": int(qtd)}, user_id=user_id)
    return {"ok": True, "item_id": item_id, "estoque": int(qtd)}


def atualizar_atributos(item_id: str, atributos: list, user_id=None):
    _put(f"/items/{item_id}", json={"attributes": atributos}, user_id=user_id)
    return {"ok": True, "item_id": item_id}


def atualizar_fotos(item_id: str, pictures: list, user_id=None):
    _put(f"/items/{item_id}", json={"pictures": pictures}, user_id=user_id)
    return {"ok": True, "item_id": item_id}


def atualizar_descricao(item_id: str, texto: str, user_id=None):
    """Cria/atualiza a descrição do anúncio (PUT /items/{id}/description)."""
    _put(f"/items/{item_id}/description", json={"plain_text": texto}, user_id=user_id)
    return {"ok": True, "item_id": item_id}


def adicionar_foto(item_id: str, url: str, user_id=None):
    """Acrescenta uma foto ao anúncio, preservando as existentes (PUT /items {pictures})."""
    it = _get(f"/items/{item_id}", user_id=user_id)
    atuais = [{"id": p["id"]} for p in (it.get("pictures") or []) if p.get("id")]
    atuais.append({"source": url})
    _put(f"/items/{item_id}", json={"pictures": atuais}, user_id=user_id)
    return {"ok": True, "item_id": item_id, "n_fotos": len(atuais)}


# =========================================================================== #
# Domínio C — Preço & Líquido
# =========================================================================== #
def atualizar_preco(item_id: str, preco: float, user_id=None) -> dict:
    """Empurra o novo preço direto no anúncio do ML (PUT /items/{id})."""
    _put(f"/items/{item_id}", json={"price": round(float(preco), 2)}, user_id=user_id)
    return {"ok": True, "item_id": item_id, "preco": round(float(preco), 2)}


def preco_de_venda(item_id: str, user_id=None) -> dict:
    """Preço de venda vencedor + contexto de promoção (GET /items/{id}/prices)."""
    return _get(f"/items/{item_id}/prices", user_id=user_id)


def tarifas_de_venda(category_id=None, price=0.0, listing_type_id="gold_special",
                     logistic_type=None, shipping_mode=None, user_id=None) -> dict:
    """Tarifa de venda (sale_fee %) + custo fixo por faixa (GET /sites/MLB/listing_prices).
    listing_type_id: free | gold_special (Clássico) | gold_pro (Premium)."""
    params = {"price": round(float(price), 2), "listing_type_id": listing_type_id,
              "currency_id": "BRL"}
    if category_id:
        params["category_id"] = category_id
    if logistic_type:
        params["logistic_type"] = logistic_type
    if shipping_mode:
        params["shipping_mode"] = shipping_mode
    d = _get(f"/sites/{SITE}/listing_prices", params=params, user_id=user_id)
    obj = None
    if isinstance(d, list):
        for o in d:
            if o.get("listing_type_id") == listing_type_id:
                obj = o
                break
        obj = obj or (d[0] if d else {})
    elif isinstance(d, dict):
        obj = d
    obj = obj or {}
    det = obj.get("sale_fee_details") or {}
    sale_fee = float(obj.get("sale_fee_amount") or 0)
    return {"listing_type_id": obj.get("listing_type_id"),
            "comissao_pct": float(det.get("percentage_fee") or 0),
            "sale_fee": round(sale_fee, 2),
            "custo_fixo": round(float(det.get("fixed_fee") or 0), 2),
            "gross_amount": float(det.get("gross_amount") or 0)}


def frete_do_item(item_id: str, cep: str, user_id=None) -> dict:
    return _get(f"/items/{item_id}/shipping_options", params={"zip_code": cep}, user_id=user_id)


def calcular_liquido(preco, category_id=None, listing_type_id="gold_special",
                     logistic_type=None, frete=0.0, imposto_pct=0.0, custo=0.0, user_id=None) -> dict:
    """Anatomia do líquido do ML: preço − comissão − custo fixo − frete − imposto."""
    preco = float(preco or 0)
    taxas = tarifas_de_venda(category_id, preco, listing_type_id, logistic_type, user_id=user_id)
    sale_fee = taxas["sale_fee"]
    fixo = taxas["custo_fixo"]
    imp = round(preco * (float(imposto_pct or 0) / 100), 2)
    frete = round(float(frete or 0), 2)
    liquido = round(preco - sale_fee - fixo - frete - imp, 2)
    custo = float(custo or 0)
    margem = round((liquido - custo) / liquido * 100, 1) if (custo and liquido) else None
    quebra = []
    if sale_fee:
        quebra.append({"rotulo": f"Comissão {taxas['comissao_pct']:.1f}%", "valor": -sale_fee})
    if fixo:
        quebra.append({"rotulo": "Custo fixo ML", "valor": -fixo})
    if frete:
        quebra.append({"rotulo": "Frete", "valor": -frete})
    if imp:
        quebra.append({"rotulo": f"Imposto {imposto_pct:.0f}%", "valor": -imp})
    return {"preco": round(preco, 2), "sale_fee": sale_fee, "custo_fixo": fixo,
            "frete": frete, "imposto": imp, "liquido": liquido, "margem": margem,
            "lucro": round(liquido - custo, 2) if custo else None,
            "comissao_pct": taxas["comissao_pct"], "quebra": quebra}


# =========================================================================== #
# Domínio D — Radar de concorrência (nativo)
# =========================================================================== #
def _amt(x):
    return float((x or {}).get("amount") or 0)


def referencia_de_preco(item_id: str, user_id=None) -> dict:
    """Preço de referência + concorrentes + custos (GET /marketplace/benchmarks/items/{id}/details)."""
    d = _get(f"/marketplace/benchmarks/items/{item_id}/details", user_id=user_id)
    meta = d.get("metadata") or {}
    grafo = []
    for g in (meta.get("graph") or []):
        info = g.get("info") or {}
        grafo.append({"item_id": g.get("item_id"), "titulo": info.get("title"),
                      "preco": _amt(g.get("price")), "vendas": info.get("sold_quantity"),
                      "atual": g.get("current"), "sugerido": g.get("suggested")})
    custos = d.get("costs") or {}
    return {"item_id": d.get("item_id"), "status": d.get("status"),
            "atual": _amt(d.get("current_price")), "sugerido": _amt(d.get("suggested_price")),
            "menor": _amt(d.get("lowest_price")), "interno": _amt(d.get("internal_price")),
            "externo": _amt(d.get("external_price")), "diff_pct": d.get("percent_difference"),
            "comissao": float(custos.get("selling_fees") or 0),
            "frete": float(custos.get("shipping_fees") or 0),
            "aplicavel": d.get("applicable_suggestion"), "concorrentes": grafo}


def itens_com_referencia(seller_id=None, user_id=None) -> dict:
    sid = seller_id or _seller_id(user_id)
    return _get(f"/marketplace/benchmarks/user/{sid}/items", user_id=user_id)


def concorrentes(query: str, category_id=None, limit=20, user_id=None) -> dict:
    """Descoberta pública de concorrentes (GET /sites/MLB/search?q=)."""
    params = {"q": query, "limit": limit}
    if category_id:
        params["category_id"] = category_id
    d = _get(f"/sites/{SITE}/search", params=params, user_id=user_id)
    out = []
    for r in (d.get("results") or []):
        out.append({"item_id": r.get("id"), "titulo": r.get("title"),
                    "preco": float(r.get("price") or 0), "vendas": r.get("sold_quantity"),
                    "vendedor": (r.get("seller") or {}).get("nickname"),
                    "permalink": r.get("permalink"),
                    "frete_gratis": bool((r.get("shipping") or {}).get("free_shipping"))})
    return {"total": (d.get("paging") or {}).get("total"), "resultados": out}


# =========================================================================== #
# Domínio E — Pedidos
# =========================================================================== #
def listar_pedidos(user_id=None, status="paid", desde=None, ate=None, offset=0, limit=50) -> dict:
    sid = _seller_id(user_id)
    params = {"seller": sid, "sort": "date_desc", "offset": offset, "limit": limit}
    if status:
        params["order.status"] = status
    if desde:
        params["order.date_created.from"] = desde
    if ate:
        params["order.date_created.to"] = ate
    return _get("/orders/search", params=params, user_id=user_id)


def obter_pedido(order_id: str, user_id=None) -> dict:
    return _get(f"/orders/{order_id}", user_id=user_id)


def pedidos_do_pack(pack_id: str, user_id=None) -> dict:
    return _get(f"/marketplace/orders/pack/{pack_id}", user_id=user_id)


def responder_feedback(feedback_id: str, texto: str, user_id=None) -> dict:
    return _post(f"/feedback/{feedback_id}/reply", json={"reply": texto}, user_id=user_id)


# =========================================================================== #
# Domínio F — Envios & Etiquetas (resolve mascaramento de endereço)
# =========================================================================== #
def envio_do_pedido(shipment_id: str, user_id=None) -> dict:
    """Detalhe do envio com nome+endereço reais do comprador (header x-format-new: true)."""
    return _req("GET", f"/shipments/{shipment_id}", user_id=user_id,
                headers={"x-format-new": "true"})


def custos_de_envio(order_id: str, user_id=None) -> dict:
    """shipments_options: cost (pago pelo comprador) + list_cost (pago pelo vendedor)."""
    return _get(f"/orders/{order_id}/shipments", user_id=user_id)


def custos_do_shipment(shipment_id, user_id=None) -> dict:
    """Custos do envio: receiver.cost (comprador) + senders[].cost (vendedor). Seção 3."""
    return _get(f"/shipments/{shipment_id}/costs", user_id=user_id)


def _custos_envio(raw: dict) -> dict:
    """Extrai frete do vendedor e do comprador da resposta de /shipments/{id}/costs."""
    raw = raw or {}
    receiver = raw.get("receiver") or {}
    senders = raw.get("senders") or []
    vendedor = None
    if senders and isinstance(senders, list):
        vendedor = senders[0].get("cost")
    return {"vendedor": vendedor, "comprador": receiver.get("cost")}


def etiqueta(shipment_ids, formato="pdf", user_id=None):
    """Etiqueta (waybill) real. Retorna (bytes, content_type). Máx 50 shipment_ids."""
    rt = "zpl2" if formato == "zpl" else "pdf"
    ids = ",".join(shipment_ids) if isinstance(shipment_ids, (list, tuple)) else str(shipment_ids)
    r = _req("GET", "/shipment_labels", user_id=user_id,
             params={"shipment_ids": ids, "response_type": rt}, raw=True)
    ct = r.headers.get("Content-Type") or ("application/pdf" if rt == "pdf" else "application/octet-stream")
    return r.content, ct


# =========================================================================== #
# Domínio G — Perguntas
# =========================================================================== #
def listar_perguntas(user_id=None, status=None, item_id=None, limit=50, offset=0) -> dict:
    params = {"api_version": 4, "limit": limit, "offset": offset}
    if item_id:
        params["item"] = item_id
    else:
        params["seller_id"] = _seller_id(user_id)
    if status:
        params["status"] = status
    return _get("/questions/search", params=params, user_id=user_id)


def responder_pergunta(question_id, texto: str, user_id=None) -> dict:
    return _post("/answers", json={"question_id": int(question_id), "text": texto}, user_id=user_id)


def ocultar_pergunta(question_id, user_id=None) -> dict:
    return _post("/my/questions/hidden", json={"questions_ids": [int(question_id)]}, user_id=user_id)


def tempo_de_resposta(user_id=None) -> dict:
    sid = _seller_id(user_id)
    return _get(f"/users/{sid}/questions/response_time", user_id=user_id)


# =========================================================================== #
# Domínio H — Avaliações
# =========================================================================== #
def avaliacoes_do_item(item_id: str, limit=20, offset=0, user_id=None) -> dict:
    d = _get(f"/reviews/item/{item_id}", params={"limit": limit, "offset": offset}, user_id=user_id)
    paging = d.get("paging") or {}
    revs = []
    for r in (d.get("reviews") or []):
        revs.append({"id": r.get("id"), "nota": r.get("rate"), "titulo": r.get("title"),
                     "texto": r.get("content"), "data": r.get("date_created"),
                     "likes": r.get("likes"), "dislikes": r.get("dislikes")})
    return {"total": paging.get("total"), "com_comentario": paging.get("reviews_with_comment"),
            "avaliacoes": revs}


# =========================================================================== #
# Domínio I — Visitas / Funil
# =========================================================================== #
def visitas_do_vendedor(desde: str, ate: str, user_id=None) -> dict:
    sid = _seller_id(user_id)
    return _get(f"/users/{sid}/items_visits",
                params={"date_from": desde, "date_to": ate}, user_id=user_id)


def visitas_do_item(item_id: str, last=30, unit="day", user_id=None) -> dict:
    return _get(f"/items/{item_id}/visits/time_window",
                params={"last": last, "unit": unit}, user_id=user_id)


def visitas_multi(ids, user_id=None) -> dict:
    return _get("/visits/items", params={"ids": ",".join(list(ids))}, user_id=user_id)


# =========================================================================== #
# Domínio J — Promoções (Promotions v2)
# =========================================================================== #
def promocoes_do_vendedor(user_id=None) -> dict:
    sid = _seller_id(user_id)
    return _get(f"/seller-promotions/users/{sid}", params={"app_version": "v2"}, user_id=user_id)


def promocoes_do_item(item_id: str, user_id=None) -> dict:
    return _get(f"/seller-promotions/items/{item_id}", params={"app_version": "v2"}, user_id=user_id)


def detalhe_oferta(offer_id: str, user_id=None) -> dict:
    return _get(f"/seller-promotions/offers/{offer_id}", params={"app_version": "v2"}, user_id=user_id)


def aplicar_desconto(item_id: str, deal_price, top_deal_price=None, inicio=None, fim=None, user_id=None) -> dict:
    """Cria um PRICE_DISCOUNT real no item (POST /marketplace/seller-promotions/items/{id})."""
    a = _app()
    sid = _seller_id(user_id)
    body = {"deal_price": round(float(deal_price), 2), "promotion_type": "PRICE_DISCOUNT"}
    if top_deal_price:
        body["top_deal_price"] = round(float(top_deal_price), 2)
    if inicio:
        body["start_date"] = inicio
    if fim:
        body["finish_date"] = fim
    headers = {"version": "v2", "X-Client-Id": str(a["client_id"]), "X-Caller-Id": str(sid)}
    return _req("POST", f"/marketplace/seller-promotions/items/{item_id}", user_id=user_id,
                params={"user_id": sid}, json=body, headers=headers)


def remover_desconto(item_id: str, user_id=None) -> dict:
    a = _app()
    sid = _seller_id(user_id)
    headers = {"version": "v2", "X-Client-Id": str(a["client_id"]), "X-Caller-Id": str(sid)}
    return _req("DELETE", f"/marketplace/seller-promotions/items/{item_id}", user_id=user_id,
                params={"promotion_type": "PRICE_DISCOUNT", "user_id": sid}, headers=headers)


# =========================================================================== #
# Domínio K — Qualidade do anúncio
# =========================================================================== #
def qualidade_ml(item_id: str, user_id=None) -> dict:
    """Diagnóstico 0-100 do anúncio (fotos, ficha, descrição, vídeo) + health do ML."""
    it = obter_item(item_id, user_id=user_id)
    desc = descricao_item(item_id, user_id=user_id)
    comp = []
    t = it.get("titulo") or ""
    comp.append({"chave": "titulo", "label": "Título", "valor": round(min(len(t) / 60, 1.0) * 20),
                 "max": 20, "status": "ok" if len(t) >= 40 else "alerta",
                 "detalhe": f"{len(t)} caracteres"})
    nf = it.get("n_fotos") or 0
    comp.append({"chave": "fotos", "label": "Fotos", "valor": round(min(nf / 8, 1.0) * 25),
                 "max": 25, "status": "ok" if nf >= 6 else ("alerta" if nf >= 3 else "ruim"),
                 "detalhe": f"{nf} foto(s)"})
    attrs = {a.get("id"): a.get("value_name") for a in (it.get("atributos") or [])}
    tem_ean = bool(attrs.get("GTIN") or attrs.get("EAN"))
    preenchidos = sum(1 for v in attrs.values() if v)
    attr_score = min((preenchidos / 12) + (0.3 if tem_ean else 0), 1.0)
    comp.append({"chave": "atributos", "label": "Ficha técnica", "valor": round(attr_score * 20),
                 "max": 20, "status": "ok" if (tem_ean and preenchidos >= 6) else "alerta",
                 "detalhe": ("com EAN" if tem_ean else "sem EAN") + f", {preenchidos} atributos"})
    dlen = len(desc or "")
    comp.append({"chave": "descricao", "label": "Descrição", "valor": round(min(dlen / 600, 1.0) * 20),
                 "max": 20, "status": "ok" if dlen >= 300 else ("alerta" if dlen > 0 else "ruim"),
                 "detalhe": f"{dlen} caracteres"})
    tv = it.get("tem_video")
    comp.append({"chave": "video", "label": "Vídeo", "valor": 15 if tv else 0, "max": 15,
                 "status": "ok" if tv else "alerta", "detalhe": "com vídeo" if tv else "sem vídeo"})
    return {"item_id": item_id, "score": sum(c["valor"] for c in comp), "health": it.get("health"),
            "titulo": t, "status": it.get("status"), "componentes": comp}


# =========================================================================== #
# Cache & sincronização
# =========================================================================== #
def _upsert_cache(db, user_id, it):
    c = db.query(MLItemCache).filter_by(user_id=user_id, item_id=it["item_id"]).first()
    if not c:
        c = MLItemCache(user_id=user_id, item_id=it["item_id"])
        db.add(c)
    c.sku = it.get("sku")
    c.titulo = it.get("titulo")
    c.preco = it.get("preco") or 0
    c.preco_original = it.get("preco_original") or 0
    c.status = it.get("status")
    c.estoque = it.get("estoque")
    c.category_id = it.get("category_id")
    c.listing_type_id = it.get("listing_type_id")
    c.logistic_type = it.get("logistic_type")
    c.permalink = it.get("permalink")
    c.imagem = it.get("imagem")
    c.saude = it.get("health")
    c.em_promocao = bool(it.get("preco_original") and it.get("preco")
                         and it["preco"] < it["preco_original"])
    c.atualizado_em = datetime.utcnow()


def sincronizar_catalogo(user_id) -> dict:
    """Varre todos os anúncios (scan) e popula o MLItemCache via multiget. Job pesado."""
    db = SessionLocal()
    try:
        s = db.query(MLSync).filter_by(user_id=user_id).first()
        if not s:
            s = MLSync(user_id=user_id)
            db.add(s)
        s.status = "rodando"
        s.iniciado_em = datetime.utcnow()
        s.processados = 0
        s.erro = None
        db.commit()
    finally:
        db.close()
    try:
        ids = listar_ids(user_id=user_id)
        db = SessionLocal()
        try:
            s = db.query(MLSync).filter_by(user_id=user_id).first()
            s.total = len(ids)
            db.commit()
        finally:
            db.close()
        proc = 0
        for i in range(0, len(ids), 20):
            itens = obter_itens(ids[i:i + 20], user_id=user_id)
            db = SessionLocal()
            try:
                for it in itens:
                    _upsert_cache(db, user_id, it)
                proc += len(itens)
                s = db.query(MLSync).filter_by(user_id=user_id).first()
                s.processados = proc
                db.commit()
            finally:
                db.close()
        db = SessionLocal()
        try:
            s = db.query(MLSync).filter_by(user_id=user_id).first()
            s.status = "concluido"
            s.concluido_em = datetime.utcnow()
            db.commit()
        finally:
            db.close()
        return {"ok": True, "total": len(ids)}
    except Exception as e:  # noqa: BLE001
        db = SessionLocal()
        try:
            s = db.query(MLSync).filter_by(user_id=user_id).first()
            if s:
                s.status = "erro"
                s.erro = str(e)[:300]
                db.commit()
        finally:
            db.close()
        raise


def status_sync(user_id) -> dict:
    db = SessionLocal()
    try:
        s = db.query(MLSync).filter_by(user_id=user_id).first()
        if not s:
            return {"status": "ocioso", "total": 0, "processados": 0}
        return {"status": s.status, "total": s.total, "processados": s.processados, "erro": s.erro,
                "iniciado_em": s.iniciado_em.isoformat() if s.iniciado_em else None,
                "concluido_em": s.concluido_em.isoformat() if s.concluido_em else None}
    finally:
        db.close()


def listar_cache(user_id, sku=None, limite=200):
    db = SessionLocal()
    try:
        q = db.query(MLItemCache).filter_by(user_id=user_id)
        if sku:
            q = q.filter(MLItemCache.sku == sku)
        rows = q.limit(limite).all()
        return [{"item_id": r.item_id, "sku": r.sku, "titulo": r.titulo, "preco": r.preco,
                 "preco_original": r.preco_original, "status": r.status, "estoque": r.estoque,
                 "category_id": r.category_id, "listing_type_id": r.listing_type_id,
                 "logistic_type": r.logistic_type, "permalink": r.permalink, "imagem": r.imagem,
                 "em_promocao": r.em_promocao,
                 "atualizado_em": r.atualizado_em.isoformat() if r.atualizado_em else None}
                for r in rows]
    finally:
        db.close()


def cache_por_sku(user_id, sku):
    rows = listar_cache(user_id, sku=sku, limite=1)
    return rows[0] if rows else None


# =========================================================================== #
# Webhooks (tempo real) — atualiza o cache a partir das notificações do ML
# =========================================================================== #
def _dt_iso(s):
    """Parse ISO do ML → datetime naive. Tolerante a fuso e a None."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d.replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def _limite_valor(x):
    return x.get("date") if isinstance(x, dict) else x


def _resumo_envio(raw: dict) -> dict:
    """Extrai do shipment (x-format-new) os campos que o painel usa. 100% defensivo."""
    raw = raw or {}
    lead = raw.get("lead_time") or {}
    sh = raw.get("status_history") or {}
    ra = raw.get("receiver_address") or {}
    status = raw.get("status")
    substatus = raw.get("substatus")
    handling = _limite_valor(lead.get("estimated_handling_limit")) or _limite_valor(raw.get("estimated_handling_limit"))
    delivery = _limite_valor(lead.get("estimated_delivery_limit")) or _limite_valor(lead.get("estimated_delivery_time"))
    try:
        custo_comprador = (raw.get("shipping_option") or {}).get("cost")
    except Exception:  # noqa: BLE001
        custo_comprador = None
    sub = str(substatus or "")
    fiscal_pend = sub in ("invoice_pending", "waiting_for_invoice") or ("invoice" in sub and "pend" in sub)
    devol = status in ("returned", "to_be_returned") or ("return" in sub)
    cidade = (ra.get("city") or {}).get("name") if isinstance(ra.get("city"), dict) else ra.get("city")
    estado = (ra.get("state") or {}).get("name") if isinstance(ra.get("state"), dict) else ra.get("state")
    linha = ra.get("address_line") or " ".join(
        x for x in [ra.get("street_name"), str(ra.get("street_number") or "")] if x
    ).strip()
    return {
        "status": status, "substatus": substatus,
        "logistic_type": raw.get("logistic_type"), "mode": raw.get("mode"),
        "handling_limit": handling, "delivery_limit": delivery,
        "date_ready": sh.get("date_ready_to_ship"), "date_shipped": sh.get("date_shipped"),
        "date_delivered": sh.get("date_delivered"),
        "tracking_number": raw.get("tracking_number"), "tracking_method": raw.get("tracking_method"),
        "custo_comprador": custo_comprador,
        "receiver_nome": ra.get("receiver_name"), "receiver_endereco": linha or None,
        "receiver_cidade": cidade, "receiver_estado": estado, "receiver_cep": ra.get("zip_code"),
        "fiscal_pendente": bool(fiscal_pend), "devolucao": bool(devol),
    }


def _upsert_envio_cache(db, user_id, shipment_id, raw, order_id=None, custos=None):
    from .models import MLEnvioCache
    r = _resumo_envio(raw)
    c = db.query(MLEnvioCache).filter_by(user_id=user_id, shipment_id=str(shipment_id)).first()
    if not c:
        c = MLEnvioCache(user_id=user_id, shipment_id=str(shipment_id))
        db.add(c)
    if order_id:
        c.order_id = str(order_id)
    c.status = r["status"]; c.substatus = r["substatus"]
    c.logistic_type = r["logistic_type"]; c.mode = r["mode"]
    c.handling_limit = _dt_iso(r["handling_limit"]); c.delivery_limit = _dt_iso(r["delivery_limit"])
    c.date_ready = _dt_iso(r["date_ready"]); c.date_shipped = _dt_iso(r["date_shipped"])
    c.date_delivered = _dt_iso(r["date_delivered"])
    c.tracking_number = r["tracking_number"]; c.tracking_method = r["tracking_method"]
    c.custo_comprador = r["custo_comprador"]
    if custos:
        if custos.get("vendedor") is not None:
            c.custo_vendedor = custos["vendedor"]
        if custos.get("comprador") is not None:
            c.custo_comprador = custos["comprador"]
    c.receiver_nome = r["receiver_nome"]; c.receiver_endereco = r["receiver_endereco"]
    c.receiver_cidade = r["receiver_cidade"]; c.receiver_estado = r["receiver_estado"]; c.receiver_cep = r["receiver_cep"]
    c.fiscal_pendente = r["fiscal_pendente"]; c.devolucao = r["devolucao"]
    c.dados = raw
    c.atualizado_em = datetime.utcnow()
    return c


def _envio_cache_dict(c) -> dict:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "status": c.status, "substatus": c.substatus,
        "logistic_type": c.logistic_type, "mode": c.mode,
        "handling_limit": iso(c.handling_limit), "delivery_limit": iso(c.delivery_limit),
        "date_ready": iso(c.date_ready), "date_shipped": iso(c.date_shipped), "date_delivered": iso(c.date_delivered),
        "tracking_number": c.tracking_number, "tracking_method": c.tracking_method,
        "custo_comprador": c.custo_comprador, "custo_vendedor": c.custo_vendedor,
        "receiver_nome": c.receiver_nome, "receiver_endereco": c.receiver_endereco,
        "receiver_cidade": c.receiver_cidade, "receiver_estado": c.receiver_estado, "receiver_cep": c.receiver_cep,
        "fiscal_pendente": bool(c.fiscal_pendente), "devolucao": bool(c.devolucao),
        "atualizado_em": iso(c.atualizado_em),
    }


def ler_envios_cache(db, user_id, shipment_ids) -> dict:
    """Lê do cache os envios pedidos (hot-path, sem chamar o ML). {shipment_id: dict}."""
    from .models import MLEnvioCache
    ids = [str(x) for x in shipment_ids if x]
    if not ids:
        return {}
    out = {}
    for i in range(0, len(ids), 400):
        bloco = ids[i:i + 400]
        for c in db.query(MLEnvioCache).filter(
            MLEnvioCache.user_id == user_id, MLEnvioCache.shipment_id.in_(bloco)
        ).all():
            out[c.shipment_id] = _envio_cache_dict(c)
    return out


def sincronizar_envios(user_id, shipment_ids, cap=60) -> dict:
    """Backfill: busca no ML os envios ainda ausentes do cache (até `cap`) e grava.
    Mantém o hot-path leve — os webhooks do tópico `shipments` mantêm o resto vivo."""
    from .models import MLEnvioCache
    ids = [str(x) for x in shipment_ids if x]
    db = SessionLocal()
    try:
        existentes = set()
        if ids:
            for i in range(0, len(ids), 400):
                bloco = ids[i:i + 400]
                for (sid,) in db.query(MLEnvioCache.shipment_id).filter(
                    MLEnvioCache.user_id == user_id, MLEnvioCache.shipment_id.in_(bloco)
                ).all():
                    existentes.add(sid)
        faltam = [x for x in ids if x not in existentes]
        alvo = faltam[:cap]
        buscados = 0
        for sid in alvo:
            try:
                raw = envio_do_pedido(sid, user_id=user_id)
                _upsert_envio_cache(db, user_id, sid, raw)
                buscados += 1
            except Exception:  # noqa: BLE001
                continue
        db.commit()
        return {"buscados": buscados, "faltam": max(0, len(faltam) - buscados), "total": len(ids)}
    finally:
        db.close()


def processar_notificacao(user_id, topic, resource) -> dict:
    try:
        if topic in ("items", "marketplace_items", "items_prices") and resource:
            item_id = str(resource).rstrip("/").split("/")[-1]
            if item_id.startswith("ML") or item_id.startswith("CBT"):
                it = obter_item(item_id, user_id=user_id)
                db = SessionLocal()
                try:
                    _upsert_cache(db, user_id, it)
                    db.commit()
                finally:
                    db.close()
                return {"ok": True, "item_id": item_id}
        if topic == "shipments" and resource:
            sid = str(resource).rstrip("/").split("/")[-1]
            if sid.isdigit():
                raw = envio_do_pedido(sid, user_id=user_id)
                custos = None
                try:
                    custos = _custos_envio(custos_do_shipment(sid, user_id=user_id))
                except Exception:  # noqa: BLE001
                    custos = None
                db = SessionLocal()
                try:
                    _upsert_envio_cache(db, user_id, sid, raw, custos=custos)
                    db.commit()
                finally:
                    db.close()
                return {"ok": True, "shipment_id": sid}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    return {"ok": True, "ignorado": True}


# =========================================================================== #
# Domínio — Pós-venda (reclamações / devoluções)  [post-purchase v1 + returns v2]
# =========================================================================== #
_REASON_PREFIXO = {
    "PNR": "Produto não recebido",
    "PDD": "Produto com defeito",
    "PDW": "Produto diferente do anúncio",
    "MED": "Mediação",
    "CANC": "Cancelamento",
}


def _claim_item(c: dict) -> dict:
    c = c or {}
    res = c.get("resource")
    rid = c.get("resource_id")
    order_id = str(rid) if res in ("order", "purchase") and rid else None
    players = c.get("players") or []
    comprador = next((p.get("user_id") for p in players if p.get("role") == "complainant"), None)
    reason = str(c.get("reason_id") or "")
    pref = next((v for k, v in _REASON_PREFIXO.items() if reason.startswith(k)), None)
    return {
        "claim_id": c.get("id"),
        "order_id": order_id, "pack_id": c.get("pack_id"),
        "resource": res, "resource_id": rid,
        "stage": c.get("stage"), "status": c.get("status"), "type": c.get("type"),
        "reason_id": c.get("reason_id"), "reason_grupo": pref,
        "comprador_id": comprador,
        "last_updated": c.get("last_updated") or c.get("date_created"),
    }


def listar_posvenda(user_id=None, status="opened", limit=50) -> dict:
    """Reclamações/devoluções em que o vendedor é parte. Fonte do balde Devoluções
    e do painel de Pós-venda. Ver relatório, seção 9 (POST-PURCHASE claims)."""
    sid = _seller_id(user_id)
    params = {"players.user_id": sid, "limit": limit, "sort": "last_updated:desc"}
    if status:
        params["status"] = status
    data = _get("/post-purchase/v1/claims/search", params=params, user_id=user_id)
    linhas = data.get("data") or data.get("results") or []
    paging = data.get("paging") or {}
    return {"itens": [_claim_item(c) for c in linhas], "total": paging.get("total", len(linhas))}


def detalhe_posvenda(claim_id, user_id=None) -> dict:
    """Claim + detalhe (o que precisa ser feito) + se afeta reputação (janela de 48h)."""
    base = f"/post-purchase/v1/claims/{claim_id}"
    claim = _get(base, user_id=user_id)
    try:
        detalhe = _get(base + "/detail", user_id=user_id)
    except Exception:  # noqa: BLE001
        detalhe = {}
    try:
        reput = _get(base + "/affects-reputation", user_id=user_id)
    except Exception:  # noqa: BLE001
        reput = {}
    reason_nome = None
    rid = (claim or {}).get("reason_id")
    if rid:
        try:
            reason_nome = (_get(f"/post-purchase/v1/claims/reasons/{rid}", user_id=user_id) or {}).get("name")
        except Exception:  # noqa: BLE001
            reason_nome = None
    players = (claim or {}).get("players") or []
    acoes = []
    for p in players:
        if p.get("role") in ("respondent", "seller"):
            for a in (p.get("available_actions") or []):
                acoes.append({"action": a.get("action"), "mandatory": a.get("mandatory"), "due_date": a.get("due_date")})
    return {
        "resumo": _claim_item(claim),
        "reason_nome": reason_nome,
        "titulo": (detalhe or {}).get("title"),
        "descricao": (detalhe or {}).get("description"),
        "problema": (detalhe or {}).get("problem"),
        "due_date": (detalhe or {}).get("due_date"),
        "responsavel": (detalhe or {}).get("action_responsible"),
        "afeta_reputacao": bool((reput or {}).get("affects_reputation")),
        "tem_incentivo": bool((reput or {}).get("has_incentive")),
        "resolucao": (claim or {}).get("resolution"),
        "acoes_vendedor": acoes,
    }


# =========================================================================== #
# Domínio — Detalhe de tarifa por pedido (faturamento real)  [seção 8]
# =========================================================================== #
def detalhe_tarifa(order_id, user_id=None) -> dict:
    """Composição real da tarifa cobrada no pedido (comissão, custo fixo, descontos/rebates),
    via relatório de faturamento filtrado por pedido. Best-effort e defensivo — nomes de
    campo confirmados no relatório, seção 8 (/billing/integration/group/ML/order/details)."""
    try:
        data = _get("/billing/integration/group/ML/order/details",
                    params={"order_ids": str(order_id)}, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    linhas = data.get("data") or data.get("results") or (data if isinstance(data, list) else [])
    saidas = []
    for row in linhas if isinstance(linhas, list) else []:
        sales = row.get("sales_info") or []
        for s in sales:
            sf = s.get("sale_fee") or {}
            di = s.get("discount_info") or {}
            saidas.append({
                "order_id": s.get("order_id"),
                "valor_venda": s.get("transaction_amount"),
                "financing_fee": s.get("financing_fee"),
                "tarifa_bruta": sf.get("gross"), "tarifa_liquida": sf.get("net"), "rebate": sf.get("rebate"),
                "sem_desconto": di.get("charge_amount_without_discount"),
                "desconto": di.get("discount_amount"),
            })
    return {"ok": True, "itens": saidas, "cru": data if not saidas else None}


# =========================================================================== #
# Domínio — Mensagens pós-venda (comprador)  [seção 10]
# =========================================================================== #
def _msg_item(m: dict, seller_id) -> dict:
    m = m or {}
    frm = (m.get("from") or {}).get("user_id")
    md = m.get("message_date") or {}
    mod = m.get("message_moderation") or {}
    anexos = [{"nome": a.get("original_filename") or a.get("filename"), "tipo": a.get("type")}
              for a in (m.get("message_attachments") or [])]
    return {
        "id": m.get("id"),
        "de_vendedor": str(frm) == str(seller_id),
        "texto": m.get("text") or m.get("text_translated"),
        "data": md.get("created") or md.get("received") or md.get("available"),
        "lida": bool(md.get("read")),
        "status": m.get("status"),
        "moderacao": mod.get("status"),
        "anexos": anexos,
    }


def mensagens_pedido(pack_id, user_id=None) -> dict:
    """Thread pós-venda de um pedido/pacote. Não marca como lida (mark_as_read=false)."""
    sid = _seller_id(user_id)
    data = _get(f"/messages/packs/{pack_id}/sellers/{sid}",
                params={"tag": "post_sale", "mark_as_read": "false"}, user_id=user_id)
    cs = data.get("conversation_status") or {}
    msgs = data.get("messages") or []
    return {
        "conversa": {
            "status": cs.get("status"), "substatus": cs.get("substatus"),
            "pode_responder": cs.get("status_update_allowed", True),
            "claim_id": cs.get("claim_id"), "shipping_id": cs.get("shipping_id"),
        },
        "mensagens": [_msg_item(m, sid) for m in msgs],
        "seller_id": sid,
    }


def enviar_mensagem(pack_id, buyer_id, texto, user_id=None) -> dict:
    """Responde o comprador (limite de 350 caracteres)."""
    sid = _seller_id(user_id)
    t = (texto or "").strip()[:350]
    if not t:
        return {"ok": False, "erro": "Mensagem vazia."}
    if not buyer_id:
        return {"ok": False, "erro": "Comprador não identificado."}
    body = {"from": {"user_id": int(sid)}, "to": {"user_id": int(buyer_id)}, "text": t}
    try:
        r = _post(f"/messages/packs/{pack_id}/sellers/{sid}", json=body,
                  params={"tag": "post_sale"}, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    return {"ok": True, "resposta": r}


def mensagens_nao_lidas(user_id=None) -> dict:
    """Contagem de conversas não lidas (badge)."""
    try:
        data = _get("/messages/unread", params={"tag": "post_sale", "role": "seller"}, user_id=user_id)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200], "total": 0, "recursos": []}
    res = data.get("results") or []
    total = sum(int(x.get("count") or 0) for x in res)
    return {"ok": True, "total": total,
            "recursos": [{"resource": x.get("resource"), "count": x.get("count")} for x in res]}


# =========================================================================== #
# Domínio — Dados fiscais do comprador (p/ NF-e)  [seção 12]
# =========================================================================== #
def dados_fiscais_comprador(order_id, user_id=None) -> dict:
    """Nome + CPF/CNPJ + endereço do comprador para a NF-e (/orders/{id}/billing_info,
    header x-version:2). É o que o Bling precisa para emitir. Defensivo."""
    try:
        data = _req("GET", f"/orders/{order_id}/billing_info", user_id=user_id, headers={"x-version": "2"})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    b = ((data or {}).get("buyer") or {}).get("billing_info") or {}
    ident = b.get("identification") or {}
    addr = b.get("address") or {}
    def _nome(x):
        return x.get("name") if isinstance(x, dict) else x
    nome = " ".join(v for v in [b.get("name"), b.get("last_name")] if v).strip() or None
    return {
        "ok": True,
        "nome": nome,
        "doc_tipo": ident.get("type"), "doc_numero": ident.get("number"),
        "endereco": " ".join(v for v in [addr.get("street_name"), str(addr.get("street_number") or "")] if v).strip() or None,
        "bairro": _nome(addr.get("neighborhood")),
        "cidade": addr.get("city_name") or _nome(addr.get("city")),
        "estado": _nome(addr.get("state")),
        "cep": addr.get("zip_code"),
    }
