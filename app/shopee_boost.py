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

_MSG_CHEIO = ("As 5 vagas de destaque da Shopee já estão ocupadas — inclui os boosts feitos "
              "manualmente pelo painel da Shopee. Libere uma vaga ou aguarde o fim do período (4h); "
              "assim que houver vaga, o motor impulsiona sozinho.")


def _boosted_ids(user_id: int):
    """IDs dos itens que a Shopee reporta em destaque AGORA (inclui os boosts manuais).
    Retorna lista de strings, ou None se não deu para ler (não bloqueia o ciclo)."""
    try:
        r = shopee.itens_impulsionados(user_id)
    except Exception:  # noqa: BLE001 — se não ler, o ciclo segue com o cálculo local
        return None
    resp = r.get("response") if isinstance(r, dict) else r
    lista = []
    if isinstance(resp, list):
        lista = resp
    elif isinstance(resp, dict):
        lista = (resp.get("list") or resp.get("item_list") or resp.get("item")
                 or resp.get("item_id_list") or [])
    ids = []
    for x in lista:
        v = x.get("item_id") if isinstance(x, dict) else x
        if v is not None:
            ids.append(str(v))
    return ids


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
            "auto_selecao": bool(getattr(cfg, "auto_selecao", False)),
            "auto_estrategia": getattr(cfg, "auto_estrategia", "estoque_parado") or "estoque_parado",
            "auto_maximo": getattr(cfg, "auto_maximo", 30) or 30,
            "qtd_auto": sum(1 for i in itens if getattr(i, "auto", False)),
            "qtd_manual": sum(1 for i in itens if not getattr(i, "auto", False)),
            "total": len(itens), "fixos": sum(1 for i in itens if i.fixo),
            "itens_ids": [i.item_id for i in itens],
            "impulsionando": [{
                "item_id": i.item_id, "nome": _nome(i), "fixo": i.fixo, "auto": getattr(i, "auto", False),
                "termina_em": i.boost_ate.isoformat() if i.boost_ate else None,
                "impulsos": i.impulsos,
            } for i in ativos],
            "fila": [{
                "item_id": i.item_id, "nome": _nome(i), "prioridade": i.prioridade, "auto": getattr(i, "auto", False),
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


def ciclo(user_id: int, notificar: bool = True) -> dict:
    """Executa um ciclo: se houver vaga (até 5), impulsiona os próximos da fila.
    Chamado periodicamente pelo agendador em background. `notificar=False` quando chamado
    de dentro do boost condicional (que emite a própria notificação, evitando duplicar)."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        if not cfg.ativo or not _na_janela(cfg):
            return {"acao": "ocioso"}
        agora = datetime.utcnow()
        itens = db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()
        # reconcilia com o estado REAL da Shopee — boosts feitos manualmente também
        # ocupam as 5 vagas de destaque da loja e não estavam registrados no nosso banco.
        reais = _boosted_ids(user_id)
        if reais is not None:
            por_id = {str(i.item_id): i for i in itens}
            for bid in reais:
                reg = por_id.get(str(bid))
                if reg and not (reg.boost_ate and reg.boost_ate > agora):
                    reg.boost_ate = agora + timedelta(hours=BOOST_HORAS)
            if reais:
                db.flush()
            ocupadas = len(reais)
        else:
            ocupadas = len([i for i in itens if i.boost_ate and i.boost_ate > agora])
        vagas = max(0, min(cfg.max_simultaneos, MAX) - ocupadas)
        if vagas == 0:
            return {"acao": "cheio", "ativos": ocupadas, "msg": _MSG_CHEIO}
        # fixos primeiro (que não estão ativos), depois fila por critério
        fixos = [i for i in itens if i.fixo and not (i.boost_ate and i.boost_ate > agora)]
        fila = [i for i in itens if not i.fixo and not (i.boost_ate and i.boost_ate > agora)]
        fila = _ordenar(db, user_id, fila, cfg.criterio)
        escolhidos = (fixos + fila)[:vagas]
        if not escolhidos:
            return {"acao": "sem_candidatos"}
        ids = [e.item_id for e in escolhidos]
        nomes = [e.nome for e in escolhidos]
        try:
            shopee.impulsionar(user_id, ids)
        except shopee.ShopeeError as e:
            m = str(e)
            if "bump slot" in m.lower() or "slot limit" in m.lower():
                return {"acao": "cheio", "ativos": MAX, "msg": _MSG_CHEIO}
            return {"acao": "erro", "erro": m}
        fim = agora + timedelta(hours=BOOST_HORAS)
        for e in escolhidos:
            e.ultimo_boost = agora
            e.boost_ate = fim
            e.impulsos = (e.impulsos or 0) + 1
        db.commit()
        if notificar:
            try:
                from . import notificacoes as notif
                amostra = ", ".join([n for n in nomes if n][:2])
                notif.criar(user_id, "agente",
                            f"Boost: {len(ids)} produto(s) impulsionado(s) na Shopee",
                            (f"{amostra}… " if amostra else "") + f"Em destaque por {BOOST_HORAS}h.",
                            ok=True, modulo="boost")
            except Exception:  # noqa: BLE001
                pass
        return {"acao": "impulsionado", "itens": ids, "termina_em": fim.isoformat()}
    finally:
        db.close()


def auto_selecionar(user_id: int, estrategia: str | None = None) -> dict:
    """Os agentes escolhem os produtos do boost SOZINHOS, por estratégia — sem seleção manual.
    estrategia: 'estoque_parado' (mais saldo, para girar o que não vende) | 'margem' (mais lucrativos).
    Preserva os itens adicionados manualmente e os fixos; só substitui os automáticos."""
    from . import catalogo, precificacao
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        estr = estrategia or cfg.auto_estrategia or "estoque_parado"
        teto = max(5, min(cfg.auto_maximo or 30, 50))

        cat = {p["sku"]: p for p in catalogo.todos(user_id) if p.get("sku")}
        if not cat:
            return {"acao": "sem_catalogo", "selecionados": 0,
                    "msg": "Sincronize o catálogo completo (aba Catálogo) para a auto-seleção funcionar."}

        # anúncios da Shopee com SKU (1 a 2 páginas)
        shop_itens = []
        try:
            for off in (0, 100):
                r = shopee.listar_itens(user_id, offset=off, limite=100)
                lst = (r.get("response") or {}).get("item") or []
                shop_itens.extend(lst)
                if len(lst) < 100:
                    break
        except shopee.ShopeeError as e:
            return {"acao": "erro", "erro": str(e), "selecionados": 0}

        cfg_preco = precificacao.obter_config(user_id)
        cands = []
        for it in shop_itens:
            sku = it.get("item_sku") or it.get("sku")
            base = cat.get(sku)
            if not base:
                continue
            saldo = float(base.get("saldo") or 0)
            if saldo <= 0:  # sem estoque não dá pra impulsionar
                continue
            custo = float(base.get("custo") or 0)
            preco = float(base.get("preco") or 0)
            margem = 0.0
            if preco > 0:
                av = precificacao.avaliar_com_cfg(cfg_preco, custo, preco, "shopee")
                margem = av["margem_atual"] if av["margem_atual"] is not None else (av["margem_sugerida"] or 0)
            cands.append({"item_id": str(it.get("item_id")), "nome": it.get("item_name") or sku,
                          "saldo": saldo, "margem": margem})

        cands.sort(key=lambda x: x["margem"] if estr == "margem" else x["saldo"], reverse=True)
        escolhidos = cands[:teto]

        # substitui só os automáticos; preserva manuais/fixos
        for a in db.query(ShopeeBoostItem).filter_by(user_id=user_id, auto=True).all():
            db.delete(a)
        db.flush()
        manuais = {i.item_id for i in db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()}
        n = 0
        for c in escolhidos:
            if c["item_id"] in manuais:
                continue
            db.add(ShopeeBoostItem(user_id=user_id, item_id=c["item_id"], nome=c["nome"],
                                   auto=True, prioridade=0))
            n += 1
        cfg.auto_estrategia = estr
        db.commit()
        return {"acao": "ok", "estrategia": estr, "selecionados": n, "candidatos": len(cands)}
    finally:
        db.close()
