"""Integração com a Shopee Open Platform (API v2) — multi-tenant.

Dois tipos de assinatura HMAC-SHA256:
  • pública (endpoints de auth): partner_id + path + timestamp
  • de loja (demais endpoints):   partner_id + path + timestamp + access_token + shop_id

O access_token expira em ~4h e é renovado automaticamente pelo refresh_token.
Credenciais do APP (partner_id/partner_key) vêm do ambiente; as da LOJA
(shop_id/access_token/refresh_token) ficam por usuário em ShopeeConta.
"""
import hashlib
import hmac
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import jwt
import requests

from .config import settings
from .db import SessionLocal
from .models import ShopeeConta


class ShopeeError(RuntimeError):
    pass


def app_configurado() -> bool:
    """O app (partner) está configurado no ambiente?"""
    return bool(settings.shopee_partner_id and settings.shopee_partner_key)


def _conta(db, user_id: int) -> ShopeeConta | None:
    c = db.query(ShopeeConta).filter_by(user_id=user_id).first()
    if c and (c.access_token or c.refresh_token):
        return c
    # fallback: credenciais únicas no ambiente (uso single-tenant)
    if settings.shopee_shop_id and settings.shopee_access_token:
        return ShopeeConta(user_id=user_id, shop_id=settings.shopee_shop_id,
                           access_token=settings.shopee_access_token,
                           refresh_token=settings.shopee_refresh_token or None,
                           expira_em=None)
    return None


def configurada(user_id: int) -> bool:
    db = SessionLocal()
    try:
        return app_configurado() and _conta(db, user_id) is not None
    finally:
        db.close()


def status_conexao(user_id: int) -> dict:
    db = SessionLocal()
    try:
        if not app_configurado():
            return {"app": False, "loja": False,
                    "msg": "Faltam SHOPEE_PARTNER_ID e SHOPEE_PARTNER_KEY no servidor."}
        c = _conta(db, user_id)
        if not c:
            return {"app": True, "loja": False,
                    "msg": "App pronto. Falta autorizar a loja (shop_id + tokens)."}
        return {"app": True, "loja": True, "shop_id": c.shop_id, "nome_loja": c.nome_loja,
                "expira_em": c.expira_em.isoformat() if c.expira_em else None}
    finally:
        db.close()


