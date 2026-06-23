"""Motor de auto-boost da Shopee.

Regra da Shopee: até 5 itens impulsionados por vez, cada boost dura 4h.
Este motor mantém uma lista (até 30 itens), prioriza os fixos (pin) e roda os
demais em rodízio — reimpulsionando os próximos quando os 4h de um lote acabam,
respeitando uma janela de horário opcional (ex.: só nos picos).
"""
from datetime import datetime, timedelta

from . import shopee
from .db import SessionLocal
from .models import ShopeeBoostItem, ShopeeBoostConfig, ProdutoCache

BOOST_HORAS = 4
MAX = 5


def _config(db, user_id: int) -> ShopeeBoostConfig:
    c = db.query(ShopeeBoostConfig).filter_by(user_id=user_id).first()
    if not c:
        c = ShopeeBoostConfig(user_id=user_id)
        db.add(c)
        db.commit()
    return c


def _na_janela(cfg: ShopeeBoostConfig) -> bool:
    """Respeita a janela de horário (0/0 = sempre)."""
    if not cfg.janela_inicio and not cfg.janela_fim:
        return True
    h = datetime.utcnow().hour
    ini, fim = cfg.janela_inicio, cfg.janela_fim
    return (ini <= h < fim) if ini <= fim else (h >= ini or h < fim)


def _ordenar(db, user_id: int, itens: list, criterio: str) -> list:
    """Ordena os candidatos por critério (prioridade/margem/giro/abc)."""
    if criterio == "prioridade":
        return sorted(itens, key=lambda x: (-(x.prioridade or 0), x.ultimo_boost or datetime.min))
    # cruza com o cache de produtos quando o critério é de negócio
    cache = {p.sku: p for p in db.query(ProdutoCache).filter_by(user_id=user_id).all()}

    def chave(x):
        p = cache.get(x.item_id)
        if criterio == "margem":
            return -((p.preco - p.custo) if p else 0)
        if criterio == "giro":
            return -(p.saldo if p else 0)
        return x.ultimo_boost or datetime.min
    return sorted(itens, key=chave)


def status(user_id: int) -> dict:
    """Estado atual do motor para o painel. SÓ banco — rápido e sem chamar a Shopee
    (resolver nome via API aqui travava os workers; nomes são sincronizados à parte)."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        itens = db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()
        agora = datetime.utcnow()
        ativos = [i for i in itens if i.boost_ate and i.boost_ate > agora]
        fila = [i for i in itens if not (i.boost_ate and i.boost_ate > agora) and not i.fixo]
        fila = _ordenar(db, user_id, fila, cfg.criterio)

        def _nome(i):
            return i.nome if (i.nome and str(i.nome).lstrip("#") != str(i.item_id)) else f"#{i.item_id}"

        return {
            "ativo": cfg.ativo, "criterio": cfg.criterio,
            "janela_inicio": cfg.janela_inicio, "janela_fim": cfg.janela_fim,
            "max_simultaneos": cfg.max_simultaneos,
            "total": len(itens), "fixos": sum(1 for i in itens if i.fixo),
            "impulsionando": [{
                "item_id": i.item_id, "nome": _nome(i), "fixo": i.fixo,
                "termina_em": i.boost_ate.isoformat() if i.boost_ate else None,
                "impulsos": i.impulsos,
            } for i in ativos],
            "fila": [{
                "item_id": i.item_id, "nome": _nome(i), "prioridade": i.prioridade,
                "ultimo_boost": i.ultimo_boost.isoformat() if i.ultimo_boost else None,
                "impulsos": i.impulsos,
            } for i in fila],
        }
    finally:
        db.close()


def sincronizar_nomes(user_id: int) -> dict:
    """Resolve e persiste os nomes reais dos itens do boost (best-effort, sob demanda).
    Chamado por um endpoint próprio, NUNCA dentro do status."""
    db = SessionLocal()
    try:
        itens = db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()
        precisa = [i.item_id for i in itens if not i.nome or str(i.nome).lstrip("#") == str(i.item_id)]
        if not precisa:
            return {"atualizados": 0, "total": len(itens)}
        meta = shopee.nomes_itens(user_id, precisa)  # 1 chamada Shopee
        n = 0
        for i in itens:
            m = meta.get(int(i.item_id)) if str(i.item_id).isdigit() else None
            if m and m.get("nome"):
                i.nome = m["nome"]; n += 1
        db.commit()
        return {"atualizados": n, "total": len(itens)}
    finally:
        db.close()


def ciclo(user_id: int) -> dict:
    """Executa um ciclo: se houver vaga (até 5), impulsiona os próximos da fila.
    Chamado periodicamente pelo agendador em background."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        if not cfg.ativo or not _na_janela(cfg):
            return {"acao": "ocioso"}
        agora = datetime.utcnow()
        itens = db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()
        ativos = [i for i in itens if i.boost_ate and i.boost_ate > agora]
        vagas = max(0, min(cfg.max_simultaneos, MAX) - len(ativos))
        if vagas == 0:
            return {"acao": "cheio", "ativos": len(ativos)}
        # fixos primeiro (que não estão ativos), depois fila por critério
        fixos = [i for i in itens if i.fixo and not (i.boost_ate and i.boost_ate > agora)]
        fila = [i for i in itens if not i.fixo and not (i.boost_ate and i.boost_ate > agora)]
        fila = _ordenar(db, user_id, fila, cfg.criterio)
        escolhidos = (fixos + fila)[:vagas]
        if not escolhidos:
            return {"acao": "sem_candidatos"}
        ids = [e.item_id for e in escolhidos]
        try:
            shopee.impulsionar(user_id, ids)
        except shopee.ShopeeError as e:
            return {"acao": "erro", "erro": str(e)}
        fim = agora + timedelta(hours=BOOST_HORAS)
        for e in escolhidos:
            e.ultimo_boost = agora
            e.boost_ate = fim
            e.impulsos = (e.impulsos or 0) + 1
        db.commit()
        return {"acao": "impulsionado", "itens": ids, "termina_em": fim.isoformat()}
    finally:
        db.close()
