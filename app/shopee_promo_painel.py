"""Painel consolidado da Central de Promoções (Shopee).

Monta, numa única resposta cacheada, tudo que a Central MAX precisa:
KPIs, termômetro de vendas, insights com ação, agenda (linha do tempo),
vitrine de produtos em campanha (com trava anti-duplicação), campanhas ativas
com dados de ação e status dos 6 motores.

Reaproveita as funções já existentes em shopee.py e shopee_promo_auto.py — não
recria nenhuma chamada de API.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from . import shopee
from . import shopee_promo_auto as motor
from .db import SessionLocal
from .models import ShopeePromoLog

_PAINEL_CACHE: dict = {}     # user_id -> (ts, payload)
_TOTAL_CACHE: dict = {}      # user_id -> (ts, total_anuncios)

# tipos com item-level (foto/preço/timer) para a vitrine
_VITRINE_TIPOS = ("desconto", "flash")


def _total_anuncios(user_id: int, ttl: int = 3600) -> int | None:
    """Total de anúncios ativos — 1 chamada barata (get_item_list total_count)."""
    ag = time.time()
    c = _TOTAL_CACHE.get(user_id)
    if c and ag - c[0] < ttl:
        return c[1]
    total = None
    try:
        r = shopee._chamar(user_id, "/api/v2/product/get_item_list",
                           extra={"offset": 0, "page_size": 1, "item_status": "NORMAL"})
        total = (r.get("response") or {}).get("total_count")
    except Exception:  # noqa: BLE001
        total = None
    _TOTAL_CACHE[user_id] = (ag, total)
    return total


def _vitrine(user_id: int, ativas: list, cap_camp: int = 8, cap_itens: int = 60) -> list:
    """Produtos em campanha (descontos + flash ongoing) com foto, preço de/por,
    desconto e o fim da campanha (para o anel-temporizador). Deduplica por item."""
    agora = int(time.time())
    vistos, out = set(), []
    # ordena: quem termina antes primeiro (mais urgente/relevante)
    ordenadas = sorted([c for c in ativas if c["tipo"] in _VITRINE_TIPOS],
                       key=lambda c: c.get("fim") or 9e18)[:cap_camp]
    for c in ordenadas:
        if len(out) >= cap_itens:
            break
        try:
            if c["tipo"] == "desconto":
                d = shopee.detalhe_desconto(user_id, c["id"])
            else:
                d = shopee.detalhe_flash(user_id, c["id"])
        except Exception:  # noqa: BLE001
            continue
        for it in (d.get("itens") or []):
            iid = it.get("item_id")
            if not iid or iid in vistos:
                continue
            vistos.add(iid)
            po = it.get("preco_promo")
            oo = it.get("preco_original")
            desc = it.get("desconto_pct")
            if desc is None and po and oo and oo > 0:
                desc = round((1 - po / oo) * 100)
            out.append({
                "item_id": iid, "nome": it.get("nome") or f"#{iid}", "imagem": it.get("imagem"),
                "tipo": c["tipo"], "campanha": c.get("nome") or ("Flash" if c["tipo"] == "flash" else "Desconto"),
                "preco_promo": po, "preco_original": oo, "desconto_pct": desc,
                "fim": c.get("fim"), "ao_vivo": c["tipo"] == "flash",
            })
            if len(out) >= cap_itens:
                break
    return out


def _guardiao_reduzidos_30d(user_id: int) -> int:
    """Quantas campanhas o motor registrou nos últimos 30 dias (auditoria do guardião)."""
    db = SessionLocal()
    try:
        desde = datetime.utcnow() - timedelta(days=30)
        return (db.query(ShopeePromoLog)
                .filter(ShopeePromoLog.user_id == user_id, ShopeePromoLog.criado_em >= desde)
                .count())
    except Exception:  # noqa: BLE001
        return 0
    finally:
        db.close()


def _slots_flash_livres(user_id: int) -> int | None:
    try:
        r = shopee.flash_slots(user_id, 7)
        slots = (r.get("response") or {}).get("time_slot_list") or (r.get("response") or {}).get("timeslot_list") or []
        return len(slots) if slots else 0
    except Exception:  # noqa: BLE001
        return None


def _motores(ativas: list, cfg: dict) -> list:
    """Status dos 6 motores (contagem de campanhas ativas por tipo + se o agente cria)."""
    cont: dict = {}
    for c in ativas:
        cont[c["tipo"]] = cont.get(c["tipo"], 0) + 1
    tipo_cfg = (cfg.get("tipo") or "desconto")
    agente_faz = {
        "desconto": tipo_cfg in ("desconto", "ambos"),
        "flash": tipo_cfg in ("flash", "ambos"),
    }
    defs = [
        ("desconto", "Descontos", "shopee"),
        ("flash", "Relâmpago", "accent"),
        ("cupom", "Cupons", "gold"),
        ("bundle", "Leve+ por menos", "purple"),
        ("addon", "Add-on", "teal"),
        ("seguidor", "Prêmio de seguidor", "blue"),
    ]
    return [{"chave": k, "rotulo": rot, "cor": cor, "ativas": cont.get(k, 0),
             "agente": bool(agente_faz.get(k, False))} for k, rot, cor in defs]


def _insights(ativas: list, expiram_24h: list, slots_flash, termometro: dict, cfg: dict) -> list:
    """Achados proativos com ação (cada um tem 'acao' que o frontend liga a um botão)."""
    out = []
    # 0) loja sem nenhuma oferta ativa
    if not ativas:
        out.append({"tipo": "sem_ofertas", "cor": "purple", "titulo": "Loja sem ofertas ativas",
                    "texto": "Nenhuma campanha rodando agora. O Agente de Ofertas acha estoque parado com margem e monta as propostas na hora.",
                    "acao": "relampago", "ref": {}, "cta": "✦ Abrir o Agente"})
    # 1) expira sem auto-continuar
    for c in expiram_24h[:1]:
        horas = max(1, round(((c.get("fim") or 0) - time.time()) / 3600))
        out.append({"tipo": "expira", "cor": "warn", "titulo": "Expira sem renovação",
                    "texto": f'"{c.get("nome") or "Campanha"}" acaba em {horas}h. Renove pra não sair do ar.',
                    "acao": "renovar", "ref": {"tipo": c["tipo"], "id": c["id"], "nome": c.get("nome")},
                    "cta": f"↻ Renovar +7 dias"})
    # 2) slot de flash livre
    if slots_flash:
        out.append({"tipo": "slot", "cor": "accent", "titulo": "Slot de Flash livre",
                    "texto": f"{slots_flash} horário(s) de Flash Sale livres nos próximos 7 dias — aproveite os campeões de giro.",
                    "acao": "agendar_flash", "ref": {}, "cta": "Agendar Flash"})
    # 3) termômetro em queda
    if termometro.get("queda"):
        out.append({"tipo": "queda", "cor": "danger", "titulo": "Vendas caindo",
                    "texto": f'Ritmo {termometro.get("queda_pct")}% abaixo do normal. Dispare uma oferta relâmpago nos campeões.',
                    "acao": "relampago", "ref": {}, "cta": "⚡ Ação relâmpago"})
    # 4) cupom entre as ativas terminando
    cupons_fim = sorted([c for c in ativas if c["tipo"] == "cupom"], key=lambda c: c.get("fim") or 9e18)
    if cupons_fim:
        c = cupons_fim[0]
        horas = max(1, round(((c.get("fim") or 0) - time.time()) / 3600))
        if horas <= 72:
            out.append({"tipo": "cupom", "cor": "gold", "titulo": "Cupom no fim",
                        "texto": f'"{c.get("nome") or "Cupom"}" termina em {horas}h. Vale recarregar se está convertendo.',
                        "acao": "renovar", "ref": {"tipo": "cupom", "id": c["id"], "nome": c.get("nome")},
                        "cta": "↻ Renovar cupom"})
    return out[:4]


def painel(user_id: int, forcar: bool = False) -> dict:
    ag = time.time()
    cache = _PAINEL_CACHE.get(user_id)
    if cache and not forcar and ag - cache[0] < 300:
        return cache[1]

    agora = int(ag)
    # --- agenda / campanhas ---
    try:
        campanhas = shopee.agenda_campanhas(user_id).get("campanhas") or []
    except Exception:  # noqa: BLE001
        campanhas = []
    ativas = [c for c in campanhas if (c.get("inicio") or 0) <= agora and (c.get("fim") or 0) >= agora]
    agendadas = [c for c in campanhas if (c.get("inicio") or 0) > agora]
    expiram_24h = sorted([c for c in ativas if 0 <= (c.get("fim") or 0) - agora <= 86400],
                         key=lambda c: c.get("fim") or 0)

    # --- trava (itens em campanha) ---
    try:
        trava = shopee.itens_em_campanha(user_id)
    except Exception:  # noqa: BLE001
        trava = set()
    em_oferta = len(trava)
    total = _total_anuncios(user_id)
    cobertura = round(em_oferta / total * 100, 1) if (total and total > 0) else None

    # --- vitrine ---
    vitrine = _vitrine(user_id, ativas)
    descs = [v["desconto_pct"] for v in vitrine if v.get("desconto_pct")]
    desconto_medio = round(sum(descs) / len(descs)) if descs else None

    # --- desempenho (gmv/vendas promo 30d) ---
    try:
        dash = shopee.dashboard_promo(user_id, 30)
    except Exception:  # noqa: BLE001
        dash = {"total": {"receita": 0, "unidades": 0, "pedidos": 0}, "por_tipo": [], "top_campanhas": []}
    tot = dash.get("total") or {}
    gmv = tot.get("receita") or 0
    vendas = tot.get("unidades") or 0
    pedidos_promo = tot.get("pedidos") or 0
    ticket = round(gmv / pedidos_promo, 2) if pedidos_promo else None

    # --- termômetro ---
    try:
        termometro = motor.detectar_queda(user_id)
    except Exception as e:  # noqa: BLE001
        termometro = {"queda": False, "motivo": "erro", "msg": str(e)}

    # --- config / motores ---
    try:
        cfg = motor.obter_config(user_id)
    except Exception:  # noqa: BLE001
        cfg = {}
    slots_flash = _slots_flash_livres(user_id)
    guardiao = _guardiao_reduzidos_30d(user_id)

    # --- enriquecer campanhas com dados de ação (contagem via vitrine/dash) ---
    receita_por_camp = {str(c.get("id")): c for c in (dash.get("top_campanhas") or [])}
    itens_por_camp: dict = {}
    for v in vitrine:
        itens_por_camp[v["campanha"]] = itens_por_camp.get(v["campanha"], 0) + 1
    campanhas_ativas = []
    for c in ativas:
        rc = receita_por_camp.get(str(c["id"])) or {}
        campanhas_ativas.append({
            "tipo": c["tipo"], "id": c["id"], "nome": c.get("nome"),
            "inicio": c.get("inicio"), "fim": c.get("fim"),
            "itens": itens_por_camp.get(c.get("nome"), None),
            "vendas": rc.get("unidades"), "receita": rc.get("receita"),
            "expira_em_ms": ((c.get("fim") or 0) - agora) * 1000 if c.get("fim") else None,
        })
    campanhas_ativas.sort(key=lambda c: c.get("fim") or 9e18)

    payload = {
        "kpis": {
            "ativas": len(ativas),
            "agendadas": len(agendadas),
            "itens_em_oferta": em_oferta,
            "cobertura_pct": cobertura,
            "total_anuncios": total,
            "vendas_promo_30d": vendas,
            "gmv_promo_30d": round(gmv, 2),
            "ticket_medio_promo": ticket,
            "desconto_medio": desconto_medio,
            "piso_margem": cfg.get("piso_margem"),
            "guardiao_reduzidos_30d": guardiao,
            "expiram_24h": len(expiram_24h),
            "slots_flash": slots_flash,
        },
        "termometro": termometro,
        "insights": _insights(ativas, expiram_24h, slots_flash, termometro, cfg),
        "agenda": {"campanhas": campanhas, "ativas": len(ativas), "agendadas": len(agendadas)},
        "vitrine": vitrine,
        "campanhas": campanhas_ativas,
        "motores": _motores(ativas, cfg),
        "por_tipo": dash.get("por_tipo") or [],
        "config": cfg,
        "parcial": dash.get("parcial", False),
    }
    _PAINEL_CACHE[user_id] = (ag, payload)
    return payload