# ----------------------------- Assinaturas -------------------------------- #
def _sign(base: str) -> str:
    return hmac.new(settings.shopee_partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


def _sign_publica(path: str, ts: int) -> str:
    return _sign(f"{settings.shopee_partner_id}{path}{ts}")


def _sign_loja(path: str, ts: int, access_token: str, shop_id: str) -> str:
    return _sign(f"{settings.shopee_partner_id}{path}{ts}{access_token}{shop_id}")


# ------------------------------- Tokens ----------------------------------- #
def url_autorizacao(redirect: str) -> str:
    """URL para o lojista autorizar o app na conta Shopee dele."""
    ts = int(time.time())
    path = "/api/v2/shop/auth_partner"
    sign = _sign_publica(path, ts)
    redir = quote(redirect, safe="")
    return (f"{settings.shopee_base_url}{path}?partner_id={settings.shopee_partner_id}"
            f"&timestamp={ts}&sign={sign}&redirect={redir}")


def state_token(user_id: int) -> str:
    """Token curto (10min) que carrega o user_id pelo redirect do OAuth da Shopee."""
    payload = {"uid": user_id, "exp": datetime.utcnow() + timedelta(minutes=10), "scp": "shopee_oauth"}
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def ler_state(token: str):
    try:
        d = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return d.get("uid") if d.get("scp") == "shopee_oauth" else None
    except jwt.PyJWTError:
        return None


def trocar_code_por_token(user_id: int, code: str, shop_id: str) -> dict:
    """Troca o 'code' do callback de autorização por access_token + refresh_token."""
    ts = int(time.time())
    path = "/api/v2/auth/token/get"
    sign = _sign_publica(path, ts)
    url = f"{settings.shopee_base_url}{path}?partner_id={settings.shopee_partner_id}&timestamp={ts}&sign={sign}"
    body = {"code": code, "shop_id": int(shop_id), "partner_id": int(settings.shopee_partner_id)}
    try:
        r = requests.post(url, json=body, timeout=30)
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ShopeeError(f"Falha ao obter token: {e}")
    if d.get("error"):
        raise ShopeeError(f"{d.get('error')}: {d.get('message')}")
    salvar_conta(user_id, shop_id, d.get("access_token"), d.get("refresh_token"),
                 d.get("expire_in", 14400))
    return {"ok": True, "shop_id": shop_id}


def salvar_conta(user_id: int, shop_id, access_token, refresh_token, expire_in: int = 14400):
    db = SessionLocal()
    try:
        c = db.query(ShopeeConta).filter_by(user_id=user_id).first()
        if not c:
            c = ShopeeConta(user_id=user_id)
            db.add(c)
        c.shop_id = str(shop_id)
        c.access_token = access_token
        c.refresh_token = refresh_token
        c.expira_em = datetime.utcnow() + timedelta(seconds=int(expire_in) - 300)  # margem 5min
        if not c.conectado_em:
            c.conectado_em = datetime.utcnow()
        c.ativo = True
        db.commit()
    finally:
        db.close()


def renovar_token(user_id: int) -> bool:
    """Renova o access_token usando o refresh_token. Retorna True se renovou."""
    db = SessionLocal()
    try:
        c = db.query(ShopeeConta).filter_by(user_id=user_id).first()
        if not c or not c.refresh_token or not c.shop_id:
            return False
    finally:
        db.close()
    ts = int(time.time())
    path = "/api/v2/auth/access_token/get"
    sign = _sign_publica(path, ts)
    url = f"{settings.shopee_base_url}{path}?partner_id={settings.shopee_partner_id}&timestamp={ts}&sign={sign}"
    body = {"refresh_token": c.refresh_token, "shop_id": int(c.shop_id),
            "partner_id": int(settings.shopee_partner_id)}
    try:
        r = requests.post(url, json=body, timeout=30)
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ShopeeError(f"Falha ao renovar token: {e}")
    if d.get("error"):
        raise ShopeeError(f"{d.get('error')}: {d.get('message')}")
    salvar_conta(user_id, c.shop_id, d.get("access_token"), d.get("refresh_token"),
                 d.get("expire_in", 14400))
    return True


def _token_valido(user_id: int) -> tuple[str, str]:
    """Garante um access_token válido (renova se expirado). Retorna (access_token, shop_id)."""
    db = SessionLocal()
    try:
        c = _conta(db, user_id)
        if not c:
            raise ShopeeError("Loja Shopee não conectada.")
        precisa = c.expira_em and c.expira_em <= datetime.utcnow()
    finally:
        db.close()
    if precisa:
        renovar_token(user_id)
        db = SessionLocal()
        try:
            c = _conta(db, user_id)
        finally:
            db.close()
    return c.access_token, c.shop_id


# ------------------------------ Chamada base ------------------------------ #
def _chamar(user_id: int, path: str, extra: dict | None = None, metodo: str = "GET", timeout: int = 25) -> dict:
    if not app_configurado():
        raise ShopeeError("App Shopee não configurado no servidor.")
    access_token, shop_id = _token_valido(user_id)
    ts = int(time.time())
    sign = _sign_loja(path, ts, access_token, shop_id)
    params = {"partner_id": int(settings.shopee_partner_id), "timestamp": ts,
              "access_token": access_token, "shop_id": int(shop_id), "sign": sign}
    url = f"{settings.shopee_base_url}{path}"
    try:
        if metodo == "GET":
            r = requests.get(url, params={**params, **(extra or {})}, timeout=timeout)
        else:
            r = requests.post(url, params=params, json=(extra or {}), timeout=timeout)
        d = r.json()
    except (requests.RequestException, ValueError) as e:
        raise ShopeeError(f"Falha na chamada Shopee: {e}")
    if isinstance(d, dict) and d.get("error"):
        # token expirado no meio -> tenta renovar uma vez
        if "token" in str(d.get("error", "")).lower():
            renovar_token(user_id)
        raise ShopeeError(f"{d.get('error')}: {d.get('message')}")
    return d


# ------------------------------- Loja / saúde ----------------------------- #
def info_loja(user_id: int) -> dict:
    return _chamar(user_id, "/api/v2/shop/get_shop_info")


def desempenho_loja(user_id: int) -> dict:
    return _chamar(user_id, "/api/v2/account_health/get_shop_performance")


# ------------------------------- Catálogo --------------------------------- #
def nomes_itens(user_id: int, item_ids: list) -> dict:
    """Devolve {item_id: {nome, sku, imagem, preco, estoque}} para uma lista de item_ids.
    A Shopee aceita no máx. 50 ids por chamada de get_item_base_info, então processa em lotes."""
    mapa = {}
    ids = [int(i) for i in item_ids if i]
    if not ids:
        return mapa
    for ini in range(0, len(ids), 50):
        lote = ids[ini:ini + 50]
        try:
            info = info_itens(user_id, lote)
        except ShopeeError:
            continue
        for x in (info.get("response") or {}).get("item_list") or []:
            imgs = (x.get("image") or {}).get("image_url_list") or []
            precos = x.get("price_info") or []
            mapa[x.get("item_id")] = {
                "nome": x.get("item_name"),
                "sku": x.get("item_sku"),
                "imagem": imgs[0] if imgs else None,
                "preco": (precos[0].get("current_price") if precos else None),
                "estoque": (x.get("stock_info_v2") or {}).get("summary_info", {}).get("total_available_stock"),
            }
    return mapa


def listar_itens(user_id: int, offset: int = 0, limite: int = 50) -> dict:
    """Lista produtos da loja JÁ com nome, imagem e preço (get_item_list só traz IDs;
    enriquecemos com get_item_base_info)."""
    r = _chamar(user_id, "/api/v2/product/get_item_list",
                extra={"offset": offset, "page_size": min(limite, 100), "item_status": "NORMAL"})
    resp = r.get("response") or {}
    itens = resp.get("item") or []
    mapa = nomes_itens(user_id, [it.get("item_id") for it in itens])
    for it in itens:
        meta = mapa.get(it.get("item_id")) or {}
        it["item_name"] = meta.get("nome") or f"#{it.get('item_id')}"
        it["item_sku"] = meta.get("sku")
        it["image"] = meta.get("imagem")
        it["price"] = meta.get("preco")
        it["stock"] = meta.get("estoque")
    resp["item"] = itens
    r["response"] = resp
    return r


def info_itens(user_id: int, item_ids: list) -> dict:
    return _chamar(user_id, "/api/v2/product/get_item_base_info",
                   extra={"item_id_list": ",".join(str(i) for i in item_ids)})


# --------------------------------- Boost ---------------------------------- #
def impulsionar(user_id: int, item_ids: list) -> dict:
    """Impulsiona (boost) até 5 itens. Cada boost dura 4h."""
    return _chamar(user_id, "/api/v2/product/boost_item", metodo="POST",
                   extra={"item_id_list": [int(i) for i in item_ids][:5]})


def itens_impulsionados(user_id: int) -> dict:
    """Lista os itens atualmente impulsionados e quando o boost termina."""
    return _chamar(user_id, "/api/v2/product/get_boosted_list")


# ------------------------------ Avaliações -------------------------------- #
def todos_item_ids(user_id: int, max_paginas: int = 120) -> list:
    """Todos os item_id da loja (get_item_list cru, sem enriquecer). Pagina por offset."""
    ids, offset = [], 0
    for _ in range(max_paginas):
        r = _chamar(user_id, "/api/v2/product/get_item_list",
                    extra={"offset": offset, "page_size": 100, "item_status": "NORMAL"})
        resp = r.get("response") or {}
        lote = resp.get("item") or []
        ids.extend(int(it["item_id"]) for it in lote if it.get("item_id"))
        if not resp.get("has_next_page"):
            break
        offset = resp.get("next_offset") or (offset + 100)
    return ids


def comentarios_brutos(user_id: int, item_id=None, status: str = "UNANSWERED",
                       cursor: str = "", limite: int = 100) -> dict:
    """get_comment CRU (sem enriquecer com nome/foto) — usado na varredura em massa.
    Retorna o bloco 'response' {item_comment_list, more, next_cursor}."""
    extra = {"cursor": cursor, "page_size": min(limite, 100), "comment_status": status}
    if item_id:
        extra["item_id"] = int(item_id)
    r = _chamar(user_id, "/api/v2/product/get_comment", extra=extra)
    return r.get("response") or {}


def listar_avaliacoes(user_id: int, item_id=None, cursor: str = "", limite: int = 20,
                      status: str = "UNANSWERED") -> dict:
    """Comentários/avaliações já enriquecidos com nome e foto do produto.
    status: ALL | UNANSWERED | ANSWERED."""
    extra = {"cursor": cursor, "page_size": min(limite, 100), "comment_status": status}
    if item_id:
        extra["item_id"] = int(item_id)
    r = _chamar(user_id, "/api/v2/product/get_comment", extra=extra)
    coments = (r.get("response") or {}).get("item_comment_list") or []
    ids = list({c.get("item_id") for c in coments if c.get("item_id")})
    if ids:
        try:
            meta = nomes_itens(user_id, ids)
        except ShopeeError:
            meta = {}
        for c in coments:
            m = meta.get(c.get("item_id")) or {}
            c["produto_nome"] = m.get("nome")
            c["produto_imagem"] = m.get("imagem")
    return r


def responder_avaliacao(user_id: int, comment_id, texto: str) -> dict:
    """Responde uma avaliação (uma ou várias)."""
    return _chamar(user_id, "/api/v2/product/reply_comment", metodo="POST",
                   extra={"comment_list": [{"comment_id": int(comment_id), "comment": texto}]})


# ------------------------------- Pedidos ---------------------------------- #
_STATUS_PEDIDO = {
    "A_ENVIAR": ["READY_TO_SHIP", "PROCESSED"],
    "ENVIADO": ["SHIPPED"],
    "CONCLUIDO": ["COMPLETED"],
    "CANCELADO": ["CANCELLED", "IN_CANCEL"],
    "NAO_PAGO": ["UNPAID"],
}


def _margem_real_shopee(cfg_prec: dict, preco: float, custo) -> dict | None:
    """Líquido, lucro e margem REAIS ao vender por `preco` na Shopee — depois de comissão,
    taxa fixa, imposto, cartão e embalagem (usa a config de precificação do usuário)."""
    preco = float(preco or 0)
    if preco <= 0:
        return None
    canais = cfg_prec.get("canais") or []
    canal = next((c for c in canais if (c.get("canal") or "").lower() == "shopee"), None)
    if not canal:
        return None
    faixas = sorted(canal.get("faixas") or [], key=lambda f: (f.get("ate") is None, f.get("ate") or 0))
    faixa = next((f for f in faixas if f.get("ate") is None or preco <= f.get("ate")), faixas[-1] if faixas else None)
    if faixa is None:
        return None
    pct = (float(faixa.get("comissao", 0)) + float(faixa.get("fixo_pct", 0))
           + float(cfg_prec.get("imposto", 0)) + float(cfg_prec.get("cartao", 0))) / 100.0
    fixos = float(faixa.get("fixo", 0)) + float(cfg_prec.get("embalagem", 0))
    liquido = preco * (1 - pct) - fixos
    tem_custo = custo is not None
    lucro = liquido - float(custo or 0)
    return {"liquido": round(liquido, 2), "taxas": round(preco - liquido, 2),
            "lucro": round(lucro, 2) if tem_custo else None,
            "margem_pct": round(lucro / preco * 100, 1) if tem_custo else None}


def pedidos_painel(user_id: int, status: str = "A_ENVIAR", dias: int = 15, limite: int = 100) -> dict:
    """Pedidos enriquecidos (produto, SKU, imagem, valor pago) + análise de valor:
    compara o que o comprador pagou com o seu preço de tabela (catálogo) e marca
    quando a venda saiu abaixo do preço. Ordena pelos mais urgentes (ship_by)."""
    from . import catalogo
    agora = int(time.time())
    inicio = agora - max(1, dias) * 86400
    statuses = _STATUS_PEDIDO.get(status, ["READY_TO_SHIP"])

    sns = []
    for st in statuses:
        cursor = ""
        for _ in range(6):
            r = _chamar(user_id, "/api/v2/order/get_order_list",
                        extra={"time_range_field": "create_time", "time_from": inicio, "time_to": agora,
                               "page_size": 100, "cursor": cursor, "order_status": st})
            resp = r.get("response") or {}
            for o in (resp.get("order_list") or []):
                if o.get("order_sn"):
                    sns.append(o["order_sn"])
            cursor = resp.get("next_cursor") or ""
            if not resp.get("more") or not cursor or len(sns) >= limite:
                break
        if len(sns) >= limite:
            break
    sns = sns[:limite]

    from . import catalogo, precificacao
    cat = {p["sku"]: p for p in catalogo.todos(user_id) if p.get("sku")}
    cfg_prec = precificacao.obter_config(user_id)
    margem_alvo = float(cfg_prec.get("margem_padrao") or 0)
    pedidos = []
    for i in range(0, len(sns), 50):
        lote = sns[i:i + 50]
        try:
            rd = _chamar(user_id, "/api/v2/order/get_order_detail",
                         extra={"order_sn_list": ",".join(lote),
                                "response_optional_fields": "item_list,recipient_address,buyer_username,pay_time,note"})
        except ShopeeError:
            continue
        for o in ((rd.get("response") or {}).get("order_list") or []):
            itens, total, abaixo_meta, prejuizo, sem_cad = [], 0.0, False, False, False
            lucro_pedido, tem_lucro = 0.0, False
            for it in (o.get("item_list") or []):
                sku = it.get("model_sku") or it.get("item_sku")
                qtd = int(it.get("model_quantity_purchased") or 0)
                pago = float(it.get("model_discounted_price") or it.get("model_original_price") or 0)
                total += pago * qtd
                base = cat.get(sku)
                custo = float(base.get("custo")) if (base and base.get("custo") is not None) else None
                if base is None:
                    sem_cad = True
                mr = _margem_real_shopee(cfg_prec, pago, custo)
                margem_real = mr.get("margem_pct") if mr else None
                lucro_real = mr.get("lucro") if mr else None
                if lucro_real is not None:
                    lucro_pedido += lucro_real * qtd
                    tem_lucro = True
                if margem_real is not None:
                    if margem_real < 0:
                        prejuizo = True
                    elif margem_alvo and margem_real < margem_alvo - 0.5:
                        abaixo_meta = True
                itens.append({
                    "nome": it.get("item_name") or f"#{it.get('item_id')}", "sku": sku, "qtd": qtd,
                    "variacao": it.get("model_name"),
                    "preco_pago": round(pago, 2), "imagem": (it.get("image_info") or {}).get("image_url"),
                    "custo": custo, "tem_cadastro": base is not None,
                    "margem_real": margem_real, "lucro_real": lucro_real,
                    "liquido": mr.get("liquido") if mr else None, "taxas_mkt": mr.get("taxas") if mr else None,
                })
            rec = o.get("recipient_address") or {}
            pedidos.append({
                "order_sn": o.get("order_sn"), "status": o.get("order_status"),
                "comprador": o.get("buyer_username") or rec.get("name") or "—",
                "cliente": rec.get("name"), "cidade": rec.get("city"), "uf": rec.get("state"),
                "endereco": {"nome": rec.get("name"), "telefone": rec.get("phone"),
                             "cidade": rec.get("city"), "uf": rec.get("state"),
                             "cep": rec.get("zipcode"), "completo": rec.get("full_address")},
                "ship_by": o.get("ship_by_date"), "criado": o.get("create_time"),
                "nota_comprador": o.get("note") or "",
                "total_pago": round(total, 2), "abaixo_meta": abaixo_meta, "prejuizo": prejuizo,
                "sem_cadastro": sem_cad, "lucro_real": round(lucro_pedido, 2) if tem_lucro else None,
                "itens": itens,
            })
    pedidos.sort(key=lambda x: x.get("ship_by") or 9_000_000_000_000)
    lucros = [p["lucro_real"] for p in pedidos if p.get("lucro_real") is not None]
    resumo = {"total": len(pedidos),
              "abaixo_meta": sum(1 for p in pedidos if p["abaixo_meta"] or p["prejuizo"]),
              "prejuizo": sum(1 for p in pedidos if p["prejuizo"]),
              "receita": round(sum(p["total_pago"] for p in pedidos), 2),
              "unidades": sum(sum(i["qtd"] for i in p["itens"]) for p in pedidos),
              "lucro_real": round(sum(lucros), 2) if lucros else None,
              "margem_alvo": margem_alvo, "cobertura_lucro": len(lucros)}
    return {"status": status, "pedidos": pedidos, "resumo": resumo, "margem_alvo": margem_alvo}


def lista_separacao(user_id: int, status: str = "A_ENVIAR", dias: int = 15) -> dict:
    """Lista de separação/picking: agrega todos os produtos dos pedidos do status,
    em ORDEM ALFABÉTICA, com a quantidade total a separar e em quantos pedidos aparece."""
    pain = pedidos_painel(user_id, status, dias, limite=300)
    agg = {}
    for p in pain["pedidos"]:
        for it in p["itens"]:
            k = it["sku"] or it["nome"]
            a = agg.setdefault(k, {"nome": it["nome"], "sku": it["sku"], "qtd": 0, "pedidos": set()})
            a["qtd"] += it["qtd"]
            a["pedidos"].add(p["order_sn"])
    linhas = sorted(
        [{"nome": a["nome"], "sku": a["sku"], "qtd": a["qtd"], "pedidos": len(a["pedidos"])} for a in agg.values()],
        key=lambda x: (x["nome"] or "").lower())
    return {"itens": linhas, "total_unidades": sum(l["qtd"] for l in linhas),
            "skus": len(linhas), "pedidos": pain["resumo"]["total"], "status": status}


def pedido_detalhe(user_id: int, order_sn: str) -> dict:
    """Detalhe completo de UM pedido pro modal: produtos com margem real, repasse (escrow)
    com a quebra das taxas, comprador, endereço e logística."""
    from . import catalogo, precificacao
    rd = _chamar(user_id, "/api/v2/order/get_order_detail",
                 extra={"order_sn_list": order_sn,
                        "response_optional_fields": "item_list,recipient_address,buyer_username,pay_time,actual_shipping_fee,note,payment_method,cod"})
    od = (((rd.get("response") or {}).get("order_list") or [{}]) or [{}])[0]

    inc = {}
    try:
        inc = (_chamar(user_id, "/api/v2/payment/get_escrow_detail",
                       extra={"order_sn": order_sn}).get("response") or {}).get("order_income") or {}
    except ShopeeError:
        inc = {}

    tracking = None
    try:
        tr = _chamar(user_id, "/api/v2/logistics/get_tracking_number", extra={"order_sn": order_sn})
        tracking = (tr.get("response") or {}).get("tracking_number")
    except ShopeeError:
        tracking = None

    cat = {p["sku"]: p for p in catalogo.todos(user_id) if p.get("sku")}
    cfg_prec = precificacao.obter_config(user_id)
    itens, total_pago, custo_total, tem_custo_all = [], 0.0, 0.0, True
    for it in (od.get("item_list") or []):
        sku = it.get("model_sku") or it.get("item_sku")
        qtd = int(it.get("model_quantity_purchased") or 0)
        pago = float(it.get("model_discounted_price") or it.get("model_original_price") or 0)
        total_pago += pago * qtd
        base = cat.get(sku)
        custo = float(base.get("custo")) if (base and base.get("custo") is not None) else None
        if custo is None:
            tem_custo_all = False
        else:
            custo_total += custo * qtd
        mr = _margem_real_shopee(cfg_prec, pago, custo)
        itens.append({
            "nome": it.get("item_name") or f"#{it.get('item_id')}", "sku": sku, "qtd": qtd,
            "variacao": it.get("model_name"), "preco_pago": round(pago, 2),
            "imagem": (it.get("image_info") or {}).get("image_url"), "custo": custo,
            "tem_cadastro": base is not None,
            "margem_real": mr.get("margem_pct") if mr else None,
            "lucro_real": mr.get("lucro") if mr else None,
            "liquido": mr.get("liquido") if mr else None, "taxas_mkt": mr.get("taxas") if mr else None,
        })

    tem_escrow = bool(inc)
    receita = float(inc.get("buyer_total_amount") or 0) or round(total_pago, 2)
    comissao = float(inc.get("commission_fee") or 0)
    servico = float(inc.get("service_fee") or 0)
    transacao = float(inc.get("seller_transaction_fee") or inc.get("transaction_fee") or 0)
    frete = float(inc.get("actual_shipping_fee") or od.get("actual_shipping_fee") or 0) - float(inc.get("shopee_shipping_rebate") or 0)
    liquido = float(inc.get("escrow_amount") or 0) if tem_escrow else None
    lucro = round(liquido - custo_total, 2) if (tem_escrow and tem_custo_all) else None
    financeiro = {
        "tem_escrow": tem_escrow, "receita": round(receita, 2),
        "comissao": round(comissao, 2), "servico": round(servico, 2), "transacao": round(transacao, 2),
        "taxas": round(comissao + servico + transacao, 2), "frete": round(frete, 2),
        "liquido": round(liquido, 2) if liquido is not None else None,
        "custo": round(custo_total, 2), "custo_completo": tem_custo_all,
        "lucro": lucro, "margem_pct": round(lucro / receita * 100, 1) if (lucro is not None and receita) else None,
    }
    rec = od.get("recipient_address") or {}
    return {
        "order_sn": od.get("order_sn") or order_sn, "status": od.get("order_status"),
        "criado": od.get("create_time"), "pago_em": od.get("pay_time"), "ship_by": od.get("ship_by_date"),
        "comprador": od.get("buyer_username") or rec.get("name") or "—",
        "endereco": {"nome": rec.get("name"), "telefone": rec.get("phone"),
                     "cidade": rec.get("city"), "uf": rec.get("state"),
                     "cep": rec.get("zipcode"), "completo": rec.get("full_address")},
        "logistica": {"transportadora": od.get("shipping_carrier"), "rastreio": tracking},
        "pagamento": od.get("payment_method"), "nota_comprador": od.get("note") or "", "cod": bool(od.get("cod")),
        "itens": itens, "total_pago": round(total_pago, 2), "financeiro": financeiro,
    }


def enriquecer_impressao(user_id: int, order_sns: list, skus: list | None = None) -> dict:
    """Enriquecimento sob demanda para a impressão (etiqueta/folha): para os pedidos
    selecionados busca rastreio (tracking_number) + dados da NF-e (casada pelo
    numeroPedidoLoja); para os SKUs, a descrição complementar (Bling, cacheada).
    O front mescla isso nos pedidos antes de abrir a janela de impressão.
    Retorna {"patches": {order_sn: {...}}, "complementos": {sku: texto}}."""
    from . import catalogo, nfe as nfe_mod
    sns = [str(s) for s in (order_sns or []) if s][:60]
    patches: dict = {}
    if sns:
        try:
            mapa_nfe = nfe_mod.nfe_por_pedidos(user_id, sns)  # um scan cacheado por usuário
        except Exception:  # noqa: BLE001
            mapa_nfe = {}
        for sn in sns:
            patch: dict = {}
            try:
                tr = _chamar(user_id, "/api/v2/logistics/get_tracking_number",
                             extra={"order_sn": sn})
                rastreio = (tr.get("response") or {}).get("tracking_number")
                if rastreio:
                    patch["rastreio"] = rastreio
            except ShopeeError:
                pass
            info = mapa_nfe.get(sn)
            if info:
                for k, v in info.items():
                    if v not in (None, ""):
                        patch[k] = v
            if patch:
                patches[sn] = patch
    complementos: dict = {}
    vistos: set = set()
    for sku in (skus or []):
        s = str(sku or "").strip()
        if not s or s in vistos:
            continue
        vistos.add(s)
        if len(vistos) > 400:
            break
        try:
            dc = catalogo.descricao_complementar(user_id, s)
        except Exception:  # noqa: BLE001
            dc = ""
        if dc:
            complementos[s] = dc
    return {"patches": patches, "complementos": complementos}


# ============================================================================
# MÓDULO 5 — Documento de envio oficial da Shopee (waybill/etiqueta em PDF)
# Fluxo: resolver tipo -> create_shipping_document -> get_result (poll) -> download (PDF)
# ============================================================================
_DOC_ENVIO_TIPO_PADRAO = "THERMAL_AIR_WAYBILL"


def _chamar_binario(user_id: int, path: str, extra: dict | None = None,
                    metodo: str = "POST", timeout: int = 45) -> bytes:
    """Igual ao _chamar, mas devolve o corpo BINÁRIO (ex.: PDF do download_shipping_document).
    Se a Shopee responder JSON (caso de erro), levanta ShopeeError com a mensagem."""
    if not app_configurado():
        raise ShopeeError("App Shopee não configurado no servidor.")
    access_token, shop_id = _token_valido(user_id)
    ts = int(time.time())
    sign = _sign_loja(path, ts, access_token, shop_id)
    params = {"partner_id": int(settings.shopee_partner_id), "timestamp": ts,
              "access_token": access_token, "shop_id": int(shop_id), "sign": sign}
    url = f"{settings.shopee_base_url}{path}"
    try:
        if metodo == "GET":
            r = requests.get(url, params={**params, **(extra or {})}, timeout=timeout)
        else:
            r = requests.post(url, params=params, json=(extra or {}), timeout=timeout)
    except requests.RequestException as e:
        raise ShopeeError(f"Falha na chamada Shopee: {e}")
    ct = (r.headers.get("content-type") or "").lower()
    if "application/json" in ct or "text/" in ct or (r.content[:1] in (b"{", b"[")):
        try:
            d = r.json()
        except ValueError:
            d = {}
        if isinstance(d, dict) and d.get("error"):
            if "token" in str(d.get("error", "")).lower():
                renovar_token(user_id)
            raise ShopeeError(f"{d.get('error')}: {d.get('message')}")
        raise ShopeeError("Resposta inesperada da Shopee ao baixar o documento.")
    if not r.content:
        raise ShopeeError("Documento vazio retornado pela Shopee.")
    return r.content


def tipo_documento_sugerido(user_id: int, order_sn: str) -> str:
    """Pergunta à Shopee qual tipo de waybill o pedido aceita (preferindo o térmico).
    Cai no padrão térmico se a consulta não retornar nada utilizável."""
    try:
        r = _chamar(user_id, "/api/v2/logistics/get_shipping_document_parameter",
                    extra={"order_sn": order_sn})
        resp = r.get("response") or {}
        sug = resp.get("suggest_shipping_document_type")
        if sug:
            return sug
        infos = (resp.get("selectable_shipping_document_type")
                 or resp.get("shipping_document_info") or resp.get("info_list") or [])
        tipos = []
        for x in infos:
            t = x.get("shipping_document_type") if isinstance(x, dict) else x
            if t:
                tipos.append(t)
        for t in tipos:
            if "THERMAL" in str(t).upper():
                return t
        if tipos:
            return tipos[0]
    except ShopeeError:
        pass
    return _DOC_ENVIO_TIPO_PADRAO


def criar_documento_envio(user_id: int, order_sns: list, tipo: str,
                          rastreios: dict | None = None) -> dict:
    """Dispara a geração do(s) waybill(s) na Shopee (create_shipping_document)."""
    lista = []
    for sn in order_sns:
        item = {"order_sn": sn, "shipping_document_type": tipo}
        if rastreios and rastreios.get(sn):
            item["tracking_number"] = rastreios[sn]
        lista.append(item)
    return _chamar(user_id, "/api/v2/logistics/create_shipping_document",
                   extra={"order_list": lista}, metodo="POST")


def resultado_documento_envio(user_id: int, order_sns: list, tipo: str) -> dict:
    """Status da geração por pedido: {order_sn: {status, erro}} (READY/PROCESSING/FAILED)."""
    lista = [{"order_sn": sn, "shipping_document_type": tipo} for sn in order_sns]
    r = _chamar(user_id, "/api/v2/logistics/get_shipping_document_result",
                extra={"order_list": lista}, metodo="POST")
    out = {}
    for x in ((r.get("response") or {}).get("result_list") or []):
        out[x.get("order_sn")] = {"status": x.get("status"),
                                  "erro": x.get("fail_message") or x.get("fail_error") or ""}
    return out


def baixar_documento_envio(user_id: int, order_sns: list, tipo: str) -> bytes:
    """Baixa o PDF do(s) waybill(s) já gerado(s) — binário, combina todos numa folha."""
    lista = [{"order_sn": sn} for sn in order_sns]
    return _chamar_binario(user_id, "/api/v2/logistics/download_shipping_document",
                           extra={"shipping_document_type": tipo, "order_list": lista}, metodo="POST")


def gerar_etiqueta_oficial(user_id: int, order_sns: list, tipo: str = "auto",
                           tentativas: int = 6, intervalo: float = 1.5) -> bytes:
    """Fluxo completo do waybill oficial: garante rastreio, resolve o tipo, cria, espera
    ficar PRONTO e baixa o PDF. Retorna os bytes do PDF (todos os pedidos numa folha)."""
    sns = [str(s) for s in (order_sns or []) if s][:50]
    if not sns:
        raise ShopeeError("Nenhum pedido informado.")
    if not tipo or tipo == "auto":
        tipo = tipo_documento_sugerido(user_id, sns[0])
    # rastreio ajuda a Shopee a casar o pedido; busca o que der (sem bloquear)
    rastreios = {}
    for sn in sns:
        try:
            tr = _chamar(user_id, "/api/v2/logistics/get_tracking_number", extra={"order_sn": sn})
            tn = (tr.get("response") or {}).get("tracking_number")
            if tn:
                rastreios[sn] = tn
        except ShopeeError:
            pass
    criar_documento_envio(user_id, sns, tipo, rastreios)
    prontos: set = set()
    for _ in range(max(1, tentativas)):
        res = resultado_documento_envio(user_id, sns, tipo)
        prontos = {sn for sn, v in res.items() if str(v.get("status") or "").upper() == "READY"}
        falhou = {sn: v.get("erro") for sn, v in res.items() if str(v.get("status") or "").upper() == "FAILED"}
        if falhou:
            msg = "; ".join(f"{sn}: {e}" for sn, e in falhou.items() if e) or "a Shopee recusou a geração"
            raise ShopeeError(f"Etiqueta recusada — {msg}")
        if len(prontos) >= len(sns):
            break
        time.sleep(intervalo)
    if not prontos:
        raise ShopeeError("A Shopee ainda está gerando a etiqueta. Tente de novo em alguns segundos.")
    return baixar_documento_envio(user_id, sorted(prontos), tipo)


def listar_pedidos(user_id: int, dias: int = 7, cursor: str = "", limite: int = 50) -> dict:
    agora = int(time.time())
    return _chamar(user_id, "/api/v2/order/get_order_list",
                   extra={"time_range_field": "create_time", "time_from": agora - dias * 86400,
                          "time_to": agora, "page_size": min(limite, 100), "cursor": cursor})


def detalhe_pedidos(user_id: int, order_sns: list) -> dict:
    return _chamar(user_id, "/api/v2/order/get_order_detail",
                   extra={"order_sn_list": ",".join(order_sns)})


def pedidos_por_dia(user_id: int, dias: int = 8) -> list:
    """Quantos pedidos por dia nos últimos `dias` (hoje primeiro). Usado para detectar
    queda de vendas a partir dos pedidos REAIS, sem depender de snapshots acumulados."""
    agora = int(time.time())
    inicio = agora - int(dias) * 86400
    por_dia = {}
    cursor = ""
    for _ in range(30):  # teto de páginas
        r = _chamar(user_id, "/api/v2/order/get_order_list",
                    extra={"time_range_field": "create_time", "time_from": inicio, "time_to": agora,
                           "page_size": 100, "cursor": cursor, "response_optional_fields": "create_time"})
        resp = r.get("response") or {}
        for o in (resp.get("order_list") or []):
            ct = o.get("create_time")
            if not ct:
                continue
            dia = (agora - int(ct)) // 86400  # 0 = hoje, 1 = ontem, ...
            por_dia[dia] = por_dia.get(dia, 0) + 1
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more") or not cursor:
            break
    return [por_dia.get(d, 0) for d in range(dias)]


def contar_pedidos_horas(user_id: int, horas: int) -> int:
    """Conta pedidos criados nas últimas `horas` horas (paginando até o teto)."""
    agora = int(time.time())
    inicio = agora - int(horas) * 3600
    total, cursor = 0, ""
    for _ in range(10):  # teto de páginas
        r = _chamar(user_id, "/api/v2/order/get_order_list",
                    extra={"time_range_field": "create_time", "time_from": inicio,
                           "time_to": agora, "page_size": 100, "cursor": cursor})
        resp = r.get("response") or {}
        total += len(resp.get("order_list") or [])
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more"):
            break
    return total


_MARGEM_CACHE: dict = {}


def margem_real(user_id: int, dias: int = 7, limite_pedidos: int = 40) -> dict:
    """Margem líquida REAL: para cada pedido recente, cruza o repasse da Shopee (líquido +
    comissão/taxas/frete) com o CUSTO dos produtos (catálogo). Responde 'a venda deu o lucro
    que eu esperava?'. Caro (1 chamada de repasse por pedido) — teto + cache de ~20 min."""
    from . import catalogo
    chave = (user_id, dias, limite_pedidos)
    cache = _MARGEM_CACHE.get(chave)
    if cache and (time.time() - cache["_ts"]) < 1200:
        return {k: v for k, v in cache.items() if k != "_ts"}

    agora = int(time.time())
    inicio = agora - max(1, dias) * 86400
    sns, cursor = [], ""
    for _ in range(8):
        r = _chamar(user_id, "/api/v2/order/get_order_list",
                    extra={"time_range_field": "create_time", "time_from": inicio, "time_to": agora,
                           "page_size": 100, "cursor": cursor, "order_status": "COMPLETED"})
        resp = r.get("response") or {}
        for o in (resp.get("order_list") or []):
            if o.get("order_sn"):
                sns.append(o["order_sn"])
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more") or not cursor:
            break
    parcial = len(sns) > limite_pedidos
    sns = sns[:limite_pedidos]

    custo_por_sku = {p["sku"]: p.get("custo") for p in catalogo.todos(user_id) if p.get("sku")}

    escrows, item_ids = [], set()
    for sn in sns:
        try:
            e = (_chamar(user_id, "/api/v2/payment/get_escrow_detail", extra={"order_sn": sn}).get("response") or {})
        except ShopeeError:
            continue
        inc = e.get("order_income") or {}
        itens = inc.get("items") or e.get("items") or []
        for it in itens:
            if it.get("item_id"):
                item_ids.add(it.get("item_id"))
        escrows.append((sn, inc, itens))

    meta = nomes_itens(user_id, list(item_ids)) if item_ids else {}

    pedidos = []
    for sn, inc, itens in escrows:
        liquido = float(inc.get("escrow_amount") or inc.get("escrow_amount_after_adjustment") or 0)
        receita = float(inc.get("buyer_total_amount") or inc.get("order_original_price") or inc.get("original_price") or 0)
        comissao = float(inc.get("commission_fee") or 0)
        servico = float(inc.get("service_fee") or 0)
        transacao = float(inc.get("seller_transaction_fee") or inc.get("transaction_fee") or 0)
        frete = float(inc.get("actual_shipping_fee") or 0) - float(inc.get("shopee_shipping_rebate") or 0)
        custo_total, det, sem_custo = 0.0, [], False
        for it in itens:
            iid = it.get("item_id")
            q = int(it.get("quantity_purchased") or it.get("amount") or it.get("model_quantity_purchased") or 1)
            sku = (meta.get(iid) or {}).get("sku")
            c = custo_por_sku.get(sku)
            if c is None:
                sem_custo = True
            custo_total += (c or 0) * q
            det.append({"nome": (meta.get(iid) or {}).get("nome") or f"#{iid}", "sku": sku,
                        "qtd": q, "custo_unit": c, "tem_custo": c is not None})
        lucro = liquido - custo_total
        margem = round(lucro / receita * 100, 1) if receita else 0
        pedidos.append({
            "order_sn": sn, "receita": round(receita, 2),
            "taxas": round(comissao + servico + transacao, 2), "comissao": round(comissao, 2),
            "servico": round(servico, 2), "frete": round(frete, 2),
            "liquido_shopee": round(liquido, 2), "custo": round(custo_total, 2),
            "lucro": round(lucro, 2), "margem_pct": margem, "prejuizo": lucro < 0,
            "sem_custo": sem_custo, "itens": det})

    n = len(pedidos)
    receita_total = sum(p["receita"] for p in pedidos)
    taxas_total = sum(p["taxas"] for p in pedidos)
    frete_total = sum(p["frete"] for p in pedidos)
    custo_total = sum(p["custo"] for p in pedidos)
    lucro_total = sum(p["lucro"] for p in pedidos)
    prejuizo = sum(1 for p in pedidos if p["prejuizo"])
    com_custo = [p for p in pedidos if not p["sem_custo"]]
    out = {
        "periodo_dias": dias, "parcial": parcial,
        "pedidos": sorted(pedidos, key=lambda x: x["margem_pct"]),  # piores margens primeiro
        "resumo": {
            "pedidos": n, "receita_total": round(receita_total, 2), "taxas_total": round(taxas_total, 2),
            "frete_total": round(frete_total, 2), "custo_total": round(custo_total, 2),
            "lucro_liquido_total": round(lucro_total, 2),
            "margem_media_pct": round(lucro_total / receita_total * 100, 1) if receita_total else 0,
            "pedidos_prejuizo": prejuizo,
            "pct_taxas": round(taxas_total / receita_total * 100, 1) if receita_total else 0,
            "sem_custo": sum(1 for p in pedidos if p["sem_custo"]),
            "cobertura_custo": len(com_custo),
        },
    }
    _MARGEM_CACHE[chave] = {**out, "_ts": time.time()}
    return out


def repasse_pedido(user_id: int, order_sn: str) -> dict:
    """Escrow: valor líquido recebido, comissões e taxas (margem real)."""
    return _chamar(user_id, "/api/v2/payment/get_escrow_detail", extra={"order_sn": order_sn})


# ------------------------- Promoções: descontos --------------------------- #
def listar_descontos(user_id: int, status: str = "ongoing", limite: int = 50) -> dict:
    """status: upcoming | ongoing | expired."""
    return _chamar(user_id, "/api/v2/discount/get_discount_list",
                   extra={"discount_status": status, "page_size": min(limite, 100)})


def _enriquecer_itens(user_id: int, ids: list) -> dict:
    if not ids:
        return {}
    try:
        return nomes_itens(user_id, ids)
    except ShopeeError:
        return {}


def detalhe_desconto(user_id: int, discount_id) -> dict:
    """Detalhe de uma campanha de desconto JÁ com os produtos (nome, imagem, preço de/por)."""
    r = _chamar(user_id, "/api/v2/discount/get_discount",
                extra={"discount_id": int(discount_id), "page_no": 1, "page_size": 100})
    resp = r.get("response") or {}
    itens = resp.get("item_list") or []
    meta = _enriquecer_itens(user_id, [it.get("item_id") for it in itens if it.get("item_id")])
    out = []
    for it in itens:
        iid = it.get("item_id")
        m = meta.get(iid) or {}
        models = it.get("model_list") or []
        promo = [mm.get("model_promotion_price") for mm in models if mm.get("model_promotion_price")]
        orig = [mm.get("model_original_price") or mm.get("model_normal_price")
                for mm in models if (mm.get("model_original_price") or mm.get("model_normal_price"))]
        po = min(promo) if promo else it.get("item_promotion_price")
        oo = min(orig) if orig else it.get("item_original_price")
        desc = round((1 - po / oo) * 100) if (po and oo and oo > 0) else None
        out.append({"item_id": iid, "nome": m.get("nome") or it.get("item_name") or f"#{iid}",
                    "imagem": m.get("imagem"), "preco_promo": po, "preco_original": oo,
                    "desconto_pct": desc, "variacoes": len(models)})
    return {"tipo": "desconto", "id": discount_id, "nome": resp.get("discount_name"),
            "inicio": resp.get("start_time"), "fim": resp.get("end_time"),
            "status": resp.get("status"), "itens": out, "total_itens": len(out)}


def detalhe_bundle(user_id: int, bundle_id) -> dict:
    """Detalhe de um bundle (combo) com a regra e os produtos."""
    r = _chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal",
                extra={"bundle_deal_id": int(bundle_id)})
    resp = r.get("response") or {}
    try:
        ri = _chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal_item",
                     extra={"bundle_deal_id": int(bundle_id)})
        itens_raw = (ri.get("response") or {}).get("item_list") or []
    except ShopeeError:
        itens_raw = resp.get("item_list") or []
    ids = [it.get("item_id") for it in itens_raw if it.get("item_id")]
    meta = _enriquecer_itens(user_id, ids)
    itens = [{"item_id": i, "nome": (meta.get(i) or {}).get("nome") or f"#{i}",
              "imagem": (meta.get(i) or {}).get("imagem")} for i in ids]
    regra = resp.get("bundle_deal_rule") or {}
    return {"tipo": "bundle", "id": bundle_id, "nome": resp.get("name"),
            "inicio": resp.get("start_time"), "fim": resp.get("end_time"),
            "status": resp.get("bundle_deal_status") or resp.get("status"),
            "regra": {"rule_type": regra.get("rule_type"), "valor": regra.get("discount_value"),
                      "min_itens": regra.get("min_amount")},
            "itens": itens, "total_itens": len(itens)}


