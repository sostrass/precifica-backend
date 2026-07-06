"""Central de Reputação — agregador do painel Enterprise de avaliações da Shopee.

Coleta as avaliações reais via get_comment (paginado, cacheado), e monta em uma
passada: KPIs, distribuição de notas, tendência diária, radar de compradores
(score próprio derivado dos dados) e saúde da conta (account_health). Reaproveita
o motor de respostas do shopee_reviews (config, mutirão, log) sem duplicar nada.
"""
import time
from datetime import datetime, timezone, timedelta

from . import shopee
from .db import SessionLocal

BR = timezone(timedelta(hours=-3))

# cache: {user_id: (ts, lista_avaliacoes)}
_COLETA: dict = {}
_COLETA_TTL = 900  # 15 min


# --------------------------------------------------------------------------- #
# Coleta paginada (cacheada) — base de tudo
# --------------------------------------------------------------------------- #
def _coletar(user_id: int, max_paginas: int = 30, forcar: bool = False) -> list:
    """Todas as avaliações (status ALL) até max_paginas x 100. Cache de 15 min.
    Cada item: dict cru do get_comment (comment_id, comment, buyer_username,
    order_sn, item_id, rating_star, create_time, comment_reply, media...)."""
    ch = _COLETA.get(user_id)
    if not forcar and ch and time.time() - ch[0] < _COLETA_TTL:
        return ch[1]
    out, cursor = [], ""
    for _ in range(max_paginas):
        try:
            r = shopee.comentarios_brutos(user_id, status="ALL", cursor=cursor, limite=100)
        except shopee.ShopeeError:
            break
        lote = r.get("item_comment_list") or []
        out.extend(lote)
        if not r.get("more") or not r.get("next_cursor"):
            break
        cursor = r.get("next_cursor")
    _COLETA[user_id] = (time.time(), out)
    return out


def _respondida(c: dict) -> bool:
    rep = c.get("comment_reply") or {}
    return bool((rep.get("reply") or "").strip())


def _tem_midia(c: dict) -> bool:
    m = c.get("media") or {}
    return bool((m.get("image_url_list") or []) or (m.get("video_url_list") or []))


# --------------------------------------------------------------------------- #
# Radar de compradores — score próprio (dados reais, sem inventar nada)
# --------------------------------------------------------------------------- #
def _radar_compradores(avaliacoes: list, limite: int = 12) -> dict:
    """Agrega por buyer_username: quantas avaliações, média que dá, mídia enviada,
    recência. Score 0-100: começa em 50; +10 por avaliação (cap 3), +média*6 - 18
    (5.0 => +12, 3.0 => 0, 1.0 => -12), +6 se envia mídia. Classes:
    promotor (média>=4.5), neutro (3<=média<4.5), critico (média<3)."""
    agg: dict = {}
    for c in avaliacoes:
        u = c.get("buyer_username")
        if not u:
            continue
        a = agg.setdefault(u, {"usuario": u, "avaliacoes": 0, "soma": 0, "midia": 0,
                               "ultima_ts": 0, "criticas": 0, "pedidos": set()})
        a["avaliacoes"] += 1
        a["soma"] += c.get("rating_star") or 0
        if _tem_midia(c):
            a["midia"] += 1
        if (c.get("rating_star") or 5) <= 2:
            a["criticas"] += 1
        ct = c.get("create_time") or 0
        if ct > a["ultima_ts"]:
            a["ultima_ts"] = ct
        if c.get("order_sn"):
            a["pedidos"].add(c["order_sn"])
    lista = []
    for a in agg.values():
        n = a["avaliacoes"]
        media = a["soma"] / n if n else 0
        score = 50 + min(n, 3) * 10 + (media * 6 - 18) + (6 if a["midia"] else 0)
        score = max(0, min(100, round(score)))
        classe = "promotor" if media >= 4.5 else ("neutro" if media >= 3 else "critico")
        lista.append({"usuario": a["usuario"], "avaliacoes": n, "pedidos": len(a["pedidos"]),
                      "media": round(media, 1), "com_midia": a["midia"],
                      "criticas": a["criticas"], "classe": classe, "score": score,
                      "ultima_ts": a["ultima_ts"] * 1000 if a["ultima_ts"] else None})
    lista.sort(key=lambda x: (-x["score"], -x["avaliacoes"]))
    kpis = {"total": len(lista),
            "promotores": sum(1 for x in lista if x["classe"] == "promotor"),
            "neutros": sum(1 for x in lista if x["classe"] == "neutro"),
            "criticos": sum(1 for x in lista if x["classe"] == "critico")}
    # destaque: melhores + os críticos (que pedem atenção)
    criticos = [x for x in lista if x["classe"] == "critico"][:4]
    tops = [x for x in lista if x["classe"] != "critico"][:limite - len(criticos)]
    return {"kpis": kpis, "destaques": tops + criticos}


