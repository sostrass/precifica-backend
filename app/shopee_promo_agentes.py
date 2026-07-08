"""
Agentes por vendas — estudam os PEDIDOS REAIS da loja Shopee e criam campanhas sozinhos:

  · CUPOM AUTO   → quando as vendas justificam, cria 1 cupom percentual por semana
                   (com teto do desconto, quota controlada) para acelerar conversão.
  · LEVE+ AUTO   → encontra o PAR de produtos mais comprado junto (co-ocorrência nas
                   cestas) e monta o combo "leve 2 e pague menos".
  · ADD-ON AUTO  → pega o produto mais vendido como principal e oferece como adicionais
                   os itens que os clientes costumam levar junto, com desconto.

Regras de segurança:
  - Cada agente cria NO MÁXIMO 1 campanha por semana (guarda no diário ShopeePromoLog,
    motivo "vendas") — nada de spam de campanhas.
  - Só age com amostra mínima de vendas (>= 5 cestas na janela) — sem inventar padrão.
  - Toda criação passa pelo motor verificado (shopee_campanhas) — critérios oficiais,
    verificação real pós-criação e logs CAMPANHA[...] no Railway.
"""
from __future__ import annotations

import logging
import random
import string
import time
from collections import Counter
from datetime import datetime, timedelta

from .db import SessionLocal
from .models import ShopeePromoLog
from . import shopee, shopee_campanhas
from .shopee_promo_auto import obter_config, _registrar_log

log = logging.getLogger("precifica.agentes")

DIA = 86400
JANELA_DIAS = 45          # quanto de histórico de vendas estudar
MIN_CESTAS = 5            # amostra mínima para agir
COOLDOWN_DIAS = 7         # 1 campanha por agente por semana


# ------------------------------------------------------------------ vendas ---
def _cestas(user_id: int, dias: int = JANELA_DIAS) -> list:
    """Lista de cestas [(item_id, qtd), ...] dos pedidos reais na janela."""
    sns, cursor = [], ""
    for _ in range(6):  # até ~300 pedidos
        try:
            r = shopee.listar_pedidos(user_id, dias=dias, cursor=cursor, limite=50)
        except shopee.ShopeeError as e:
            log.warning("AGENTES vendas: listar_pedidos falhou: %s", e)
            break
        resp = r.get("response") or {}
        sns += [o.get("order_sn") for o in resp.get("order_list") or [] if o.get("order_sn")]
        cursor = resp.get("next_cursor") or ""
        if not resp.get("more") or not cursor:
            break
    cestas = []
    for i in range(0, len(sns), 50):
        lote = sns[i:i + 50]
        try:
            d = shopee.detalhe_pedidos(user_id, lote)
        except shopee.ShopeeError as e:
            log.warning("AGENTES vendas: detalhe_pedidos falhou: %s", e)
            continue
        for o in (d.get("response") or {}).get("order_list") or []:
            itens = [(int(x.get("item_id") or 0), int(x.get("model_quantity_purchased") or 1))
                     for x in o.get("item_list") or [] if x.get("item_id")]
            if itens:
                cestas.append(itens)
    log.info("AGENTES vendas: %d pedido(s) estudado(s) na janela de %dd", len(cestas), dias)
    return cestas


def _top_vendidos(cestas: list) -> list:
    c = Counter()
    for cesta in cestas:
        for iid, q in cesta:
            c[iid] += q
    return [iid for iid, _ in c.most_common(20)]


def _pares(cestas: list) -> list:
    """Pares de itens comprados JUNTOS, do mais frequente ao menos."""
    c = Counter()
    for cesta in cestas:
        ids = sorted({iid for iid, _ in cesta})
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                c[(ids[i], ids[j])] += 1
    return [par for par, n in c.most_common(10) if n >= 2]


# ------------------------------------------------------------------ guarda ---
def _criou_recente(user_id: int, tipo: str) -> bool:
    db = SessionLocal()
    try:
        corte = datetime.utcnow() - timedelta(days=COOLDOWN_DIAS)
        q = (db.query(ShopeePromoLog)
             .filter(ShopeePromoLog.user_id == user_id,
                     ShopeePromoLog.tipo == tipo,
                     ShopeePromoLog.motivo == "vendas",
                     ShopeePromoLog.criado_em >= corte)
             .first())
        return q is not None
    finally:
        db.close()


# ------------------------------------------------------------------ agentes --
def _agente_cupom(user_id: int, ex: dict, cestas: list) -> dict | None:
    if _criou_recente(user_id, "cupom"):
        return None
    pct = int(ex.get("cupom_desconto") or 10)
    quota = int(ex.get("cupom_quota") or 100)
    agora = int(time.time())
    codigo = "AUTO" + datetime.now().strftime("%d%m") + "".join(random.choices(string.ascii_uppercase, k=2))
    nome = f"Cupom Auto {datetime.now().strftime('%d/%m')}"
    r = shopee_campanhas.criar_cupom_verificado(user_id, {
        "nome": nome, "codigo": codigo, "inicio": agora + 900, "fim": agora + 7 * DIA,
        "tipo_desconto": 2, "valor": pct, "compra_minima": 0, "quantidade": quota, "escopo": 1,
    })
    _registrar_log(user_id, "cupom", r.get("voucher_id"), nome, quota, pct, "vendas")
    log.info("AGENTES cupom: criado %s (%s, %d%%, quota %d)", nome, codigo, pct, quota)
    return {"tipo": "cupom", "id": r.get("voucher_id"), "nome": nome}