def detalhe_addon(user_id: int, addon_id) -> dict:
    """Detalhe de um add-on: produto(s) principal(is) + adicionais com preço promocional."""
    base = {}
    try:
        rb = _chamar(user_id, "/api/v2/add_on_deal/get_add_on_deal",
                     extra={"add_on_deal_id": int(addon_id)})
        base = rb.get("response") or {}
    except ShopeeError:
        base = {}
    principais, adicionais = [], []
    try:
        rm = _chamar(user_id, "/api/v2/add_on_deal/get_add_on_deal_main_item",
                     extra={"add_on_deal_id": int(addon_id)})
        principais = (rm.get("response") or {}).get("main_item_list") or []
    except ShopeeError:
        pass
    try:
        rs = _chamar(user_id, "/api/v2/add_on_deal/get_add_on_deal_sub_item",
                     extra={"add_on_deal_id": int(addon_id)})
        adicionais = (rs.get("response") or {}).get("sub_item_list") or []
    except ShopeeError:
        pass
    ids = [x.get("item_id") for x in (principais + adicionais) if x.get("item_id")]
    meta = _enriquecer_itens(user_id, ids)
    def _norm(x, extra=None):
        i = x.get("item_id")
        d = {"item_id": i, "nome": (meta.get(i) or {}).get("nome") or f"#{i}",
             "imagem": (meta.get(i) or {}).get("imagem")}
        if extra:
            d.update(extra)
        return d
    itens_p = [_norm(x) for x in principais]
    itens_s = [_norm(x, {"preco_promo": x.get("add_on_deal_price")}) for x in adicionais]
    return {"tipo": "addon", "id": addon_id, "nome": base.get("add_on_deal_name"),
            "inicio": base.get("start_time"), "fim": base.get("end_time"),
            "status": base.get("status"), "principais": itens_p, "adicionais": itens_s,
            "itens": itens_p + itens_s, "total_itens": len(itens_p) + len(itens_s)}