# --------------------------------------------------------------------------- #
# Saúde da conta (account_health) — defensivo: formatos variam por região
# --------------------------------------------------------------------------- #
def _saude_conta(user_id: int) -> dict:
    try:
        r = shopee.desempenho_loja(user_id)
    except Exception:  # noqa: BLE001
        return {"disponivel": False}
    resp = (r or {}).get("response") or {}
    if not resp:
        return {"disponivel": False}
    out = {"disponivel": True, "nivel": None, "metricas": []}
    # get_shop_performance: overall_performance {rating, fulfillment_failed, listing_failed, custom_service_failed}
    ov = resp.get("overall_performance") or {}
    if ov:
        out["nivel"] = ov.get("rating")  # 1 Poor, 2 ImprovementNeeded, 3 Good, 4 Excellent
        rot = {"fulfillment_failed": "Indicadores de envio com falha",
               "listing_failed": "Anúncios com problema",
               "custom_service_failed": "Atendimento com falha"}
        for k, label in rot.items():
            if k in ov:
                out["metricas"].append({"chave": k, "rotulo": label, "valor": ov.get(k)})
    # metric_list (formato detalhado): pega até 6 métricas com meta
    for m in (resp.get("metric_list") or [])[:6]:
        out["metricas"].append({"chave": m.get("metric_name"), "rotulo": m.get("metric_name"),
                                "valor": m.get("current_period"),
                                "meta": m.get("target", {}).get("value") if isinstance(m.get("target"), dict) else m.get("target")})
    return out


# --------------------------------------------------------------------------- #
# Painel — tudo em uma chamada
# --------------------------------------------------------------------------- #
def painel(user_id: int, forcar: bool = False) -> dict:
    from . import shopee_reviews
    avals = _coletar(user_id, forcar=forcar)
    agora = time.time()
    d30 = agora - 30 * 86400
    d14 = agora - 14 * 86400

    total = len(avals)
    respondidas = sum(1 for c in avals if _respondida(c))
    sem_resposta = total - respondidas
    soma = sum(c.get("rating_star") or 0 for c in avals)
    media_geral = round(soma / total, 2) if total else None

    ult30 = [c for c in avals if (c.get("create_time") or 0) >= d30]
    dist = {s: 0 for s in (5, 4, 3, 2, 1)}
    com_midia_30 = 0
    for c in ult30:
        s = c.get("rating_star") or 0
        if s in dist:
            dist[s] += 1
        if _tem_midia(c):
            com_midia_30 += 1
    n30 = len(ult30)
    pct5_30 = round(dist[5] / n30 * 100) if n30 else None
    pct_midia_30 = round(com_midia_30 / n30 * 100) if n30 else None
    criticas_abertas = sum(1 for c in avals
                           if (c.get("rating_star") or 5) <= 2 and not _respondida(c))

    # tendência: média diária das novas avaliações, 14 dias (BR)
    dias = {}
    for d in range(13, -1, -1):
        k = datetime.fromtimestamp(agora - d * 86400, tz=BR).strftime("%d/%m")
        dias[k] = {"d": k, "soma": 0, "n": 0}
    for c in avals:
        ct = c.get("create_time") or 0
        if ct < d14:
            continue
        k = datetime.fromtimestamp(ct, tz=BR).strftime("%d/%m")
        if k in dias:
            dias[k]["soma"] += c.get("rating_star") or 0
            dias[k]["n"] += 1
    tendencia = [{"d": v["d"], "media": round(v["soma"] / v["n"], 2) if v["n"] else None,
                  "n": v["n"]} for v in dias.values()]

    # ritmo e meta 4.90 (projeção honesta; None quando não dá pra estimar)
    por_dia = round(n30 / 30, 1) if n30 else 0
    meta = None
    if media_geral and total and media_geral < 4.9 and por_dia > 0:
        # quantas 5★ puras faltam p/ média >= 4.90: (soma + 5x) / (total + x) >= 4.9
        faltam = int(max(0, (4.9 * total - soma) / 0.1)) + 1
        dias_meta = int(faltam / max(por_dia * (dist[5] / n30 if n30 else 0.9), 0.1))
        meta = {"alvo": 4.9, "faltam_5estrelas": faltam, "dias_estimados": dias_meta}

    # respondidas hoje (log do agente) + total do dia
    try:
        log = shopee_reviews.historico_log(user_id, 100)
    except Exception:  # noqa: BLE001
        log = []
    hoje = datetime.now(BR).strftime("%d/%m")
    ia_hoje = 0
    for e in log:
        try:
            q = e.get("quando")
            if q and datetime.fromisoformat(q).replace(tzinfo=timezone.utc).astimezone(BR).strftime("%d/%m") == hoje:
                ia_hoje += 1
        except Exception:  # noqa: BLE001
            continue

    try:
        cfg = shopee_reviews.obter_config(user_id)
    except Exception:  # noqa: BLE001
        cfg = {}
    try:
        ativ = shopee_reviews.atividade(user_id)
    except Exception:  # noqa: BLE001
        ativ = {}

    return {
        "coletadas": total,
        "kpis": {
            "media_geral": media_geral,
            "total": total,
            "sem_resposta": sem_resposta,
            "respondidas": respondidas,
            "taxa_resposta": round(respondidas / total * 100, 1) if total else None,
            "ia_hoje": ia_hoje,
            "pct5_30": pct5_30,
            "pct_midia_30": pct_midia_30,
            "novas_30d": n30,
            "por_dia": por_dia,
            "criticas_abertas": criticas_abertas,
        },
        "distribuicao": [{"estrelas": s, "qtd": dist[s]} for s in (5, 4, 3, 2, 1)],
        "tendencia": tendencia,
        "meta": meta,
        "compradores": _radar_compradores(avals),
        "saude": _saude_conta(user_id),
        "config": cfg,
        "atividade": ativ,
    }
