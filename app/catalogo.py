"""Cache local do catálogo do Bling.

Estratégia: puxar o catálogo inteiro UMA vez (paginando tudo), gravar no banco,
e manter atualizado via webhook (produto.created/updated/deleted). As telas leem
deste cache — rápido e sem martelar a API do Bling.
"""
from datetime import datetime, date, timedelta
import re as _re
import html as _html

from sqlalchemy import or_

from . import bling
from .db import SessionLocal
from .models import ProdutoCache, CatalogoSync


def _strip_html(s) -> str:
    """Remove tags HTML e decodifica entidades — o Bling devolve descrições como
    '<p>Embalagem com 100ml</p>'. Sem isso, a impressão mostra as tags como texto."""
    if not s:
        return ""
    txt = _re.sub(r"<[^>]+>", " ", str(s))
    txt = _html.unescape(txt)
    return _re.sub(r"\s+", " ", txt).strip()


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


def _img(p: dict):
    """Primeira imagem do payload do Bling (lista ou GET)."""
    ext = (((p.get("midia") or {}).get("imagens") or {}).get("externas") or [])
    for i in ext:
        if isinstance(i, dict) and i.get("link"):
            return i["link"]
    return p.get("imagemURL") or None


def _resumo(p: dict) -> dict:
    """Extrai os campos indexados de um produto do Bling."""
    est = p.get("estoque") or {}
    return {
        "produto_id": str(p.get("id")),
        "sku": p.get("codigo"),
        "nome": p.get("nome"),
        "imagem": _img(p),
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
    reg.imagem = r["imagem"]
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


def registrar_snapshot_precos(user_id: int) -> None:
    """Grava um ponto de Preço Bling por produto por dia (idempotente). Roda no fim do sync.
    Mantém só os últimos ~120 dias para o gráfico de histórico de preço no cockpit."""
    from .models import ProdutoPrecoSnapshot
    hoje = date.today()
    db = SessionLocal()
    try:
        db.query(ProdutoPrecoSnapshot).filter(
            ProdutoPrecoSnapshot.user_id == user_id,
            ProdutoPrecoSnapshot.dia == hoje).delete(synchronize_session=False)
        db.query(ProdutoPrecoSnapshot).filter(
            ProdutoPrecoSnapshot.user_id == user_id,
            ProdutoPrecoSnapshot.dia < hoje - timedelta(days=120)).delete(synchronize_session=False)
        regs = db.query(ProdutoCache.produto_id, ProdutoCache.sku, ProdutoCache.preco).filter(
            ProdutoCache.user_id == user_id).all()
        db.bulk_save_objects([
            ProdutoPrecoSnapshot(user_id=user_id, produto_id=r[0], sku=r[1], preco=float(r[2] or 0), dia=hoje)
            for r in regs if r[0] and float(r[2] or 0) > 0
        ])
        db.commit()
    except Exception:  # noqa: BLE001 — nunca derruba o sync por causa do histórico
        db.rollback()
    finally:
        db.close()


def _estado_vinculos(db, user_id):
    from .models import VinculosSync
    est = db.query(VinculosSync).filter_by(user_id=user_id).first()
    if not est:
        est = VinculosSync(user_id=user_id, status="ocioso")
        db.add(est); db.commit(); db.refresh(est)
    return est


def status_vinculos(user_id: int) -> dict:
    db = SessionLocal()
    try:
        est = _estado_vinculos(db, user_id)
        return {"status": est.status, "total": est.total, "processados": est.processados,
                "erro": est.erro,
                "iniciado_em": est.iniciado_em.isoformat() if est.iniciado_em else None,
                "concluido_em": est.concluido_em.isoformat() if est.concluido_em else None}
    except Exception:  # noqa: BLE001 — tabela ainda não criada: não derruba a página
        db.rollback()
        return {"status": "ocioso", "total": 0, "processados": 0, "erro": None,
                "iniciado_em": None, "concluido_em": None}
    finally:
        db.close()


def enriquecer_vinculos(user_id: int) -> None:
    """Mapeia, produto a produto, em quais marketplaces ele está anunciado (lê os vínculos
    no Bling) e grava a lista de canais na coluna `marketplaces` do cache. Pesado: ~1
    chamada por produto, respeitando o rate limit (~3/s). Roda em background, com progresso.
    Idempotente: pode rodar de novo a qualquer momento."""
    import time
    from .models import ProdutoCache
    db = SessionLocal()
    try:
        est = _estado_vinculos(db, user_id)
        est.status = "rodando"; est.erro = None; est.processados = 0
        est.iniciado_em = datetime.utcnow(); est.concluido_em = None; db.commit()
        ids = [r[0] for r in db.query(ProdutoCache.produto_id).filter_by(user_id=user_id).all() if r[0]]
        est.total = len(ids); db.commit()
        feito = 0
        for pid in ids:
            canais = []
            raw = {}
            try:
                raw = (bling.obter_produto(user_id, pid) or {}).get("data", {}) or {}
                for chave in ("vinculosLojas", "lojas", "produtosLojas"):
                    arr = raw.get(chave)
                    if isinstance(arr, list) and arr:
                        canais = [{"canal": v.get("canal"), "nome": v.get("nome"),
                                   "publicado": bool(v.get("publicado"))}
                                  for v in bling.parse_vinculos_multiloja(arr) if v.get("canal")]
                        break
            except bling.BlingAuthError:
                est = _estado_vinculos(db, user_id)
                est.status = "erro"; est.erro = "Bling não autorizado"; db.commit(); return
            except Exception:  # noqa: BLE001 — um produto problemático não derruba o job
                canais = []
            reg = db.query(ProdutoCache).filter_by(user_id=user_id, produto_id=pid).first()
            if reg is not None:
                reg.marketplaces = canais
                # O GET por produto traz o que a lista NÃO traz: aproveita imagem, custo e saldo
                # (sem isso a lista fica com imagem quebrada e "sem custo").
                if raw:
                    try:
                        img = _img(raw)
                        if img:
                            reg.imagem = img
                        pc = _f(raw.get("precoCusto"))
                        if pc and pc > 0:
                            reg.custo = pc
                        saldo = (raw.get("estoque") or {}).get("saldoVirtualTotal")
                        if saldo is not None:
                            reg.saldo = _f(saldo)
                    except Exception:  # noqa: BLE001
                        pass
            feito += 1
            if feito % 25 == 0 or feito == len(ids):
                est = _estado_vinculos(db, user_id); est.processados = feito; db.commit()
            time.sleep(0.34)
        est = _estado_vinculos(db, user_id)
        est.status = "concluido"; est.processados = len(ids); est.concluido_em = datetime.utcnow(); db.commit()
        try:
            from . import notificacoes as notif
            notif.criar(user_id, "produto", "Mapeamento de canais concluído",
                        f"{len(ids)} produto(s) verificados nos marketplaces.", ok=True, modulo="catalogo")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        try:
            est = _estado_vinculos(db, user_id); est.status = "erro"; est.erro = str(e)[:200]; db.commit()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


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
                registrar_snapshot_precos(user_id)
            except Exception:  # noqa: BLE001
                pass
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


# Cache em memória por (user_id, sku) além do que fica gravado em ProdutoCache.dados
_DESC_COMPL_CACHE: dict = {}


def descricao_complementar(user_id: int, sku: str) -> str:
    """Descrição complementar do produto (campo descricaoComplementar do Bling) por SKU.
    Resolve o produto_id pelo cache local, busca no Bling uma vez e grava em ProdutoCache.dados."""
    if not sku:
        return ""
    chave = (user_id, str(sku))
    if chave in _DESC_COMPL_CACHE:
        return _DESC_COMPL_CACHE[chave]
    db = SessionLocal()
    try:
        reg = db.query(ProdutoCache).filter_by(user_id=user_id, sku=str(sku)).first()
        if not reg:
            _DESC_COMPL_CACHE[chave] = ""
            return ""
        dados = reg.dados or {}
        if "descricaoComplementar" in dados:
            v = _strip_html(dados.get("descricaoComplementar"))
            _DESC_COMPL_CACHE[chave] = v
            return v
        # ainda não cacheado: busca no Bling (1 chamada) e grava no cache local
        try:
            prod = (bling.obter_produto(user_id, reg.produto_id) or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            return ""  # erro transitório: não cacheia, tenta de novo na próxima
        v = _strip_html(prod.get("descricaoComplementar"))
        dados["descricaoComplementar"] = v
        reg.dados = dados
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(reg, "dados")
        except Exception:  # noqa: BLE001
            pass
        db.commit()
        _DESC_COMPL_CACHE[chave] = v
        return v
    finally:
        db.close()