def detalhe_flash(user_id: int, flash_id) -> dict:
    """Detalhe de uma Flash Sale com os produtos, preço e estoque reservado."""
    base = {}
    try:
        rb = _chamar(user_id, "/api/v2/shop_flash_sale/get_shop_flash_sale",
                     extra={"flash_sale_id": int(flash_id)})
        base = rb.get("response") or {}
    except ShopeeError:
        base = {}
    itens_raw = []
    try:
        ri = _chamar(user_id, "/api/v2/shop_flash_sale/get_shop_flash_sale_items",
                     extra={"flash_sale_id": int(flash_id), "offset": 0, "limit": 100})
        rr = ri.get("response") or {}
        itens_raw = rr.get("item_info") or rr.get("models") or rr.get("item_list") or []
    except ShopeeError:
        pass
    ids = [it.get("item_id") for it in itens_raw if it.get("item_id")]
    meta = _enriquecer_itens(user_id, ids)
    vistos, itens = set(), []
    for it in itens_raw:
        iid = it.get("item_id")
        if iid in vistos:
            continue
        vistos.add(iid)
        m = meta.get(iid) or {}
        itens.append({"item_id": iid, "nome": m.get("nome") or f"#{iid}", "imagem": m.get("imagem"),
                      "preco_promo": it.get("promotion_price_min") or it.get("input_promo_price")
                      or it.get("promotion_price"), "estoque": it.get("campaign_stock") or it.get("stock")})
    return {"tipo": "flash", "id": flash_id, "nome": "Flash Sale",
            "inicio": base.get("start_time"), "fim": base.get("end_time"),
            "status": base.get("status"), "itens": itens, "total_itens": len(itens)}


