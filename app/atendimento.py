"""Central de Atendimento — perguntas (e, no futuro, avaliações) de todos os canais num lugar só.

Hoje cobre o Mercado Livre. A IA pré-escreve cada resposta no tom da loja; você revisa e envia.
Multicanal por design: quando a Shopee liberar perguntas/chat pela API, entram aqui no mesmo
formato, sem mudar a tela. As perguntas vêm do vendedor (todos os anúncios de uma vez) e são
enriquecidas com o produto a partir do MLItemCache (título, SKU, imagem) — sem bater na API item a item.
"""
from __future__ import annotations

from . import ai
from . import mercadolivre as ml
from .db import SessionLocal
from .models import MLItemCache


def _produto_do_item(db, user_id, item_id):
    """Resolve o produto de um item_id pelo cache local (rápido)."""
    if not item_id:
        return {}
    c = db.query(MLItemCache).filter_by(user_id=user_id, item_id=item_id).first()
    if not c:
        return {"item_id": item_id}
    return {"item_id": item_id, "titulo": c.titulo, "sku": c.sku,
            "imagem": c.imagem, "permalink": c.permalink}


def inbox(user_id, status="UNANSWERED", limite=50) -> dict:
    """Perguntas do vendedor (todos os anúncios), enriquecidas com o produto do cache."""
    st = None if status in ("ALL", "TODAS", "") else status
    d = ml.listar_perguntas(user_id, status=st, limit=limite)
    perguntas = d.get("questions") or []
    db = SessionLocal()
    try:
        itens = []
        for q in perguntas:
            ans = q.get("answer") or {}
            itens.append({
                "question_id": q.get("id"),
                "item_id": q.get("item_id"),
                "texto": q.get("text"),
                "status": q.get("status"),
                "data": q.get("date_created"),
                "comprador": "Comprador",
                "resposta": ans.get("text"),
                "resposta_data": ans.get("date_created"),
                "produto": _produto_do_item(db, user_id, q.get("item_id")),
            })
    finally:
        db.close()
    return {"total": d.get("total"), "canal": "mercadolivre", "perguntas": itens}


def stats(user_id) -> dict:
    """Contadores para o topo da central (sem resposta, respondidas, tempo médio)."""
    sem_resposta, respondidas, tempo = 0, 0, None
    try:
        sem_resposta = ml.listar_perguntas(user_id, status="UNANSWERED", limit=1).get("total") or 0
    except Exception:  # noqa: BLE001
        pass
    try:
        respondidas = ml.listar_perguntas(user_id, status="ANSWERED", limit=1).get("total") or 0
    except Exception:  # noqa: BLE001
        pass
    try:
        t = ml.tempo_de_resposta(user_id) or {}
        bloco = t.get("total") if isinstance(t.get("total"), dict) else t
        seg = (bloco or {}).get("response_time") or (bloco or {}).get("seconds")
        if seg:
            tempo = round(float(seg) / 60)
    except Exception:  # noqa: BLE001
        pass
    return {"canal": "mercadolivre", "sem_resposta": sem_resposta,
            "respondidas": respondidas, "tempo_medio_min": tempo}


def sugerir(user_id, pergunta, produto="") -> dict:
    """Rascunho da IA para uma pergunta, no tom da loja."""
    return {"texto": ai.gerar_resposta_pergunta(user_id, pergunta, produto)}


def responder(user_id, question_id, texto) -> dict:
    ml.responder_pergunta(question_id, texto, user_id=user_id)
    return {"ok": True, "question_id": question_id}


def ocultar(user_id, question_id) -> dict:
    ml.ocultar_pergunta(question_id, user_id=user_id)
    return {"ok": True, "question_id": question_id}
