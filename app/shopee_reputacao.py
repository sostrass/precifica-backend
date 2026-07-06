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
                               "ultima_ts": 0, "criticas": 0, "pedidos": set(), "itens": []})
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
        # dossiê: guarda até 8 avaliações do comprador
        if len(a["itens"]) < 8:
            a["itens"].append({"nota": c.get("rating_star"), "produto_id": c.get("item_id"),
                               "comentario": (c.get("comment") or "")[:120],
                               "ts": ct * 1000 if ct else None,
                               "respondida": _respondida(c), "midia": _tem_midia(c)})
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
                      "ultima_ts": a["ultima_ts"] * 1000 if a["ultima_ts"] else None,
                      "itens": sorted(a["itens"], key=lambda x: -(x["ts"] or 0))})
    lista.sort(key=lambda x: (-x["score"], -x["avaliacoes"]))
    kpis = {"total": len(lista),
            "promotores": sum(1 for x in lista if x["classe"] == "promotor"),
            "neutros": sum(1 for x in lista if x["classe"] == "neutro"),
            "criticos": sum(1 for x in lista if x["classe"] == "critico")}
    criticos = [x for x in lista if x["classe"] == "critico"][:4]
    tops = [x for x in lista if x["classe"] != "critico"][:limite - len(criticos)]
    return {"kpis": kpis, "destaques": tops + criticos}


# --------------------------------------------------------------------------- #
# Linha do tempo da reputação — marcos reais derivados dos dados
# --------------------------------------------------------------------------- #
def _linha_do_tempo(user_id: int, avaliacoes: list, limite: int = 16) -> list:
    """Feed cronológico de marcos: críticas, prova social (foto/vídeo), cliente fiel
    (recompra) e respostas do copiloto. Tudo com timestamp real — nada fabricado."""
    eventos = []
    pedidos_buyer: dict = {}
    for c in avaliacoes:
        b = c.get("buyer_username")
        if b and c.get("order_sn"):
            pedidos_buyer.setdefault(b, set()).add(c.get("order_sn"))
    ordenadas = sorted(avaliacoes, key=lambda c: c.get("create_time") or 0, reverse=True)
    for c in ordenadas[:70]:
        ct = (c.get("create_time") or 0) * 1000
        s = c.get("rating_star") or 0
        b = c.get("buyer_username") or "comprador"
        prod = c.get("produto_nome") or ("#" + str(c.get("item_id")))
        iid = str(c.get("item_id")) if c.get("item_id") else None
        if s <= 2:
            eventos.append({"tipo": "critica", "tom": "danger", "quando": ct, "titulo": "Crítica recebida",
                            "texto": f"{b} deu {s}★ em {prod}", "item_id": iid, "nome": prod})
        elif _tem_midia(c) and s >= 4:
            eventos.append({"tipo": "prova_social", "tom": "roxo", "quando": ct, "titulo": "Prova social nova",
                            "texto": f"{b} postou foto/vídeo com {s}★ em {prod}", "item_id": iid, "nome": prod})
        elif len(pedidos_buyer.get(b, set())) >= 2 and s >= 4:
            eventos.append({"tipo": "recompra", "tom": "gold", "quando": ct, "titulo": "Cliente fiel avaliou",
                            "texto": f"{b} ({len(pedidos_buyer[b])} pedidos) deu {s}★ em {prod}", "item_id": iid, "nome": prod})
    try:
        from . import shopee_reviews
        for e in shopee_reviews.historico_log(user_id, 20):
            q = e.get("quando")
            when = None
            if q:
                try:
                    when = int(datetime.fromisoformat(q).replace(tzinfo=timezone.utc).timestamp() * 1000)
                except Exception:  # noqa: BLE001
                    when = None
            eventos.append({"tipo": "agente", "tom": "ok", "quando": when, "titulo": "Copiloto respondeu",
                            "texto": f"Respondeu {e.get('nota')}★ de {e.get('buyer') or 'cliente'}"
                                     + (f" em {str(e.get('produto'))[:28]}" if e.get("produto") else ""),
                            "item_id": None, "nome": e.get("produto")})
    except Exception:  # noqa: BLE001
        pass
    eventos.sort(key=lambda x: x["quando"] or 0, reverse=True)
    return eventos[:limite]