_DESEMP_CACHE: dict = {}


def _pedidos_itens_periodo(user_id: int, inicio: int, fim: int, max_paginas: int = 12):
    """order_sn no período + itens de cada pedido (lotes de 50). Caro: tem teto de páginas."""
    agora = int(time.time())
    fim = min(int(fim or agora), agora)
    sns, cursor = [], ""
    for _ in range(max_paginas):
        r = _chamar(user_id, "/api/v2/order/get_order_list",
                    extra={"time_range_field": "create_time", "time_from": int(inicio),
                           "time_to": fim, "page_size": 100, "cursor": cursor})
        resp = r.get("response") or {}
        for o in (resp.get("order_list") or []):
            if o.get("order_sn"):
                sns.append(o["order_sn"])
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more") or not cursor:
            break
    parcial = len(sns) >= max_paginas * 100
    pedidos = []
    for i in range(0, len(sns), 50):
        lote = sns[i:i + 50]
        try:
            rd = _chamar(user_id, "/api/v2/order/get_order_detail",
                         extra={"order_sn_list": ",".join(lote),
                                "response_optional_fields": "item_list"})
            pedidos.extend((rd.get("response") or {}).get("order_list") or [])
        except ShopeeError:
            continue
    return pedidos, len(sns), parcial


_VENDAS_SKU_CACHE: dict = {}  # user_id -> (ts, {sku: unidades})


_VENDAS_ITEM_CACHE: dict = {}  # (user_id, dias) -> (ts, {item_id: unidades})
_CAMP_ITENS_CACHE: dict = {}   # user_id -> (ts, set(item_id))


def vendas_por_item(user_id: int, dias: int = 30, ttl: int = 1800) -> dict:
    """Unidades vendidas por item_id (ANÚNCIO) nos últimos `dias`, do histórico de pedidos.
    Agrega por item_id — não por SKU — porque produtos com variação registram a venda no
    model_sku da variação, e o casamento por SKU do anúncio-pai falharia (apareceria 0)."""
    chave = (user_id, int(dias))
    ag = time.time()
    cache = _VENDAS_ITEM_CACHE.get(chave)
    if cache and ag - cache[0] < ttl:
        return cache[1]
    fim = int(ag)
    inicio = fim - int(dias) * 86400
    try:
        pedidos, _, _ = _pedidos_itens_periodo(user_id, inicio, fim, max_paginas=20)
    except ShopeeError:
        pedidos = []
    vendas: dict = {}
    for o in pedidos:
        for it in (o.get("item_list") or []):
            iid = it.get("item_id")
            if not iid:
                continue
            vendas[int(iid)] = vendas.get(int(iid), 0) + int(it.get("model_quantity_purchased") or 0)
    _VENDAS_ITEM_CACHE[chave] = (ag, vendas)
    return vendas


def itens_em_campanha(user_id: int, ttl: int = 600) -> set:
    """item_ids que JÁ estão em campanhas de desconto ATIVAS ou AGENDADAS — não podem
    entrar em outra (a Shopee rejeita). Usado pra não sugerir esses produtos. Cacheia ~10 min."""
    ag = time.time()
    cache = _CAMP_ITENS_CACHE.get(user_id)
    if cache and ag - cache[0] < ttl:
        return cache[1]
    ids: set = set()
    for status in ("ongoing", "upcoming"):
        try:
            r = listar_descontos(user_id, status=status, limite=100)
        except ShopeeError:
            continue
        for d in ((r.get("response") or {}).get("discount_list") or []):
            did = d.get("discount_id")
            if not did:
                continue
            page = 1
            for _ in range(20):
                try:
                    rr = _chamar(user_id, "/api/v2/discount/get_discount",
                                 extra={"discount_id": int(did), "page_no": page, "page_size": 100})
                except ShopeeError:
                    break
                lote = (rr.get("response") or {}).get("item_list") or []
                for it in lote:
                    if it.get("item_id"):
                        ids.add(int(it["item_id"]))
                if len(lote) < 100:
                    break
                page += 1
    _CAMP_ITENS_CACHE[user_id] = (ag, ids)
    return ids


