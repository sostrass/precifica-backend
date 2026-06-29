"""Cache local do catálogo da Shopee.

Sincroniza os anúncios (item_id, sku, preço atual, preço original, promoção) e grava
em ShopeeItemCache. O cockpit lê daqui (promoção por SKU) e a divergência pode ler
daqui no futuro — sem repetir as ~150 chamadas sequenciais a cada tela.
"""

from datetime import datetime

from .db import SessionLocal
from .models import ShopeeItemCache, ShopeeSync
from . import shopee


def _estado(db, user_id: int) -> ShopeeSync:
    est = db.query(ShopeeSync).filter_by(user_id=user_id).first()
    if not est:
        est = ShopeeSync(user_id=user_id)
        db.add(est)
        db.commit()
        db.refresh(est)
    return est


def sincronizar(user_id: int) -> None:
    """Puxa o catálogo inteiro da Shopee e grava no cache. Roda em background."""
    db = SessionLocal()
    try:
        est = _estado(db, user_id)
        est.status = "rodando"
        est.erro = None
        est.iniciado_em = datetime.utcnow()
        est.concluido_em = None
        db.commit()
        try:
            itens = shopee.catalogo_shopee(user_id)
            try:
                campanhas = shopee.campanhas_ativas_itens(user_id)
            except Exception:  # noqa: BLE001 — promoção é opcional
                campanhas = {}

            vistos = set()
            for it in itens:
                item_id = it.get("item_id")
                if not item_id:
                    continue
                item_id = str(item_id)
                vistos.add(item_id)
                preco = float(it.get("preco") or 0)
                orig = float(it.get("preco_original") or 0)
                promo_nome = campanhas.get(item_id)
                em_promo = bool(promo_nome) or (orig > preco + 0.01)
                reg = db.query(ShopeeItemCache).filter_by(user_id=user_id, item_id=item_id).first()
                if not reg:
                    reg = ShopeeItemCache(user_id=user_id, item_id=item_id)
                    db.add(reg)
                reg.sku = it.get("sku")
                reg.nome = it.get("nome")
                reg.preco = preco
                reg.preco_original = orig
                reg.em_promocao = em_promo
                reg.promo_nome = promo_nome
                reg.imagem = it.get("imagem")
                reg.status = it.get("status")
                reg.atualizado_em = datetime.utcnow()
            db.commit()

            # remove anúncios que sumiram da Shopee
            if vistos:
                for a in db.query(ShopeeItemCache).filter_by(user_id=user_id).all():
                    if a.item_id not in vistos:
                        db.delete(a)
                db.commit()

            est = _estado(db, user_id)
            est.status = "concluido"
            est.concluido_em = datetime.utcnow()
            est.total = db.query(ShopeeItemCache).filter_by(user_id=user_id).count()
            db.commit()
            try:
                from . import notificacoes as notif
                notif.criar(user_id, "shopee",
                            f"Catálogo Shopee sincronizado: {est.total} anúncio(s)",
                            "Itens, preços e promoções atualizados no cache.",
                            ok=True, modulo="catalogo")
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001 — registra o erro e sai
            est = _estado(db, user_id)
            est.status = "erro"
            est.erro = str(e)[:200]
            db.commit()
    finally:
        db.close()


def status(user_id: int) -> dict:
    db = SessionLocal()
    try:
        est = _estado(db, user_id)
        return {
            "status": est.status,
            "total": est.total,
            "erro": est.erro,
            "iniciado_em": est.iniciado_em.isoformat() if est.iniciado_em else None,
            "concluido_em": est.concluido_em.isoformat() if est.concluido_em else None,
        }
    except Exception:  # noqa: BLE001 — tabela ainda não criada / banco indisponível: não derruba a página
        db.rollback()
        return {"status": "ocioso", "total": 0, "erro": None,
                "iniciado_em": None, "concluido_em": None}
    finally:
        db.close()


def item_por_sku(user_id: int, sku: str):
    """Lê do cache o anúncio da Shopee com aquele SKU (o mais recente)."""
    if not sku:
        return None
    db = SessionLocal()
    try:
        reg = (db.query(ShopeeItemCache)
               .filter_by(user_id=user_id, sku=sku)
               .order_by(ShopeeItemCache.atualizado_em.desc())
               .first())
        if not reg:
            return None
        return {
            "item_id": reg.item_id, "sku": reg.sku, "nome": reg.nome,
            "preco": reg.preco, "preco_original": reg.preco_original,
            "em_promocao": bool(reg.em_promocao), "promo_nome": reg.promo_nome,
            "imagem": reg.imagem, "status": reg.status,
            "atualizado_em": reg.atualizado_em.isoformat() if reg.atualizado_em else None,
        }
    finally:
        db.close()


def total_cache(user_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(ShopeeItemCache).filter_by(user_id=user_id).count()
    finally:
        db.close()
