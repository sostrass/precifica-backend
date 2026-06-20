"""Radar de concorrência — camada de histórico/persistência.

O scraper (scraper.py) busca o preço de AGORA. Este módulo guarda esses preços ao
longo do tempo (snapshots) e calcula o histórico/estatísticas que a tela do radar
mostra (gráfico de 7/30 dias, menor/maior/moda/último).

Importante e honesto: o histórico NÃO aparece instantâneo. Ele nasce do acúmulo de
varreduras em dias diferentes — antes de algumas coletas, o gráfico fica vazio.
Essa é a natureza do problema (nenhum marketplace entrega histórico do concorrente
pronto; a gente constrói por snapshots).

As estatísticas são puras e testáveis. A varredura (que chama o scraper na rede)
não é testável aqui sem internet.
"""

from collections import Counter
from datetime import datetime, timedelta

from .db import SessionLocal
from .models import RadarAlvo, RadarSnapshot


def _r2(v):
    return round(float(v), 2) if v is not None else None


# --------------------------------------------------------------------------- #
# ESTATÍSTICAS (puras, testáveis)
# --------------------------------------------------------------------------- #
def estatisticas(precos) -> dict:
    """Resumo de uma lista de preços (em ordem cronológica)."""
    vals = [float(p) for p in precos if p is not None and float(p) > 0]
    if not vals:
        return {"n": 0, "ultimo": None, "menor": None, "maior": None,
                "moda": None, "media": None}
    contagem = Counter(round(v, 2) for v in vals)
    moda = contagem.most_common(1)[0][0]
    return {
        "n": len(vals),
        "ultimo": _r2(vals[-1]),       # snapshot mais recente (lista vem em ordem)
        "menor": _r2(min(vals)),
        "maior": _r2(max(vals)),
        "moda": _r2(moda),
        "media": _r2(sum(vals) / len(vals)),
    }


# --------------------------------------------------------------------------- #
# ALVOS (CRUD simples)
# --------------------------------------------------------------------------- #
def adicionar_alvo(user_id, sku, url, nome=None, marketplace=None) -> dict:
    with SessionLocal() as db:
        alvo = RadarAlvo(user_id=user_id, sku=sku, url=url, nome=nome,
                         marketplace=marketplace, ativo=True)
        db.add(alvo)
        db.commit()
        db.refresh(alvo)
        return _alvo_dict(alvo)


def listar_alvos(user_id, sku=None) -> list:
    with SessionLocal() as db:
        q = db.query(RadarAlvo).filter(RadarAlvo.user_id == user_id)
        if sku:
            q = q.filter(RadarAlvo.sku == sku)
        return [_alvo_dict(a) for a in q.order_by(RadarAlvo.criado_em.desc()).all()]


def remover_alvo(user_id, alvo_id) -> bool:
    with SessionLocal() as db:
        alvo = db.query(RadarAlvo).filter(RadarAlvo.id == alvo_id,
                                          RadarAlvo.user_id == user_id).first()
        if not alvo:
            return False
        db.delete(alvo)
        db.commit()
        return True


def _alvo_dict(a: RadarAlvo) -> dict:
    return {"id": a.id, "sku": a.sku, "nome": a.nome, "marketplace": a.marketplace,
            "url": a.url, "ativo": bool(a.ativo)}


# --------------------------------------------------------------------------- #
# SNAPSHOTS + HISTÓRICO
# --------------------------------------------------------------------------- #
def registrar_snapshot(user_id, alvo_id, preco_oferta=None, preco_normal=None) -> dict:
    """Guarda uma foto de preço de um alvo (valida o dono)."""
    with SessionLocal() as db:
        alvo = db.query(RadarAlvo).filter(RadarAlvo.id == alvo_id,
                                          RadarAlvo.user_id == user_id).first()
        if not alvo:
            return {"ok": False, "erro": "Alvo não encontrado."}
        snap = RadarSnapshot(user_id=user_id, alvo_id=alvo_id,
                             preco_oferta=preco_oferta, preco_normal=preco_normal)
        db.add(snap)
        db.commit()
        db.refresh(snap)
        return {"ok": True, "id": snap.id, "coletado_em": snap.coletado_em.isoformat() + "Z"}


def varrer(user_id, sku) -> dict:
    """Roda o scraper em cada alvo ativo do SKU e guarda o snapshot.

    Não testável aqui (depende de internet). Retorna o preço encontrado por alvo.
    """
    from . import scraper  # import tardio

    alvos = [a for a in listar_alvos(user_id, sku) if a["ativo"]]
    resultados = []
    for a in alvos:
        achado = scraper.buscar_preco(a["url"])
        preco = achado.get("preco")
        if preco is not None:
            registrar_snapshot(user_id, a["id"], preco_oferta=preco)
        resultados.append({"alvo_id": a["id"], "nome": a["nome"],
                           "marketplace": a["marketplace"], "preco": _r2(preco),
                           "fonte": achado.get("fonte"), "erro": achado.get("erro")})
    return {"sku": sku, "varridos": len(resultados), "resultados": resultados}


