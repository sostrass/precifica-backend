"""Receptor de webhooks do Bling (push em tempo real).

O Bling faz POST numa URL nossa a cada evento (produto/pedido/NF-e/estoque...).
Formato do corpo (v1):
    { "eventId": "...", "date": "...", "version": "v1",
      "event": "<recurso>.<acao>", "companyId": "...", "data": { ... } }

Cada tenant tem uma URL única e assinada: /webhooks/bling/{token}. O token é
um HMAC do user_id com o jwt_secret — stateless, sem precisar de coluna nova.

Regras importantes (doc do Bling):
- Responder 200 rápido; se falhar por 3 dias seguidos, o Bling DESABILITA o webhook.
- Eventos podem chegar fora de ordem e duplicados (dedupe por eventId).
"""
import base64
import hashlib
import hmac

from .config import settings
from .models import WebhookEvento, ProdutoSync
from datetime import datetime

_SEP = "."


def gerar_token(user_id: int) -> str:
    """Token estável e assinado para o tenant (sem estado em banco)."""
    uid = base64.urlsafe_b64encode(str(user_id).encode()).decode().rstrip("=")
    assinatura = hmac.new(settings.jwt_secret.encode(), uid.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{uid}{_SEP}{assinatura}"


def verificar_token(token: str):
    """Devolve o user_id se o token for válido, senão None."""
    try:
        uid_b64, assinatura = (token or "").split(_SEP, 1)
    except ValueError:
        return None
    esperado = hmac.new(settings.jwt_secret.encode(), uid_b64.encode(), hashlib.sha256).hexdigest()[:24]
    if not hmac.compare_digest(assinatura, esperado):
        return None
    try:
        pad = "=" * (-len(uid_b64) % 4)
        return int(base64.urlsafe_b64decode(uid_b64 + pad).decode())
    except (ValueError, TypeError):
        return None


def _split_evento(evento: str):
    """'pedido_venda.updated' -> ('pedido_venda', 'updated')."""
    evento = evento or ""
    if "." in evento:
        recurso, acao = evento.rsplit(".", 1)
        return recurso, acao
    return evento, ""


def registrar_evento(db, user_id: int, corpo: dict) -> WebhookEvento | None:
    """Grava o evento (com dedupe por eventId) e devolve o registro novo, ou None se duplicado."""
    event = corpo.get("event") or ""
    recurso, acao = _split_evento(event)
    event_id = corpo.get("eventId")
    data = corpo.get("data") or {}
    entidade_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") is not None else None

    if event_id:
        ja = db.query(WebhookEvento).filter_by(user_id=user_id, event_id=event_id).first()
        if ja:
            return None  # duplicado — ignora

    reg = WebhookEvento(
        user_id=user_id, event=event, recurso=recurso, acao=acao,
        event_id=event_id, company_id=corpo.get("companyId"),
        entidade_id=entidade_id, payload=corpo, processado=False,
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)
    return reg


def processar(user_id: int, recurso: str, acao: str, data: dict) -> str:
    """Reage ao evento. Mantido leve (responder rápido ao Bling).
    Hoje: invalida o cache de vendas quando pedido/estoque muda, para KPIs/ABC
    refletirem na hora. Devolve uma descrição curta do que foi feito."""
    r = (recurso or "").lower()
    feitos = []

    # Pedidos e estoque afetam KPIs, Curva ABC, demanda e risco de ruptura.
    if r in ("pedido_venda", "pedidovenda", "estoque", "estoque_virtual", "estoquevirtual"):
        try:
            from . import agentes
            agentes._CACHE_VENDAS.pop(user_id, None)
            feitos.append("cache de vendas/estoque invalidado")
        except Exception:
            pass

    # NF-e: o auto-apply do desconto roda logo depois, no processamento em background
    # (em main._processar_evento_async), porque envolve buscar a nota e devolver ao Bling.
    if r in ("nfe", "notafiscal", "nota_fiscal", "notafiscaleletronica"):
        feitos.append("NF-e encaminhada para auto-aplicação do desconto")

    return "; ".join(feitos) or "evento registrado"


# ---- Status de sincronização por produto (app -> Bling -> confirmação) ----
def registrar_envio(db, user_id: int, produto_id, sku, campos) -> None:
    """Marca que empurramos uma alteração do produto para o Bling (status 'enviado')."""
    pid = str(produto_id)
    reg = db.query(ProdutoSync).filter_by(user_id=user_id, produto_id=pid).first()
    if not reg:
        reg = ProdutoSync(user_id=user_id, produto_id=pid)
        db.add(reg)
    reg.sku = sku or reg.sku
    reg.status = "enviado"
    reg.campos = list(campos) if campos else None
    reg.enviado_em = datetime.utcnow()
    reg.erro = None
    db.commit()


def confirmar_sync(db, user_id: int, produto_id) -> None:
    """Confirma que o Bling aplicou a alteração (chega via webhook produto.updated)."""
    if produto_id is None:
        return
    reg = db.query(ProdutoSync).filter_by(user_id=user_id, produto_id=str(produto_id)).first()
    if reg:
        reg.status = "confirmado"
        reg.confirmado_em = datetime.utcnow()
        db.commit()


def status_sync(db, user_id: int, produto_id) -> dict:
    """Estado de sincronização do produto para a Ficha."""
    reg = db.query(ProdutoSync).filter_by(user_id=user_id, produto_id=str(produto_id)).first()
    if not reg:
        return {"estado": "sem_alteracoes", "enviado_em": None, "confirmado_em": None, "campos": None}
    pendente = reg.enviado_em and (not reg.confirmado_em or reg.confirmado_em < reg.enviado_em)
    estado = "erro" if reg.status == "erro" else ("pendente" if pendente else "confirmado")
    return {"estado": estado,
            "enviado_em": reg.enviado_em.isoformat() if reg.enviado_em else None,
            "confirmado_em": reg.confirmado_em.isoformat() if reg.confirmado_em else None,
            "campos": reg.campos, "erro": reg.erro}
