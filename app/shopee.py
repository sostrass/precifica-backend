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
    """Devolve {item_id: {nome, imagem, preco}} para uma lista de item_ids (best-effort)."""
    mapa = {}
    ids = [int(i) for i in item_ids if i][:50]
    if not ids:
        return mapa
    try:
        info = info_itens(user_id, ids)
    except ShopeeError:
        return mapa
    for x in (info.get("response") or {}).get("item_list") or []:
        imgs = (x.get("image") or {}).get("image_url_list") or []
        precos = x.get("price_info") or []
        mapa[x.get("item_id")] = {
            "nome": x.get("item_name"),
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
def listar_avaliacoes(user_id: int, item_id=None, cursor: str = "", limite: int = 20,
                      status: str = "UNANSWERED") -> dict:
    """Comentários/avaliações. status: ALL | UNANSWERED | ANSWERED, etc."""
    extra = {"cursor": cursor, "page_size": min(limite, 100), "comment_status": status}
    if item_id:
        extra["item_id"] = int(item_id)
    return _chamar(user_id, "/api/v2/product/get_comment", extra=extra)


def responder_avaliacao(user_id: int, comment_id, texto: str) -> dict:
    """Responde uma avaliação (uma ou várias)."""
    return _chamar(user_id, "/api/v2/product/reply_comment", metodo="POST",
                   extra={"comment_list": [{"comment_id": int(comment_id), "comment": texto}]})


# ------------------------------- Pedidos ---------------------------------- #
def listar_pedidos(user_id: int, dias: int = 7, cursor: str = "", limite: int = 50) -> dict:
    agora = int(time.time())
    return _chamar(user_id, "/api/v2/order/get_order_list",
                   extra={"time_range_field": "create_time", "time_from": agora - dias * 86400,
                          "time_to": agora, "page_size": min(limite, 100), "cursor": cursor})


def detalhe_pedidos(user_id: int, order_sns: list) -> dict:
    return _chamar(user_id, "/api/v2/order/get_order_detail",
                   extra={"order_sn_list": ",".join(order_sns)})


def repasse_pedido(user_id: int, order_sn: str) -> dict:
    """Escrow: valor líquido recebido, comissões e taxas (margem real)."""
    return _chamar(user_id, "/api/v2/payment/get_escrow_detail", extra={"order_sn": order_sn})


# ------------------------- Promoções: descontos --------------------------- #
def listar_descontos(user_id: int, status: str = "ongoing", limite: int = 50) -> dict:
    """status: upcoming | ongoing | expired."""
    return _chamar(user_id, "/api/v2/discount/get_discount_list",
                   extra={"discount_status": status, "page_size": min(limite, 100)})


def criar_desconto(user_id: int, nome: str, inicio: int, fim: int, itens: list) -> dict:
    """Cria uma campanha de desconto. itens: [{item_id, purchase_limit?, model_list?, promotion_price}]."""
    return _chamar(user_id, "/api/v2/discount/add_discount", metodo="POST",
                   extra={"discount_name": nome, "start_time": inicio, "end_time": fim,
                          "item_list": itens})


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
    if bid and item_ids:
        _chamar(user_id, "/api/v2/bundle_deal/add_bundle_deal_item", metodo="POST",
                extra={"bundle_deal_id": bid,
                       "item_list": [{"item_id": int(i)} for i in item_ids]})
    return r


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
    if aid:
        if principais:
            _chamar(user_id, "/api/v2/add_on_deal/add_add_on_deal_main_item", metodo="POST",
                    extra={"add_on_deal_id": aid,
                           "main_item_list": [{"item_id": int(i), "status": 1} for i in principais]})
        if adicionais:
            _chamar(user_id, "/api/v2/add_on_deal/add_add_on_deal_sub_item", metodo="POST",
                    extra={"add_on_deal_id": aid,
                           "sub_item_list": [{"item_id": int(s["item_id"]),
                                              "add_on_deal_price": float(s["add_on_deal_price"]),
                                              "status": 1} for s in adicionais]})
    return r


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