def vendas_por_sku(user_id: int, dias: int = 30, ttl: int = 1800) -> dict:
    """Unidades vendidas por SKU nos últimos `dias`, a partir do histórico de pedidos.
    Usado para achar ESTOQUE PARADO (SKUs sem/poucas vendas). Cacheia ~30 min."""
    ag = time.time()
    cache = _VENDAS_SKU_CACHE.get(user_id)
    if cache and ag - cache[0] < ttl:
        return cache[1]
    fim = int(ag)
    inicio = fim - int(dias) * 86400
    try:
        pedidos, _, _ = _pedidos_itens_periodo(user_id, inicio, fim, max_paginas=12)
    except ShopeeError:
        pedidos = []
    vendas: dict = {}
    for o in pedidos:
        for it in (o.get("item_list") or []):
            sku = it.get("model_sku") or it.get("item_sku")
            if not sku:
                continue
            vendas[sku] = vendas.get(sku, 0) + int(it.get("model_quantity_purchased") or 0)
    _VENDAS_SKU_CACHE[user_id] = (ag, vendas)
    return vendas


def desempenho_campanha(user_id: int, tipo: str, cid) -> dict:
    """Desempenho = vendas dos PRODUTOS da campanha durante o período dela.
    Atribuição precisa quando o item do pedido carrega o promotion_id da campanha.
    Resultado em cache por ~10 min (a varredura de pedidos é cara)."""
    chave = (user_id, tipo, str(cid))
    cache = _DESEMP_CACHE.get(chave)
    if cache and (time.time() - cache["_ts"]) < 600:
        return {k: v for k, v in cache.items() if k != "_ts"}

    det_fn = {"desconto": detalhe_desconto, "bundle": detalhe_bundle,
              "addon": detalhe_addon, "flash": detalhe_flash}.get(tipo)
    if not det_fn:
        return {"indisponivel": True, "motivo": "tipo desconhecido"}
    d = det_fn(user_id, cid)
    inicio, fim = d.get("inicio"), d.get("fim")
    agora = int(time.time())
    if not inicio:
        return {"indisponivel": True, "motivo": "campanha sem período definido"}
    if inicio > agora:
        return {"indisponivel": True, "motivo": "a campanha ainda não começou"}
    produtos = {p["item_id"] for p in d.get("itens", []) if p.get("item_id")}
    if not produtos:
        return {"indisponivel": True, "motivo": "campanha sem produtos vinculados"}

    pedidos, total_periodo, parcial = _pedidos_itens_periodo(user_id, inicio, fim or agora)
    unidades = receita = pedidos_com = atribuidos = 0
    for o in pedidos:
        tem = False
        for it in (o.get("item_list") or []):
            if it.get("item_id") in produtos:
                q = int(it.get("model_quantity_purchased") or it.get("quantity_purchased") or 0)
                preco = float(it.get("model_discounted_price") or it.get("discounted_price")
                              or it.get("model_original_price") or 0)
                unidades += q
                receita += preco * q
                tem = True
                if str(it.get("promotion_id") or "") == str(cid):
                    atribuidos += q
        if tem:
            pedidos_com += 1
    out = {"pedidos_com_produto": pedidos_com, "unidades": unidades, "receita": round(receita, 2),
           "atribuido_promo": atribuidos, "pedidos_no_periodo": total_periodo, "parcial": parcial,
           "ticket_medio": round(receita / pedidos_com, 2) if pedidos_com else 0,
           "janela_inicio": inicio, "janela_fim": min(fim or agora, agora)}
    _DESEMP_CACHE[chave] = {**out, "_ts": time.time()}
    return out


_DASH_CACHE: dict = {}


def agenda_campanhas(user_id: int) -> dict:
    """Lista TODAS as campanhas (todos os tipos) normalizadas, pra visão geral e timeline. Barato."""
    out = []
    def _add(tipo, cid, nome, ini, fim):
        if cid is not None:
            out.append({"tipo": tipo, "id": cid, "nome": nome, "inicio": ini, "fim": fim})
    for st in ("upcoming", "ongoing"):
        try:
            for c in ((listar_descontos(user_id, st).get("response") or {}).get("discount_list") or []):
                _add("desconto", c.get("discount_id"), c.get("discount_name"), c.get("start_time"), c.get("end_time"))
        except ShopeeError:
            pass
        try:
            for c in ((listar_cupons(user_id, st).get("response") or {}).get("voucher_list") or []):
                _add("cupom", c.get("voucher_id"), c.get("voucher_name"), c.get("start_time"), c.get("end_time"))
        except ShopeeError:
            pass
        try:
            for c in ((listar_bundles(user_id, st).get("response") or {}).get("bundle_deal_list") or []):
                _add("bundle", c.get("bundle_deal_id"), c.get("name"), c.get("start_time"), c.get("end_time"))
        except ShopeeError:
            pass
        try:
            for c in ((listar_addons(user_id, st).get("response") or {}).get("add_on_deal_list") or []):
                _add("addon", c.get("add_on_deal_id"), c.get("add_on_deal_name"), c.get("start_time"), c.get("end_time"))
        except ShopeeError:
            pass
    for tp in (1, 2):  # flash: upcoming + ongoing
        try:
            fr = listar_flash(user_id, tp).get("response") or {}
            for c in (fr.get("flash_sale_list") or (fr if isinstance(fr, list) else [])):
                _add("flash", c.get("flash_sale_id"), f"Flash #{c.get('flash_sale_id')}", c.get("start_time"), c.get("end_time"))
        except ShopeeError:
            pass
    # dedup por (tipo,id)
    vistos, limpo = set(), []
    for c in out:
        k = (c["tipo"], c["id"])
        if k not in vistos:
            vistos.add(k)
            limpo.append(c)
    return {"campanhas": limpo, "total": len(limpo)}


def _tipo_promo(ptype_raw, pid, nomes):
    p = (ptype_raw or "").lower()
    if "flash" in p:
        return "flash"
    if "bundle" in p:
        return "bundle"
    if "add" in p:
        return "addon"
    if "voucher" in p or "coupon" in p or "cupom" in p:
        return "cupom"
    if "discount" in p or "price" in p:
        return "desconto"
    return (nomes.get(str(pid)) or {}).get("tipo")


def dashboard_promo(user_id: int, dias: int = 30) -> dict:
    """Receita gerada por promoções: UMA varredura de pedidos, atribuindo cada venda à
    campanha pelo promotion_id/promotion_type do item. Caro — cache ~20 min."""
    chave = (user_id, dias)
    cache = _DASH_CACHE.get(chave)
    if cache and (time.time() - cache["_ts"]) < 1200:
        return {k: v for k, v in cache.items() if k != "_ts"}

    agora = int(time.time())
    nomes = {}
    try:
        for c in agenda_campanhas(user_id)["campanhas"]:
            nomes[str(c["id"])] = {"tipo": c["tipo"], "nome": c["nome"]}
    except ShopeeError:
        pass
    for st in ("expired",):  # nomeia campanhas que rodaram e já encerraram no período
        try:
            for c in ((listar_descontos(user_id, st).get("response") or {}).get("discount_list") or []):
                nomes.setdefault(str(c.get("discount_id")), {"tipo": "desconto", "nome": c.get("discount_name")})
        except ShopeeError:
            pass

    pedidos, total_pedidos, parcial = _pedidos_itens_periodo(user_id, agora - max(1, dias) * 86400, agora, max_paginas=15)
    por_campanha, por_tipo = {}, {}
    tot_receita = tot_unid = 0.0
    pedidos_promo = set()
    for o in pedidos:
        osn = o.get("order_sn")
        for it in (o.get("item_list") or []):
            pid = str(it.get("promotion_id") or "")
            tipo = _tipo_promo(it.get("promotion_type"), pid, nomes)
            tem_promo = (pid and pid != "0") or tipo is not None
            if not tem_promo:
                continue
            q = int(it.get("model_quantity_purchased") or it.get("quantity_purchased") or 0)
            preco = float(it.get("model_discounted_price") or it.get("discounted_price") or it.get("model_original_price") or 0)
            val = preco * q
            tot_receita += val
            tot_unid += q
            pedidos_promo.add(osn)
            tipo = tipo or "outras"
            t = por_tipo.setdefault(tipo, {"receita": 0.0, "unidades": 0, "pedidos": set()})
            t["receita"] += val
            t["unidades"] += q
            t["pedidos"].add(osn)
            if pid and pid != "0":
                meta = nomes.get(pid) or {"tipo": tipo, "nome": None}
                c = por_campanha.setdefault(pid, {"id": pid, "tipo": meta.get("tipo") or tipo,
                                                  "nome": meta.get("nome"), "receita": 0.0, "unidades": 0, "pedidos": set()})
                c["receita"] += val
                c["unidades"] += q
                c["pedidos"].add(osn)

    por_tipo_list = sorted(
        [{"tipo": k, "receita": round(v["receita"], 2), "unidades": v["unidades"], "pedidos": len(v["pedidos"])}
         for k, v in por_tipo.items()], key=lambda x: -x["receita"])
    top = sorted(
        [{"id": c["id"], "tipo": c["tipo"], "nome": c["nome"], "receita": round(c["receita"], 2),
          "unidades": c["unidades"], "pedidos": len(c["pedidos"])} for c in por_campanha.values()],
        key=lambda x: -x["receita"])[:8]
    out = {"periodo_dias": dias, "parcial": parcial,
           "total": {"receita": round(tot_receita, 2), "unidades": tot_unid, "pedidos": len(pedidos_promo)},
           "por_tipo": por_tipo_list, "top_campanhas": top, "pedidos_no_periodo": total_pedidos}
    _DASH_CACHE[chave] = {**out, "_ts": time.time()}
    return out


