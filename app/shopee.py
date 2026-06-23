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
    return (f"{settings.shopee_base_url}{path}?partner_id={settings.shopee_partner_id}"
            f"&timestamp={ts}&sign={sign}&redirect={redirect}")


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
def _chamar(user_id: int, path: str, extra: dict | None = None, metodo: str = "GET") -> dict:
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
            r = requests.get(url, params={**params, **(extra or {})}, timeout=30)
        else:
            r = requests.post(url, params=params, json=(extra or {}), timeout=30)
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
def listar_itens(user_id: int, offset: int = 0, limite: int = 50) -> dict:
    return _chamar(user_id, "/api/v2/product/get_item_list",
                   extra={"offset": offset, "page_size": min(limite, 100), "item_status": "NORMAL"})


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