# --------------------------------------------------------------------------- #
# Insights acionáveis do agente — cada um com uma ação de verdade
# --------------------------------------------------------------------------- #
def _insights(user_id: int, avaliacoes: list, produtos: list, compradores: dict) -> list:
    """Achados priorizados com botão de ação: ver críticas de um produto, mandar produto
    pro Boost (prova social parada), responder crítica antiga, cuidar de um VIP."""
    out = []
    piores = [p for p in produtos if p["avaliacoes"] >= 3 and p["pct_criticas"] >= 25]
    if piores:
        p = piores[0]
        out.append({"tipo": "lote_defeito", "tom": "danger", "icone": "alerta",
                    "titulo": "Produto puxando a nota pra baixo",
                    "texto": f'{p["nome"]} acumula {p["pct_criticas"]}% de críticas em {p["avaliacoes"]} '
                             f'avaliações (nota {p["media"]}). Vale revisar o lote e responder rápido.',
                    "acao": "ver_criticas", "item_id": p["item_id"], "nome": p["nome"], "rotulo": "Ver críticas"})
    parados = [p for p in produtos if p["media"] >= 4.5 and p["avaliacoes"] >= 5 and not p["no_boost"]]
    if parados:
        p = max(parados, key=lambda x: x["avaliacoes"])
        out.append({"tipo": "prova_social", "tom": "gold", "icone": "foguete",
                    "titulo": "Prova social parada",
                    "texto": f'{p["nome"]} tem {p["media"]}★ com {p["avaliacoes"]} avaliações e não está no Boost. '
                             f'Transforme essa reputação em vendas colocando no destaque.',
                    "acao": "mandar_boost", "item_id": p["item_id"], "nome": p["nome"], "rotulo": "Mandar pro Boost"})
    abertas = sorted([c for c in avaliacoes if (c.get("rating_star") or 5) <= 2 and not _respondida(c)],
                     key=lambda c: c.get("create_time") or 0)
    if abertas:
        c = abertas[0]
        dias = int((time.time() - (c.get("create_time") or time.time())) / 86400)
        prod = c.get("produto_nome") or ("#" + str(c.get("item_id")))
        quando = "hoje" if dias <= 0 else (f"há {dias} dia" + ("s" if dias > 1 else ""))
        out.append({"tipo": "critica_aberta", "tom": "warn", "icone": "relogio",
                    "titulo": "Crítica esperando você",
                    "texto": f'Uma crítica de {c.get("buyer_username") or "cliente"} em {prod} está sem resposta {quando}. '
                             f'Responder rápido segura a nota e mostra cuidado.',
                    "acao": "ver_criticas", "item_id": str(c.get("item_id")) if c.get("item_id") else None,
                    "nome": prod, "rotulo": "Responder agora"})
    promotores = [d["usuario"] for d in (compradores.get("destaques") or []) if d.get("classe") == "promotor"]
    for u in promotores[:5]:
        suas = sorted([c for c in avaliacoes if c.get("buyer_username") == u],
                      key=lambda c: c.get("create_time") or 0, reverse=True)
        if suas and not _respondida(suas[0]):
            n = len(suas)
            out.append({"tipo": "vip", "tom": "azul", "icone": "coroa",
                        "titulo": "VIP sem retorno",
                        "texto": f'{u} é promotor da loja ({n} avaliação' + ("es" if n > 1 else "") +
                                 ") e a última ainda não foi respondida. Um obrigado caprichado fideliza.",
                        "acao": "ver_comprador", "usuario": u, "nome": u, "rotulo": "Abrir dossiê"})
            break
    return out[:4]


