"""Cache local do catálogo do Bling.

Estratégia: puxar o catálogo inteiro UMA vez (paginando tudo), gravar no banco,
e manter atualizado via webhook (produto.created/updated/deleted). As telas leem
deste cache — rápido e sem martelar a API do Bling.
"""
from datetime import datetime

from sqlalchemy import or_

from . import bling
from .db import SessionLocal
from .models import ProdutoCache, CatalogoSync


def _f(v) -> float:
    if isinstance(v, str):
        s = v.strip().replace(".", "").replace(",", ".") if "," in v else v
        try:
            return float(s or 0)
        except ValueError:
            return 0.0
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _resumo(p: dict) -> dict:
    """Extrai os campos indexados de um produto do Bling."""
    est = p.get("estoque") or {}
    return {
        "produto_id": str(p.get("id")),
        "sku": p.get("codigo"),
        "nome": p.get("nome"),
        "preco": _f(p.get("preco")),
        "custo": _f(p.get("precoCusto")),
        "saldo": _f(est.get("saldoVirtualTotal")),
        "situacao": p.get("situacao"),
        "tipo": p.get("tipo"),
    }


def upsert_produto(db, user_id: int, p: dict) -> None:
    """Insere/atualiza um produto no cache a partir do payload do Bling."""
    if not p or p.get("id") is None:
        return
    r = _resumo(p)
    reg = db.query(ProdutoCache).filter_by(user_id=user_id, produto_id=r["produto_id"]).first()
    if not reg:
        reg = ProdutoCache(user_id=user_id, produto_id=r["produto_id"])
        db.add(reg)
    reg.sku = r["sku"]; reg.nome = r["nome"]; reg.preco = r["preco"]
    reg.custo = r["custo"]; reg.saldo = r["saldo"]; reg.situacao = r["situacao"]
    reg.tipo = r["tipo"]; reg.dados = p; reg.atualizado_em = datetime.utcnow()
    db.commit()


def remover_produto(db, user_id: int, produto_id) -> None:
    db.query(ProdutoCache).filter_by(user_id=user_id, produto_id=str(produto_id)).delete()
    db.commit()


def atualizar_do_bling(user_id: int, produto_id) -> None:
    """Busca um produto no Bling e atualiza o cache (usado pelo webhook)."""
    db = SessionLocal()
    try:
        raw = (bling.obter_produto(user_id, produto_id) or {}).get("data") or {}
        if raw:
            upsert_produto(db, user_id, raw)
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()


def _estado(db, user_id: int) -> CatalogoSync:
    est = db.query(CatalogoSync).filter_by(user_id=user_id).first()
    if not est:
        est = CatalogoSync(user_id=user_id, status="ocioso")
        db.add(est); db.commit()
    return est


def sincronizar_tudo(user_id: int) -> None:
    """Puxa o catálogo inteiro do Bling e grava no cache. Atualiza o progresso.
    Pensado para rodar em background (pode levar minutos em catálogos grandes)."""
    db = SessionLocal()
    try:
        est = _estado(db, user_id)
        est.status = "rodando"; est.erro = None; est.paginas = 0
        est.iniciado_em = datetime.utcnow(); est.concluido_em = None
        db.commit()
        total = 0
        try:
            for pagina, lote in bling.listar_todos_produtos(user_id, limite=100):
                for p in lote:
                    upsert_produto(db, user_id, p)
                total += len(lote)
                est = _estado(db, user_id)
                est.paginas = pagina; est.total = db.query(ProdutoCache).filter_by(user_id=user_id).count()
                db.commit()
            est = _estado(db, user_id)
            est.status = "concluido"; est.concluido_em = datetime.utcnow()
            est.total = db.query(ProdutoCache).filter_by(user_id=user_id).count()
            db.commit()
            try:
                from . import notificacoes as notif
                notif.criar(user_id, "produto",
                            f"Catálogo sincronizado: {est.total} produto(s)",
                            "Importação do Bling concluída.", ok=True, modulo="catalogo")
            except Exception:  # noqa: BLE001
                pass
        except bling.BlingAuthError as e:
            est = _estado(db, user_id)
            est.status = "erro"; est.erro = f"Bling não autorizado: {e}"; db.commit()
        except Exception as e:  # noqa: BLE001
            est = _estado(db, user_id)
            est.status = "erro"; est.erro = str(e)[:200]; db.commit()
    finally:
        db.close()


def status(user_id: int) -> dict:
    db = SessionLocal()
    try:
        est = _estado(db, user_id)
        return {"status": est.status, "total": est.total, "paginas": est.paginas,
                "erro": est.erro,
                "iniciado_em": est.iniciado_em.isoformat() if est.iniciado_em else None,
                "concluido_em": est.concluido_em.isoformat() if est.concluido_em else None}
    finally:
        db.close()


def listar(user_id: int, busca: str = "", pagina: int = 1, limite: int = 50,
           situacao: str = "") -> dict:
    """Lê o catálogo DO CACHE (rápido, sem tocar no Bling)."""
    db = SessionLocal()
    try:
        q = db.query(ProdutoCache).filter_by(user_id=user_id)
        if busca:
            termo = f"%{busca.lower()}%"
            q = q.filter(or_(ProdutoCache.nome.ilike(termo), ProdutoCache.sku.ilike(termo)))
        if situacao:
            q = q.filter(ProdutoCache.situacao == situacao)
        total = q.count()
        itens = (q.order_by(ProdutoCache.nome.asc())
                 .offset((pagina - 1) * limite).limit(limite).all())
        return {"total": total, "pagina": pagina, "limite": limite,
                "itens": [{"id": r.produto_id, "sku": r.sku, "nome": r.nome,
                           "preco": r.preco, "custo": r.custo, "saldo": r.saldo,
                           "situacao": r.situacao} for r in itens]}
    finally:
        db.close()


def todos(user_id: int) -> list:
    """Todos os produtos do cache (lightweight) para cálculos do dashboard."""
    db = SessionLocal()
    try:
        rows = db.query(ProdutoCache).filter_by(user_id=user_id).all()
        return [{"sku": r.sku, "nome": r.nome, "preco": r.preco or 0.0,
                 "custo": r.custo or 0.0, "saldo": r.saldo or 0.0} for r in rows]
    finally:
        db.close()