def repetir_campanha(user_id: int, tipo: str, cid) -> dict:
    """Recria uma campanha igual, com um novo período (mesma duração, começando em ~5 min).
    Suporta desconto, bundle e addon (flash depende de slot; cupom é recriado pela tela)."""
    agora = int(time.time())
    inicio = agora + 300
    if tipo == "desconto":
        r = _chamar(user_id, "/api/v2/discount/get_discount",
                    extra={"discount_id": int(cid), "page_no": 1, "page_size": 100})
        resp = r.get("response") or {}
        itens = []
        for it in (resp.get("item_list") or []):
            models = [{"model_id": m.get("model_id"), "model_promotion_price": m.get("model_promotion_price")}
                      for m in (it.get("model_list") or []) if m.get("model_promotion_price")]
            if not models:
                ip = it.get("item_promotion_price")
                if ip:
                    models = [{"model_id": 0, "model_promotion_price": ip}]
            if models:
                itens.append({"item_id": it.get("item_id"),
                              "purchase_limit": it.get("purchase_limit", 0), "model_list": models})
        if not itens:
            raise ShopeeError("A campanha original não tem produtos para repetir.")
        dur = (resp.get("end_time") or 0) - (resp.get("start_time") or 0)
        fim = inicio + max(3600, dur or 3 * 86400)
        nome = (resp.get("discount_name") or "Desconto")[:24] + " (repetida)"
        out = criar_desconto(user_id, nome, inicio, fim, itens)
        return {"tipo": "desconto", "novo_id": (out.get("response") or {}).get("discount_id"),
                "itens": out.get("itens_adicionados", len(itens))}

    if tipo == "bundle":
        det = detalhe_bundle(user_id, cid)
        ids = [p["item_id"] for p in det.get("itens", [])]
        if not ids:
            raise ShopeeError("O combo original não tem produtos para repetir.")
        regra = det.get("regra") or {}
        dur = (det.get("fim") or 0) - (det.get("inicio") or 0)
        fim = inicio + max(3600, dur or 7 * 86400)
        out = criar_bundle(user_id, (det.get("nome") or "Combo")[:24] + " (rep.)", inicio, fim,
                           int(regra.get("rule_type") or 2), float(regra.get("valor") or 0),
                           int(regra.get("min_itens") or 2), ids)
        return {"tipo": "bundle", "novo_id": (out.get("response") or {}).get("bundle_deal_id"),
                "itens": out.get("itens_adicionados", len(ids))}

    if tipo == "addon":
        det = detalhe_addon(user_id, cid)
        principais = [p["item_id"] for p in det.get("principais", [])]
        adicionais = [{"item_id": p["item_id"], "add_on_deal_price": p.get("preco_promo") or 0}
                      for p in det.get("adicionais", [])]
        if not principais:
            raise ShopeeError("O add-on original não tem produto principal para repetir.")
        dur = (det.get("fim") or 0) - (det.get("inicio") or 0)
        fim = inicio + max(3600, dur or 7 * 86400)
        out = criar_addon(user_id, (det.get("nome") or "Add-on")[:24] + " (rep.)", inicio, fim,
                          principais, adicionais)
        return {"tipo": "addon", "novo_id": (out.get("response") or {}).get("add_on_deal_id"),
                "itens": out.get("principais_ok", len(principais))}

    raise ShopeeError(f"Repetir ainda não é suportado para o tipo '{tipo}'.")


_ERR_DESC = {
    "discount": "produto já está em outra promoção de desconto ativa (não pode entrar em duas ao mesmo tempo)",
    "already": "produto já está em outra promoção ativa",
    "promotion_overlap": "produto já está em outra promoção no mesmo período",
    "price": "preço de promoção inválido (precisa ser menor que o preço atual e dentro do limite da Shopee)",
    "lower": "o preço de promoção precisa ser menor que o preço atual",
    "model": "variação (model_id) inválida ou inexistente para este anúncio",
    "not_exist": "anúncio não existe ou não está mais disponível",
    "status": "anúncio não está ativo/elegível para desconto",
    "stock": "estoque insuficiente para entrar na promoção",
}


def _traduzir_erro_item(msg: str) -> str:
    m = (msg or "").lower()
    for chave, texto in _ERR_DESC.items():
        if chave in m:
            return texto
    return msg or "motivo não informado pela Shopee"


def criar_desconto(user_id: int, nome: str, inicio: int, fim: int, itens: list) -> dict:
    """Cria uma campanha de desconto na Shopee. São DOIS passos na API v2:
    1) add_discount cria a campanha (nome + datas) e devolve discount_id;
    2) add_discount_item anexa os produtos (o add_discount NÃO aceita itens).
    Sem o passo 2 a promoção nasce sem produtos. Anexa em lotes de 50."""
    r = _chamar(user_id, "/api/v2/discount/add_discount", metodo="POST",
                extra={"discount_name": nome, "start_time": inicio, "end_time": fim})
    did = (r.get("response") or {}).get("discount_id")
    out = {"response": {"discount_id": did}, "itens_adicionados": 0, "item_erros": [], "enviados": len(itens)}
    if not did or not itens:
        if not did:
            msg = r.get("message") or r.get("error") or "a Shopee não retornou discount_id ao criar a campanha"
            out["item_erros"].append(str(msg))
        return out
    adicionados = 0
    for i in range(0, len(itens), 50):  # add_discount_item aceita até 50 por chamada
        lote = itens[i:i + 50]
        ri = _chamar(user_id, "/api/v2/discount/add_discount_item", metodo="POST",
                     extra={"discount_id": did, "item_list": lote})
        # erro no nível da chamada toda (ex.: discount inválido, sem permissão)
        if ri.get("error") and not ri.get("response"):
            out["item_erros"].append(_traduzir_erro_item(ri.get("message") or ri.get("error")))
            continue
        resp_i = ri.get("response") or {}
        erros = resp_i.get("error_list") or resp_i.get("fail_list") or []
        # quantos entraram: usa o count da Shopee se vier, senão deduz pela diferença
        count = resp_i.get("count")
        if isinstance(count, int):
            adicionados += count
        else:
            adicionados += max(0, len(lote) - len(erros))
        for e in erros:
            if isinstance(e, dict):
                raw = e.get("fail_error") or e.get("fail_message") or e.get("error") or e.get("message")
                out["item_erros"].append(f"anúncio {e.get('item_id')}: {_traduzir_erro_item(raw)}")
            elif e:
                out["item_erros"].append(_traduzir_erro_item(str(e)))
    out["itens_adicionados"] = adicionados
    return out


def add_discount_item(user_id: int, discount_id, itens: list) -> dict:
    """Anexa/atualiza produtos numa campanha de desconto existente (lotes de 50)."""
    adicionados, item_erros = 0, []
    for i in range(0, len(itens), 50):
        lote = itens[i:i + 50]
        ri = _chamar(user_id, "/api/v2/discount/add_discount_item", metodo="POST",
                     extra={"discount_id": int(discount_id), "item_list": lote})
        resp_i = ri.get("response") or {}
        erros = resp_i.get("error_list") or resp_i.get("fail_list") or []
        adicionados += len(lote) - len(erros)
        item_erros.extend(erros)
    return {"discount_id": discount_id, "itens_adicionados": adicionados, "item_erros": item_erros}


def _preco_modelo(m: dict) -> float:
    """Extrai o preço atual de um modelo do get_model_list, tolerando price_info como
    objeto {current_price,...} OU lista [{current_price,...}] (varia por versão da API)."""
    pi = m.get("price_info")
    if isinstance(pi, list):
        pi = pi[0] if pi else {}
    if not isinstance(pi, dict):
        pi = {}
    for k in ("current_price", "promotion_price", "selling_price", "original_price", "price"):
        v = pi.get(k) or m.get(k)
        if v:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def itens_desconto_por_pct(user_id: int, itens: list) -> list:
    """Transforma [{item_id, desconto_pct, preco?, purchase_limit?}] no item_list do add_discount,
    aplicando o desconto ao preço de CADA variação (modelo). O model_promotion_price é sempre
    forçado a ficar ABAIXO do preço atual (a Shopee rejeita promo >= preço vigente)."""
    out = []
    for it in itens:
        item_id = int(it["item_id"])
        d = float(it.get("desconto_pct") or 0) / 100.0
        try:
            ml = modelos_item(user_id, item_id)
        except ShopeeError:
            ml = []
        model_list = []
        for m in ml:
            preco_m = _preco_modelo(m)
            if preco_m <= 0:
                continue
            promo = round(preco_m * (1 - d), 2)
            if promo >= preco_m:                       # garante que fica abaixo do preço atual
                promo = round(preco_m - 0.01, 2)
            if promo <= 0:
                continue
            model_list.append({"model_id": m.get("model_id"), "model_promotion_price": promo})
        if not model_list:
            # sem variação (anúncio simples): preço no nível do ITEM (model_id 0 é rejeitado por alguns anúncios)
            preco = float(it.get("preco") or 0)
            if preco > 0:
                promo = round(preco * (1 - d), 2)
                if promo >= preco:
                    promo = round(preco - 0.01, 2)
                if promo > 0:
                    out.append({"item_id": item_id, "purchase_limit": int(it.get("purchase_limit") or 0),
                                "item_promotion_price": promo})
            continue
        out.append({"item_id": item_id, "purchase_limit": int(it.get("purchase_limit") or 0),
                    "model_list": model_list})
    return out


def encerrar_desconto(user_id: int, discount_id) -> dict:
    return _chamar(user_id, "/api/v2/discount/end_discount", metodo="POST",
                   extra={"discount_id": int(discount_id)})


# ------------------------- Promoções: cupons ------------------------------ #
def listar_cupons(user_id: int, status: str = "ongoing", limite: int = 50) -> dict:
    return _chamar(user_id, "/api/v2/voucher/get_voucher_list",
                   extra={"status": status, "page_size": min(limite, 100)})


def criar_cupom(user_id: int, nome: str, codigo: str, inicio: int, fim: int,
                tipo_desconto: int, valor: float, compra_minima: float,
                quantidade: int, escopo: int = 1) -> dict:
    """Cria um cupom. tipo_desconto: 1=valor fixo, 2=percentual. escopo: 1=loja, 2=produto."""
    corpo = {"voucher_name": nome, "voucher_code": codigo, "start_time": inicio,
             "end_time": fim, "voucher_type_id": escopo, "reward_type": tipo_desconto,
             "usage_quantity": quantidade, "min_basket_price": compra_minima}
    if tipo_desconto == 1:
        corpo["discount_amount"] = valor
    else:
        corpo["percentage"] = int(valor)
    return _chamar(user_id, "/api/v2/voucher/add_voucher", metodo="POST", extra=corpo)


def encerrar_cupom(user_id: int, voucher_id) -> dict:
    return _chamar(user_id, "/api/v2/voucher/end_voucher", metodo="POST",
                   extra={"voucher_id": int(voucher_id)})


# ------------------------------ Shopee Ads -------------------------------- #
def ads_saldo(user_id: int) -> dict:
    return _chamar(user_id, "/api/v2/ads/get_total_balance")


def ads_desempenho(user_id: int, dias: int = 7) -> dict:
    agora = int(time.time())
    return _chamar(user_id, "/api/v2/ads/get_all_cpc_ads_hourly_performance",
                   extra={"performance_type": "daily", "start_date": agora - dias * 86400,
                          "end_date": agora})


# ------------------------- Perguntas no anúncio (Q&A) --------------------- #
def listar_perguntas(user_id: int, status: str = "UNANSWERED", limite: int = 20) -> dict:
    return _chamar(user_id, "/api/v2/sip/get_item_qa_list",
                   extra={"qa_status": status, "page_size": min(limite, 100)})


def responder_pergunta(user_id: int, qa_id, texto: str) -> dict:
    return _chamar(user_id, "/api/v2/sip/answer_item_qa", metodo="POST",
                   extra={"qa_id": int(qa_id), "answer": texto})


# ------------------------------ Devoluções -------------------------------- #
def listar_devolucoes(user_id: int, dias: int = 30, limite: int = 50) -> dict:
    agora = int(time.time())
    return _chamar(user_id, "/api/v2/returns/get_return_list",
                   extra={"create_time_from": agora - dias * 86400, "create_time_to": agora,
                          "page_size": min(limite, 100)})