def _reputacao_produtos(user_id: int, avaliacoes: list, limite: int = 40) -> list:
    """Nota média, volume, % de críticas e tendência por produto (item_id), cruzado com
    a fila do Boost e as ofertas ativas. Enriquece nome/imagem via nomes_itens."""
    agora = time.time()
    d30 = agora - 30 * 86400
    agg: dict = {}
    for c in avaliacoes:
        iid = c.get("item_id")
        if not iid:
            continue
        a = agg.setdefault(iid, {"item_id": iid, "n": 0, "soma": 0, "criticas": 0,
                                 "com_midia": 0, "rec_soma": 0, "rec_n": 0})
        s = c.get("rating_star") or 0
        a["n"] += 1
        a["soma"] += s
        if s <= 2:
            a["criticas"] += 1
        if _tem_midia(c):
            a["com_midia"] += 1
        if (c.get("create_time") or 0) >= d30:  # janela recente p/ tendência
            a["rec_soma"] += s
            a["rec_n"] += 1
    if not agg:
        return []
    # cruza com Boost e ofertas
    try:
        from .models import ShopeeBoostItem
        with SessionLocal() as db:
            na_fila = {str(i.item_id) for i in db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()}
    except Exception:  # noqa: BLE001
        na_fila = set()
    try:
        ofertas = shopee.itens_em_campanha(user_id)
    except Exception:  # noqa: BLE001
        ofertas = set()
    try:
        meta = shopee.nomes_itens(user_id, list(agg.keys())[:100])
    except Exception:  # noqa: BLE001
        meta = {}
    out = []
    for iid, a in agg.items():
        media = round(a["soma"] / a["n"], 2) if a["n"] else 0
        rec = round(a["rec_soma"] / a["rec_n"], 2) if a["rec_n"] else None
        tend = None
        if rec is not None and a["n"] > a["rec_n"]:
            antiga = round((a["soma"] - a["rec_soma"]) / (a["n"] - a["rec_n"]), 2)
            tend = "sobe" if rec > antiga + 0.05 else ("cai" if rec < antiga - 0.05 else "estavel")
        m = meta.get(iid) or {}
        out.append({"item_id": str(iid), "nome": m.get("nome") or ("#" + str(iid)),
                    "imagem": m.get("imagem"), "media": media, "avaliacoes": a["n"],
                    "criticas": a["criticas"],
                    "pct_criticas": round(a["criticas"] / a["n"] * 100) if a["n"] else 0,
                    "pct_midia": round(a["com_midia"] / a["n"] * 100) if a["n"] else 0,
                    "tendencia": tend, "no_boost": str(iid) in na_fila,
                    "em_oferta": str(iid).isdigit() and int(iid) in ofertas})
    out.sort(key=lambda x: (x["media"], -x["avaliacoes"]))  # piores primeiro
    return out[:limite]


# --------------------------------------------------------------------------- #
# Temas por IA — lê os comentários e extrai temas com sentimento (cacheado)
# --------------------------------------------------------------------------- #
_TEMAS_CACHE: dict = {}
_TEMAS_TTL = 6 * 3600