def _agente_bundle(user_id: int, ex: dict, cestas: list) -> dict | None:
    if _criou_recente(user_id, "bundle"):
        return None
    pares = _pares(cestas)
    if not pares:
        log.info("AGENTES leve+: nenhum par comprado junto com frequência — nada a criar")
        return None
    pct = int(ex.get("bundle_desconto") or 10)
    agora = int(time.time())
    a, b = pares[0]
    nome = f"Leve+ Auto {datetime.now().strftime('%d/%m')}"
    r = shopee_campanhas.criar_bundle_verificado(user_id, {
        "nome": nome, "inicio": agora + 3900, "fim": agora + 14 * DIA,
        "rule_type": 2, "valor": pct, "min_itens": 2, "item_ids": [a, b],
    })
    if (r.get("itens_adicionados") or 0) < 2:
        log.warning("AGENTES leve+: par (%s,%s) não entrou completo — %s", a, b, r.get("aviso"))
    _registrar_log(user_id, "bundle", r.get("bundle_deal_id"), nome, r.get("itens_adicionados") or 0, pct, "vendas")
    log.info("AGENTES leve+: criado %s com par mais vendido junto (%s + %s)", nome, a, b)
    return {"tipo": "bundle", "id": r.get("bundle_deal_id"), "nome": nome}


def _agente_addon(user_id: int, ex: dict, cestas: list) -> dict | None:
    if _criou_recente(user_id, "addon"):
        return None
    tops = _top_vendidos(cestas)
    if not tops:
        return None
    principal = tops[0]
    companheiros = []
    for (a, b) in _pares(cestas):
        if a == principal and b != principal:
            companheiros.append(b)
        elif b == principal and a != principal:
            companheiros.append(a)
    if not companheiros:
        companheiros = [i for i in tops[1:3] if i != principal]
    if not companheiros:
        log.info("AGENTES add-on: sem companheiros de compra para o principal — nada a criar")
        return None
    pct = int(ex.get("addon_desconto") or 15)
    agora = int(time.time())
    adicionais = []
    for iid in companheiros[:2]:
        corrente = shopee_campanhas._menor_preco_corrente(user_id, iid)
        if corrente > 0:
            adicionais.append({"item_id": iid, "add_on_deal_price": round(corrente * (1 - pct / 100.0), 2)})
    if not adicionais:
        return None
    nome = f"Add-on Auto {datetime.now().strftime('%d/%m')}"
    r = shopee_campanhas.criar_addon_verificado(user_id, {
        "nome": nome, "inicio": agora + 3900, "fim": agora + 14 * DIA,
        "promotion_type": 0, "principais": [principal], "adicionais": adicionais,
    })
    _registrar_log(user_id, "addon", r.get("add_on_deal_id"), nome,
                   (r.get("principais_ok") or 0) + (r.get("adicionais_ok") or 0), pct, "vendas")
    log.info("AGENTES add-on: criado %s (principal %s + %d adicionais por comportamento de compra)",
             nome, principal, len(adicionais))
    return {"tipo": "addon", "id": r.get("add_on_deal_id"), "nome": nome}


# ------------------------------------------------------------------ ciclo ----
def ciclo(user_id: int) -> dict:
    """Roda os agentes por vendas ativos (chamado pelo agendador, junto do motor)."""
    cfg = obter_config(user_id)
    ex = cfg.get("extras") or {}
    ativos = [t for t, k in (("cupom", "cupom_auto"), ("bundle", "bundle_auto"), ("addon", "addon_auto")) if ex.get(k)]
    if not cfg.get("ativo") or not ativos:
        return {"acao": "inativo"}
    cestas = _cestas(user_id)
    if len(cestas) < MIN_CESTAS:
        log.info("AGENTES: só %d cesta(s) na janela — amostra pequena, nada criado", len(cestas))
        return {"acao": "amostra_pequena", "cestas": len(cestas)}
    criadas, erros = [], []
    for tipo in ativos:
        try:
            fn = {"cupom": _agente_cupom, "bundle": _agente_bundle, "addon": _agente_addon}[tipo]
            r = fn(user_id, ex, cestas)
            if r:
                criadas.append(r)
        except shopee.ShopeeError as e:
            log.warning("AGENTES %s: Shopee recusou: %s", tipo, e)
            erros.append({"tipo": tipo, "erro": str(e)})
        except Exception as e:  # noqa: BLE001
            log.warning("AGENTES %s: falha inesperada: %s", tipo, e)
            erros.append({"tipo": tipo, "erro": str(e)})
    return {"acao": "ok", "criadas": criadas, "erros": erros, "cestas": len(cestas)}