def varrer_usuario(user_id) -> dict:
    """Varre todos os SKUs com alvos ativos de um tenant (botão 'varrer tudo')."""
    skus = sorted({a["sku"] for a in listar_alvos(user_id) if a["ativo"]})
    total = 0
    for sku in skus:
        try:
            total += varrer(user_id, sku).get("varridos", 0)
        except Exception:  # noqa: BLE001 — um SKU que falha não derruba o resto
            continue
    return {"skus": len(skus), "varridos": total}


def varrer_todos() -> dict:
    """Varre os alvos ativos de TODOS os tenants — usado pelo agendador em segundo plano."""
    with SessionLocal() as db:
        users = [u for (u,) in db.query(RadarAlvo.user_id)
                 .filter(RadarAlvo.ativo.is_(True)).distinct().all()]
    total = 0
    for uid in users:
        try:
            total += varrer_usuario(uid).get("varridos", 0)
        except Exception:  # noqa: BLE001
            continue
    return {"tenants": len(users), "varridos": total}


def historico(user_id, sku, dias=7) -> dict:
    """Série temporal por concorrente + estatísticas do período (para o gráfico)."""
    desde = datetime.utcnow() - timedelta(days=int(dias))
    with SessionLocal() as db:
        alvos = db.query(RadarAlvo).filter(RadarAlvo.user_id == user_id,
                                           RadarAlvo.sku == sku).all()
        alvo_map = {a.id: a for a in alvos}
        ids = list(alvo_map.keys()) or [-1]
        snaps = (db.query(RadarSnapshot)
                 .filter(RadarSnapshot.user_id == user_id,
                         RadarSnapshot.alvo_id.in_(ids),
                         RadarSnapshot.coletado_em >= desde)
                 .order_by(RadarSnapshot.coletado_em.asc()).all())

    series = {}
    todos = []
    for s in snaps:
        preco = s.preco_oferta if s.preco_oferta is not None else s.preco_normal
        if preco is None:
            continue
        a = alvo_map.get(s.alvo_id)
        if s.alvo_id not in series:
            series[s.alvo_id] = {"alvo_id": s.alvo_id,
                                 "nome": a.nome if a else "?",
                                 "marketplace": a.marketplace if a else None,
                                 "pontos": []}
        series[s.alvo_id]["pontos"].append(
            {"data": s.coletado_em.isoformat() + "Z", "preco": _r2(preco)})
        todos.append(preco)

    return {"sku": sku, "dias": int(dias), "series": list(series.values()),
            "estatisticas": estatisticas(todos)}


# --------------------------------------------------------------------------- #
# ALERTAS — destaca mudanças que pedem ação (puro sobre snapshots, testável)
# --------------------------------------------------------------------------- #
_SEV_ORDEM = {"alta": 0, "media": 1, "baixa": 2}