def temas_ia(user_id: int, avaliacoes: list = None, forcar: bool = False) -> dict:
    ch = _TEMAS_CACHE.get(user_id)
    if not forcar and ch and time.time() - ch[0] < _TEMAS_TTL:
        return ch[1]
    if avaliacoes is None:
        avaliacoes = _coletar(user_id)
    coments = [(c.get("rating_star") or 0, (c.get("comment") or "").strip())
               for c in avaliacoes if (c.get("comment") or "").strip()]
    if len(coments) < 5:
        res = {"disponivel": False, "motivo": "poucos comentários", "analisados": len(coments)}
        _TEMAS_CACHE[user_id] = (time.time(), res)
        return res
    amostra = coments[:180]
    linhas = "\n".join(f"[{s}★] {t[:180]}" for s, t in amostra)
    prompt = (
        "Você é analista de reputação de uma loja de armarinho na Shopee. Abaixo estão avaliações "
        "de clientes (nota + comentário). Identifique de 4 a 6 TEMAS recorrentes (ex.: qualidade, "
        "cor fiel ao anúncio, entrega/prazo, embalagem, quantidade/rendimento, atendimento). "
        "Para cada tema devolva: nome curto, total de menções, quantas positivas e quantas negativas. "
        "Some também 2-3 frases curtas que os clientes mais ELOGIAM e 2-3 que mais RECLAMAM. "
        "Responda SOMENTE um JSON válido, sem markdown, no formato: "
        '{"temas":[{"tema":"...","mencoes":N,"positivas":N,"negativas":N}],'
        '"elogios":["...","..."],"reclamacoes":["...","..."]}\n\nAVALIAÇÕES:\n' + linhas
    )
    try:
        from . import ai
        import json as _json
        bruto = (ai._gerar_texto(user_id, prompt) or "").strip()
        if bruto.startswith("```"):
            bruto = bruto.strip("`")
            bruto = bruto[bruto.find("{"):bruto.rfind("}") + 1]
        elif "{" in bruto:
            bruto = bruto[bruto.find("{"):bruto.rfind("}") + 1]
        dados = _json.loads(bruto)
        temas = []
        for t in (dados.get("temas") or [])[:6]:
            men = int(t.get("mencoes") or 0)
            pos = int(t.get("positivas") or 0)
            neg = int(t.get("negativas") or 0)
            temas.append({"tema": str(t.get("tema") or "").strip()[:40],
                          "mencoes": men, "positivas": pos, "negativas": neg})
        res = {"disponivel": True, "analisados": len(amostra), "temas": temas,
               "elogios": [str(x)[:120] for x in (dados.get("elogios") or [])[:3]],
               "reclamacoes": [str(x)[:120] for x in (dados.get("reclamacoes") or [])[:3]]}
    except Exception as e:  # noqa: BLE001
        res = {"disponivel": False, "motivo": f"IA indisponível: {e}", "analisados": len(amostra)}
    _TEMAS_CACHE[user_id] = (time.time(), res)
    return res


# --------------------------------------------------------------------------- #
# Dossiê do comprador — todos os pedidos/avaliações de um comprador
# --------------------------------------------------------------------------- #
def dossie(user_id: int, usuario: str) -> dict:
    avals = _coletar(user_id)
    minhas = [c for c in avals if c.get("buyer_username") == usuario]
    if not minhas:
        return {"usuario": usuario, "encontrado": False}
    minhas.sort(key=lambda c: c.get("create_time") or 0, reverse=True)
    n = len(minhas)
    soma = sum(c.get("rating_star") or 0 for c in minhas)
    pedidos = {c.get("order_sn") for c in minhas if c.get("order_sn")}
    com_midia = sum(1 for c in minhas if _tem_midia(c))
    respondidas = sum(1 for c in minhas if _respondida(c))
    ids = list({c.get("item_id") for c in minhas if c.get("item_id")})
    try:
        meta = shopee.nomes_itens(user_id, ids[:50])
    except Exception:  # noqa: BLE001
        meta = {}
    linha = [{"item_id": str(c.get("item_id")), "produto": (meta.get(c.get("item_id")) or {}).get("nome") or ("#" + str(c.get("item_id"))),
              "imagem": (meta.get(c.get("item_id")) or {}).get("imagem"),
              "nota": c.get("rating_star"), "comentario": (c.get("comment") or "").strip(),
              "quando": (c.get("create_time") or 0) * 1000, "respondida": _respondida(c),
              "tem_midia": _tem_midia(c)} for c in minhas[:20]]
    return {"usuario": usuario, "encontrado": True, "avaliacoes": n, "pedidos": len(pedidos),
            "media": round(soma / n, 1) if n else 0, "com_midia": com_midia,
            "respondidas": respondidas, "linha_do_tempo": linha}


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

    compradores = _radar_compradores(avals)
    produtos = _reputacao_produtos(user_id, avals)
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
        "compradores": compradores,
        "produtos": produtos,
        "linha_tempo": _linha_do_tempo(user_id, avals),
        "insights": _insights(user_id, avals, produtos, compradores),
        "saude": _saude_conta(user_id),
        "config": cfg,
        "atividade": ativ,
    }