def detalhe_devolucao(user_id: int, return_sn: str) -> dict:
    return _chamar(user_id, "/api/v2/returns/get_return_detail",
                   extra={"return_sn": return_sn})


# ---------------------- Divergência Bling × Shopee ------------------------ #
def _preco_item(info: dict) -> float:
    """Extrai o preço atual de um item da Shopee (estrutura aninhada)."""
    pl = info.get("price_info") or []
    if isinstance(pl, list) and pl:
        return float(pl[0].get("current_price") or pl[0].get("original_price") or 0)
    return float(info.get("current_price") or 0)


def catalogo_shopee(user_id: int, paginas: int = 5) -> list:
    """Lista anúncios da Shopee com preço e SKU (até N páginas de 50)."""
    out = []
    offset = 0
    for _ in range(paginas):
        r = listar_itens(user_id, offset=offset, limite=50)
        resp = r.get("response") or {}
        lista = resp.get("item") or []
        if not lista:
            break
        ids = [it.get("item_id") for it in lista if it.get("item_id")]
        if ids:
            base = (info_itens(user_id, ids).get("response") or {}).get("item_list") or []
            for b in base:
                out.append({"item_id": str(b.get("item_id")), "nome": b.get("item_name"),
                            "sku": b.get("item_sku"), "preco": _preco_item(b),
                            "status": b.get("item_status")})
        if not resp.get("has_next_page"):
            break
        offset = resp.get("next_offset", offset + 50)
    return out


def divergencia_bling_shopee(user_id: int) -> dict:
    """Cruza o preço do anúncio na Shopee com o preço registrado no Bling (cache),
    casando por SKU. Aponta divergências e prejuízo (Shopee < custo Bling)."""
    from .models import ProdutoCache
    db = SessionLocal()
    try:
        cache = {p.sku: p for p in db.query(ProdutoCache).filter_by(user_id=user_id).all() if p.sku}
    finally:
        db.close()
    itens = catalogo_shopee(user_id)
    linhas, sem_match = [], 0
    for it in itens:
        p = cache.get(it["sku"])
        if not p:
            sem_match += 1
            continue
        diff = it["preco"] - (p.preco or 0)
        linhas.append({
            "item_id": it["item_id"], "nome": it["nome"], "sku": it["sku"],
            "preco_shopee": it["preco"], "preco_bling": p.preco, "custo": p.custo,
            "diferenca": round(diff, 2),
            "divergente": abs(diff) > 0.01,
            "prejuizo": bool(it["preco"] > 0 and p.custo and it["preco"] < p.custo),
        })
    return {"total": len(itens), "casados": len(linhas), "sem_match": sem_match,
            "divergentes": sum(1 for l in linhas if l["divergente"]),
            "prejuizo": sum(1 for l in linhas if l["prejuizo"]), "itens": linhas}


# --------------------------- Bundle Deal ---------------------------------- #
def listar_bundles(user_id: int, status: str = "ongoing", limite: int = 50) -> dict:
    """status: ongoing | upcoming | expired."""
    return _chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal_list",
                   extra={"time_status": status, "page_size": min(limite, 100)})


def criar_bundle(user_id: int, nome: str, inicio: int, fim: int, rule_type: int,
                 valor: float, min_itens: int, item_ids: list) -> dict:
    """Cria um bundle (compre N, leve com desconto) e adiciona os itens.
    rule_type: 1=preço fixo do combo, 2=% de desconto, 3=valor de desconto."""
    corpo = {"name": nome, "start_time": inicio, "end_time": fim,
             "bundle_deal_rule": {"rule_type": rule_type, "discount_value": valor,
                                  "min_amount": min_itens, "max_amount": 0}}
    r = _chamar(user_id, "/api/v2/bundle_deal/add_bundle_deal", metodo="POST", extra=corpo)
    bid = (r.get("response") or {}).get("bundle_deal_id")
    out = {"response": {"bundle_deal_id": bid}, "itens_adicionados": 0, "item_erros": []}
    if not bid:
        raise ShopeeError("A Shopee não retornou bundle_deal_id ao criar o combo.")
    if item_ids:
        ri = _chamar(user_id, "/api/v2/bundle_deal/add_bundle_deal_item", metodo="POST",
                     extra={"bundle_deal_id": bid,
                            "item_list": [{"item_id": int(i)} for i in item_ids]})
        resp_i = ri.get("response") or {}
        erros = resp_i.get("error_list") or resp_i.get("fail_list") or []
        out["itens_adicionados"] = len(item_ids) - len(erros)
        out["item_erros"] = erros
    return out


def encerrar_bundle(user_id: int, bundle_deal_id) -> dict:
    return _chamar(user_id, "/api/v2/bundle_deal/end_bundle_deal", metodo="POST",
                   extra={"bundle_deal_id": int(bundle_deal_id)})


# --------------------------- Add-on Deal ---------------------------------- #
def listar_addons(user_id: int, status: str = "ongoing", limite: int = 50) -> dict:
    return _chamar(user_id, "/api/v2/add_on_deal/get_add_on_deal_list",
                   extra={"promotion_status": status, "page_size": min(limite, 100)})


def criar_addon(user_id: int, nome: str, inicio: int, fim: int,
                principais: list, adicionais: list, promotion_type: int = 0) -> dict:
    """Add-on: na compra do produto principal, leva os adicionais com desconto.
    promotion_type: 0 = add-on com desconto; 1 = brinde por valor mínimo.
    adicionais: [{item_id, add_on_deal_price}]."""
    corpo = {"add_on_deal_name": nome, "start_time": inicio, "end_time": fim,
             "promotion_type": promotion_type}
    r = _chamar(user_id, "/api/v2/add_on_deal/add_add_on_deal", metodo="POST", extra=corpo)
    aid = (r.get("response") or {}).get("add_on_deal_id")
    out = {"response": {"add_on_deal_id": aid}, "principais_ok": 0, "adicionais_ok": 0, "item_erros": []}
    if not aid:
        raise ShopeeError("A Shopee não retornou add_on_deal_id ao criar o add-on.")
    if principais:
        rp = _chamar(user_id, "/api/v2/add_on_deal/add_add_on_deal_main_item", metodo="POST",
                     extra={"add_on_deal_id": aid,
                            "main_item_list": [{"item_id": int(i), "status": 1} for i in principais]})
        ep = (rp.get("response") or {}).get("error_list") or []
        out["principais_ok"] = len(principais) - len(ep)
        out["item_erros"] += [f"principal {e.get('item_id')}: {e.get('fail_error') or e.get('error')}"
                              for e in ep if isinstance(e, dict)]
    if adicionais:
        rs = _chamar(user_id, "/api/v2/add_on_deal/add_add_on_deal_sub_item", metodo="POST",
                     extra={"add_on_deal_id": aid,
                            "sub_item_list": [{"item_id": int(s["item_id"]),
                                               "add_on_deal_price": float(s["add_on_deal_price"]),
                                               "status": 1} for s in adicionais]})
        es = (rs.get("response") or {}).get("error_list") or []
        out["adicionais_ok"] = len(adicionais) - len(es)
        out["item_erros"] += [f"adicional {e.get('item_id')}: {e.get('fail_error') or e.get('error')}"
                              for e in es if isinstance(e, dict)]
    return out


def encerrar_addon(user_id: int, add_on_deal_id) -> dict:
    return _chamar(user_id, "/api/v2/add_on_deal/delete_add_on_deal", metodo="POST",
                   extra={"add_on_deal_id": int(add_on_deal_id)})


# --------------------------- Flash Sale ----------------------------------- #
def flash_slots(user_id: int, dias: int = 7) -> dict:
    """Horários (slots) disponíveis para Flash Sale da loja nos próximos dias."""
    agora = int(time.time())
    return _chamar(user_id, "/api/v2/shop_flash_sale/get_shop_flash_sale_time_slot_id",
                   extra={"start_time": agora, "end_time": agora + max(dias, 1) * 86400})


def modelos_item(user_id: int, item_id) -> list:
    """Variações (modelos) de um anúncio: model_id, preço e estoque."""
    r = _chamar(user_id, "/api/v2/product/get_model_list", extra={"item_id": int(item_id)})
    return (r.get("response") or {}).get("model") or []


def _expandir_itens_flash(user_id: int, itens: list) -> list:
    """Transforma [{item_id, preco, purchase_limit?}] na estrutura que a Shopee exige,
    buscando as variações (modelos) de cada produto e aplicando o preço promocional a cada uma.
    Produtos sem variação viram um único modelo (model_id=0)."""
    preparados = []
    for it in itens:
        item_id = int(it["item_id"])
        modelos = it.get("models")
        if not modelos:
            preco = float(it.get("preco") or 0)
            try:
                ml = modelos_item(user_id, item_id)
            except ShopeeError:
                ml = []
            if ml:
                modelos = []
                for m in ml:
                    est = (m.get("stock_info_v2") or {}).get("summary_info", {}) or m.get("stock_info", {}) or {}
                    estoque = est.get("total_available_stock") or est.get("current_stock") or it.get("stock", 0)
                    modelos.append({"model_id": m.get("model_id"),
                                    "input_promo_price": preco,
                                    "stock": int(it.get("stock") or estoque or 0)})
            else:
                modelos = [{"model_id": 0, "input_promo_price": preco, "stock": int(it.get("stock") or 0)}]
        preparados.append({"item_id": item_id, "purchase_limit": int(it.get("purchase_limit") or 0),
                           "models": modelos})
    return preparados


def criar_flash(user_id: int, timeslot_id: int, itens: list) -> dict:
    """Cria uma Flash Sale num slot e adiciona os itens (expandindo as variações).
    itens (simples): [{item_id, preco, purchase_limit?}]."""
    r = _chamar(user_id, "/api/v2/shop_flash_sale/create_shop_flash_sale", metodo="POST",
                extra={"timeslot_id": int(timeslot_id)})
    fid = (r.get("response") or {}).get("flash_sale_id")
    if fid and itens:
        preparados = _expandir_itens_flash(user_id, itens)
        _chamar(user_id, "/api/v2/shop_flash_sale/add_shop_flash_sale_items", metodo="POST",
                extra={"flash_sale_id": fid, "items": preparados})
    return {"response": {"flash_sale_id": fid}, "itens_adicionados": len(itens)}


def encerrar_flash(user_id: int, flash_sale_id) -> dict:
    return _chamar(user_id, "/api/v2/shop_flash_sale/delete_shop_flash_sale", metodo="POST",
                   extra={"flash_sale_id": int(flash_sale_id)})


def listar_flash(user_id: int, tipo: int = 1, limite: int = 50) -> dict:
    """tipo: 1=upcoming, 2=ongoing, 3=expired (status_filter da Shopee)."""
    return _chamar(user_id, "/api/v2/shop_flash_sale/get_shop_flash_sale_list",
                   extra={"type": tipo, "offset": 0, "limit": min(limite, 100)})