def alertas(user_id, dias=7, limiar_pct=5.0, sku=None) -> dict:
    """Varre o histórico recente e devolve o que mudou: quedas, altas e novos mínimos.

    O agendador varre em silêncio; isto traduz os snapshots em avisos acionáveis.
    Não chama a rede — só lê o que já foi coletado.
    """
    desde = datetime.utcnow() - timedelta(days=int(dias))
    with SessionLocal() as db:
        q = db.query(RadarAlvo).filter(RadarAlvo.user_id == user_id,
                                       RadarAlvo.ativo.is_(True))
        if sku:
            q = q.filter(RadarAlvo.sku == sku)
        alvos = q.all()
        alvo_map = {a.id: a for a in alvos}
        ids = list(alvo_map.keys()) or [-1]
        snaps = (db.query(RadarSnapshot)
                 .filter(RadarSnapshot.user_id == user_id,
                         RadarSnapshot.alvo_id.in_(ids),
                         RadarSnapshot.coletado_em >= desde)
                 .order_by(RadarSnapshot.coletado_em.asc()).all())

    por_alvo = {}
    for s in snaps:
        preco = s.preco_oferta if s.preco_oferta is not None else s.preco_normal
        if preco is None or float(preco) <= 0:
            continue
        por_alvo.setdefault(s.alvo_id, []).append((s.coletado_em, float(preco)))

    limiar = float(limiar_pct) / 100.0
    out = []
    for alvo_id, pts in por_alvo.items():
        a = alvo_map.get(alvo_id)
        nome = a.nome if a else "?"
        precos = [p for (_, p) in pts]
        quando, atual = pts[-1]
        base = {"alvo_id": alvo_id, "nome": nome,
                "marketplace": a.marketplace if a else None,
                "sku": a.sku if a else None,
                "preco": _r2(atual), "em": quando.isoformat() + "Z", "_ts": quando}

        if len(precos) >= 2 and precos[-2] > 0:
            var = (precos[-1] - precos[-2]) / precos[-2]
            pct = round(var * 100, 1)
            if var <= -limiar:
                sev = "alta" if abs(var) >= 0.10 else "media"
                out.append({**base, "tipo": "queda", "severidade": sev, "variacao_pct": pct,
                            "mensagem": f"{nome} baixou {abs(pct)}% — agora R$ {_r2(atual)}"})
            elif var >= limiar:
                out.append({**base, "tipo": "alta", "severidade": "baixa", "variacao_pct": pct,
                            "mensagem": f"{nome} subiu {pct}% — agora R$ {_r2(atual)}"})

        if len(precos) >= 3 and atual <= min(precos):
            out.append({**base, "tipo": "menor", "severidade": "media", "variacao_pct": None,
                        "mensagem": f"{nome} no menor preço em {int(dias)} dias — R$ {_r2(atual)}"})

    out.sort(key=lambda x: x["_ts"], reverse=True)                 # mais recente primeiro
    out.sort(key=lambda x: _SEV_ORDEM.get(x["severidade"], 9))     # e severidade no topo (estável)
    for x in out:
        x.pop("_ts", None)

    resumo = {"total": len(out),
              "alta": sum(1 for x in out if x["severidade"] == "alta"),
              "media": sum(1 for x in out if x["severidade"] == "media"),
              "baixa": sum(1 for x in out if x["severidade"] == "baixa")}
    return {"dias": int(dias), "alertas": out, "resumo": resumo}


# --------------------------------------------------------------------------- #
# RADAR + DECISÃO (liga o histórico ao motor de decisão)
# --------------------------------------------------------------------------- #
def precos_atuais(user_id, sku) -> list:
    """Último preço conhecido de cada alvo ativo do SKU (1 por concorrente)."""
    with SessionLocal() as db:
        alvos = (db.query(RadarAlvo)
                 .filter(RadarAlvo.user_id == user_id, RadarAlvo.sku == sku,
                         RadarAlvo.ativo.is_(True)).all())
        out = []
        for a in alvos:
            snap = (db.query(RadarSnapshot)
                    .filter(RadarSnapshot.alvo_id == a.id)
                    .order_by(RadarSnapshot.coletado_em.desc()).first())
            if not snap:
                continue
            preco = snap.preco_oferta if snap.preco_oferta is not None else snap.preco_normal
            if preco is not None:
                out.append({"alvo_id": a.id, "nome": a.nome, "marketplace": a.marketplace,
                            "preco": _r2(preco),
                            "coletado_em": snap.coletado_em.isoformat() + "Z"})
        return out


def recomendar(user_id, sku, *, custo_base, preco_atual, canal="mercadolivre",
               comissao=None, fixo=None, imposto=0.0, cartao=0.0,
               piso_margem=15.0, estrategia="match", delta=1.0, delta_tipo="pct") -> dict:
    """Recomendação geral do SKU + por concorrente, usando o motor de decisão.

    O 'geral' decide olhando todos os concorrentes juntos. O 'por concorrente' diz,
    para cada anúncio, o que fazer se você responder àquele preço — sempre travado
    no piso de viabilidade.
    """
    from . import decisao  # import tardio

    atuais = precos_atuais(user_id, sku)
    kw = dict(custo_base=custo_base, preco_atual=preco_atual, canal=canal,
              comissao=comissao, fixo=fixo, imposto=imposto, cartao=cartao,
              piso_margem=piso_margem, estrategia=estrategia, delta=delta,
              delta_tipo=delta_tipo)
    geral = decisao.decidir_preco(precos_concorrentes=[x["preco"] for x in atuais], **kw)
    por_concorrente = []
    for x in atuais:
        d = decisao.decidir_preco(precos_concorrentes=[x["preco"]], **kw)
        por_concorrente.append({**x, "acao": d["acao"],
                                "preco_recomendado": d["preco_recomendado"],
                                "margem_recomendado": d["margem_recomendado"],
                                "abaixo_do_piso": d["abaixo_do_piso"], "motivo": d["motivo"]})
    return {"sku": sku, "canal": canal, "preco_atual": geral["preco_atual"],
            "preco_piso": geral["preco_piso"], "geral": geral,
            "concorrentes": por_concorrente}