# --------------------------------------------------------------------------- #
# Temas por IA — análise de sentimento/assuntos dos comentários (cacheado)
# --------------------------------------------------------------------------- #
_TEMAS: dict = {}
_TEMAS_TTL = 3600


def temas(user_id: int, forcar: bool = False) -> dict:
    """Lê os comentários coletados e pede à IA para agrupar em temas com sentimento
    (positivo/negativo) e citações reais. Cacheado 1h — é uma chamada de LLM."""
    import json
    from . import ai
    ch = _TEMAS.get(user_id)
    if not forcar and ch and time.time() - ch[0] < _TEMAS_TTL:
        return ch[1]
    avals = _coletar(user_id)
    coments = [(c.get("rating_star"), (c.get("comment") or "").strip())
               for c in avals if (c.get("comment") or "").strip()]
    if len(coments) < 5:
        res = {"disponivel": False, "motivo": "poucos comentários", "total": len(coments)}
        _TEMAS[user_id] = (time.time(), res)
        return res
    amostra = coments[:200]
    linhas = "\n".join(f"[{n}★] {t[:160]}" for n, t in amostra)
    prompt = (
        "Você analisa avaliações de uma loja de armarinho/bijuteria na Shopee. Abaixo há "
        f"{len(amostra)} comentários de clientes (com a nota). Agrupe em até 6 TEMAS recorrentes "
        "(ex.: qualidade, cor/aparência, entrega/prazo, embalagem, quantidade/rendimento, atendimento). "
        "Para cada tema, estime quantos comentários positivos e negativos o citam e traga 1 citação curta "
        "real de cada lado quando houver. Responda SOMENTE um JSON válido, sem texto fora dele, no formato: "
        '{"temas":[{"tema":"...","positivos":N,"negativos":N,"exemplo_positivo":"...","exemplo_negativo":"..."}],'
        '"resumo_positivo":"frase curta do que mais elogiam","resumo_negativo":"frase curta do que mais reclamam"}\n\n'
        f"COMENTÁRIOS:\n{linhas}"
    )
    try:
        bruto = ai._gerar_texto(user_id, prompt)
        txt = bruto.strip()
        if txt.startswith("```"):
            txt = txt.split("```")[1] if "```" in txt[3:] else txt
            txt = txt.replace("json", "", 1).strip("` \n")
        ini, fim = txt.find("{"), txt.rfind("}")
        dados = json.loads(txt[ini:fim + 1])
        temas_lista = []
        for t in (dados.get("temas") or [])[:6]:
            pos, neg = int(t.get("positivos") or 0), int(t.get("negativos") or 0)
            temas_lista.append({"tema": t.get("tema") or "—", "positivos": pos, "negativos": neg,
                                "total": pos + neg, "exemplo_positivo": t.get("exemplo_positivo"),
                                "exemplo_negativo": t.get("exemplo_negativo")})
        temas_lista.sort(key=lambda x: -x["total"])
        res = {"disponivel": True, "analisados": len(amostra), "temas": temas_lista,
               "resumo_positivo": dados.get("resumo_positivo"), "resumo_negativo": dados.get("resumo_negativo")}
    except Exception as e:  # noqa: BLE001
        res = {"disponivel": False, "motivo": f"IA não retornou análise válida ({e})", "total": len(coments)}
    _TEMAS[user_id] = (time.time(), res)
    return res
