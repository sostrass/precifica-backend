"""
Motor de construção de campanhas Shopee — Descontos, Cupons, Bundle, Add-on e Flash Sale.

Construído a partir dos critérios OFICIAIS da Shopee (OpenAPI v2 + Seller Education):

DESCONTO (discount)
  - Duração máxima 180 dias; início no futuro.
  - Preço promocional < preço corrente, POR VARIAÇÃO (model_list) quando houver.
  - Máx ~1000 itens por campanha. Item já em outro desconto no período é recusado.

CUPOM (voucher)
  - reward_type=2 (percentual) EXIGE `max_price` (teto do desconto em R$) — sem ele a
    Shopee recusa/zera o benefício. reward_type=1 usa `discount_amount`.
  - Início no futuro; duração máxima ~90 dias (3 meses). Código alfanumérico.

BUNDLE (bundle_deal)
  - Duração máxima 180 dias; início >= ~1h no futuro.
  - TODOS os itens do combo precisam compartilhar canal de logística com o primeiro.
  - Item não pode estar em OUTRO bundle no mesmo período, nem ser "main" de add-on.

ADD-ON (add_on_deal)
  - Início no futuro; fim >= início+1h; fim <= início+3 meses.
  - Preço do adicional DEVE ser < preço corrente do produto (menor variação).
  - Main não pode estar em bundle no período nem ser main de outro add-on;
    adicionais não podem estar em flash/shocking sale. Mains e adicionais devem
    compartilhar >= 1 canal de logística. Máx 1000 mains / 100 adicionais.

FLASH SALE (shop_flash_sale)
  - Somente em slot oficial liberado pela loja; preço por variação < corrente;
    estoque promocional >= 1. Item em add-on (como adicional) é recusado.

Este módulo PRESERVA os gatilhos e parâmetros do motor automático (shopee_promo_auto):
ele só assume a etapa de CONSTRUÇÃO+ENVIO+VERIFICAÇÃO. Toda criação é verificada por
reconsulta (quantos itens realmente entraram) e TUDO é logado no stdout (Railway).
"""
from __future__ import annotations

import logging
import re
import time

from . import shopee

log = logging.getLogger("precifica.campanhas")

DIA = 86400
HORA = 3600


def slots_oficiais(user_id: int, dias: int = 14) -> dict:
    """Aberturas (slots) oficiais de Flash Sale da loja, na rota correta da API
    (/shop_flash_sale/get_time_slot_id). A Shopee exige start_time >= o relógio DELES;
    como há latência/decalque, pedimos com margem de futuro e, se ainda reclamar,
    aumentamos a margem progressivamente (2min → 15min → 1h)."""
    ultimo_erro = None
    for margem in (120, 900, 3600):
        agora = int(time.time())
        try:
            r = shopee._chamar(user_id, "/api/v2/shop_flash_sale/get_time_slot_id",
                               extra={"start_time": agora + margem,
                                      "end_time": agora + margem + max(1, dias) * DIA})
            lista = (r.get("response") or []) if isinstance(r.get("response"), list) else \
                ((r.get("response") or {}).get("time_slot_list") or (r.get("response") or {}).get("timeslot_list") or [])
            log.info("CAMPANHA[flash] slots oficiais %dd (margem %ds): %d abertura(s)", dias, margem, len(lista))
            return {"response": {"timeslot_list": lista}, "request_id": r.get("request_id")}
        except shopee.ShopeeError as e:
            texto = str(e).lower()
            if "start_time" in texto or "param" in texto:
                log.warning("CAMPANHA[flash] slots: Shopee rejeitou margem %ds (%s) — tentando maior", margem, e)
                ultimo_erro = e
                continue
            raise
    raise ultimo_erro or shopee.ShopeeError("A Shopee rejeitou a consulta de horários.")


# ------------------------------------------------------------------ helpers --
def _menor_preco_corrente(user_id: int, item_id: int) -> float:
    """Menor preço vigente do anúncio (entre as variações; ou o item-base)."""
    try:
        ml = shopee.modelos_item(user_id, int(item_id))
    except shopee.ShopeeError:
        ml = []
    precos = [shopee._preco_modelo(m) for m in (ml or [])]
    precos = [p for p in precos if p and p > 0]
    if precos:
        return min(precos)
    try:
        nm = (shopee.nomes_itens(user_id, [int(item_id)]) or {}).get(int(item_id), {}) or {}
        return float(nm.get("preco") or 0)
    except shopee.ShopeeError:
        return 0.0


def _logisticas(user_id: int, item_ids: list) -> dict:
    """Canais de logística HABILITADOS por item (get_item_base_info.logistic_info)."""
    out = {}
    ids = [int(i) for i in item_ids if i]
    for i in range(0, len(ids), 50):
        lote = ids[i:i + 50]
        try:
            r = shopee._chamar(user_id, "/api/v2/product/get_item_base_info",
                               extra={"item_id_list": ",".join(str(x) for x in lote)})
            for it in (r.get("response") or {}).get("item_list") or []:
                canais = {li.get("logistic_id") for li in (it.get("logistic_info") or [])
                          if li.get("enabled")}
                out[int(it.get("item_id"))] = canais
        except shopee.ShopeeError as e:
            log.warning("CAMPANHA logisticas: falha ao consultar lote %s: %s", lote, e)
    return out


def _validar_janela(tipo: str, inicio: int, fim: int) -> str | None:
    """Valida a janela contra as regras oficiais. Retorna a mensagem do problema ou None."""
    agora = int(time.time())
    if inicio <= agora:
        return "o início precisa ser no futuro"
    if fim <= inicio:
        return "o fim precisa ser depois do início"
    dur = fim - inicio
    if tipo in ("desconto", "bundle") and dur > 180 * DIA:
        return "duração máxima de 180 dias"
    if tipo in ("cupom", "addon") and dur > 92 * DIA:
        return "duração máxima de 3 meses"
    if tipo == "addon" and dur < HORA:
        return "o fim precisa ser pelo menos 1h depois do início"
    return None


def _resultado(tipo: str, pid, itens_ok: int, recusados: list, aviso: str | None = None) -> dict:
    r = {"ok": bool(pid), "tipo": tipo, "id": pid,
         "itens_adicionados": itens_ok, "itens_recusados": recusados}
    if aviso:
        r["aviso"] = aviso
    if recusados:
        r["aviso"] = (r.get("aviso") or "") + (" | " if r.get("aviso") else "") + \
            f"{len(recusados)} item(ns) recusado(s): " + \
            "; ".join(f"#{x['item_id']} — {x['motivo']}" for x in recusados[:5])
    return r


# ------------------------------------------------------------------ CUPOM ----
def criar_cupom_verificado(user_id: int, payload: dict) -> dict:
    """Cria cupom com as regras oficiais (max_price no percentual) e confirma na Shopee."""
    nome = (payload.get("nome") or "Cupom").strip()[:40]
    codigo = re.sub(r"[^A-Za-z0-9]", "", str(payload.get("codigo") or ""))[:16]
    inicio, fim = int(payload["inicio"]), int(payload["fim"])
    tipo_desc = int(payload.get("tipo_desconto") or 2)   # 1=R$, 2=%
    valor = float(payload.get("valor") or 0)
    minc = float(payload.get("compra_minima") or 0)
    quota = int(payload.get("quantidade") or 100)
    escopo = int(payload.get("escopo") or 1)

    prob = _validar_janela("cupom", inicio, fim)
    if prob:
        raise shopee.ShopeeError(f"Janela inválida: {prob}.")
    if not codigo or len(codigo) < 3:
        raise shopee.ShopeeError("Código do cupom: use 3 a 16 letras/números.")
    if tipo_desc == 2 and not (1 <= valor <= 99):
        raise shopee.ShopeeError("Percentual do cupom deve estar entre 1% e 99%.")

    # REGRA OFICIAL: percentual exige teto do desconto (max_price)
    teto = payload.get("teto_desconto")
    if tipo_desc == 2:
        if not teto or float(teto) <= 0:
            teto = round(max(5.0, (minc if minc > 0 else 100.0) * valor / 100.0), 2)
            log.info("CAMPANHA[cupom] teto do desconto ausente — aplicando derivado R$ %.2f", teto)
        teto = float(teto)

    extra = {"voucher_name": nome, "voucher_code": codigo,
             "start_time": inicio, "end_time": fim,
             "voucher_type": 1 if escopo == 1 else 2,
             "reward_type": tipo_desc,
             "min_basket_price": minc, "usage_quantity": quota}
    if tipo_desc == 2:
        extra["percentage"] = int(valor)
        extra["max_price"] = teto
    else:
        extra["discount_amount"] = valor
    if escopo == 2 and payload.get("item_ids"):
        extra["item_id_list"] = [int(i) for i in payload["item_ids"]][:50]

    log.info("CAMPANHA[cupom] criando '%s' code=%s %s%s min=%.2f quota=%d",
             nome, codigo, valor, "%" if tipo_desc == 2 else " R$", minc, quota)
    r = shopee._chamar(user_id, "/api/v2/voucher/add_voucher", metodo="POST", extra=extra)
    vid = (r.get("response") or {}).get("voucher_id")
    if not vid:
        log.warning("CAMPANHA[cupom] Shopee não retornou voucher_id | resp=%s", r)
        raise shopee.ShopeeError(f"A Shopee não criou o cupom: {r.get('message') or r.get('error') or 'sem detalhe'}")
    log.info("CAMPANHA[cupom] criado voucher_id=%s", vid)
    return {"ok": True, "tipo": "cupom", "id": vid, "voucher_id": vid,
            "itens_adicionados": None, "itens_recusados": [],
            "teto_desconto": extra.get("max_price")}


# ---------------------------------------------------------------- DESCONTO ---
def criar_desconto_verificado(user_id: int, nome: str, inicio: int, fim: int, itens: list) -> dict:
    """Cria desconto (preço por variação via wrapper) e VERIFICA quantos itens entraram."""
    prob = _validar_janela("desconto", inicio, fim)
    if prob:
        raise shopee.ShopeeError(f"Janela inválida: {prob}.")
    enviados = [int(i.get("item_id")) for i in itens if i.get("item_id")]
    log.info("CAMPANHA[desconto] criando '%s' com %d itens", nome, len(enviados))
    r = shopee.criar_desconto(user_id, nome, inicio, fim, itens)
    did = r.get("discount_id") or (r.get("response") or {}).get("discount_id")
    ok, dentro = 0, set()
    if did:
        try:
            det = shopee.detalhe_desconto(user_id, did)
            dentro = {int(x["item_id"]) for x in det.get("itens") or []}
            ok = len(dentro)
        except shopee.ShopeeError as e:
            log.warning("CAMPANHA[desconto] verificação falhou: %s", e)
            ok = int(r.get("itens_adicionados") or 0)
    recusados = [{"item_id": i, "motivo": "recusado pela Shopee (preço >= vigente, sem estoque ou já em campanha)"}
                 for i in enviados if i not in dentro] if dentro or ok == 0 else []
    if recusados:
        log.warning("CAMPANHA[desconto] id=%s: %d de %d itens ENTRARAM; recusados=%s",
                    did, ok, len(enviados), [x["item_id"] for x in recusados])
    else:
        log.info("CAMPANHA[desconto] id=%s: %d de %d itens confirmados", did, ok, len(enviados))
    out = _resultado("desconto", did, ok, recusados)
    out["discount_id"] = did
    return out


# ------------------------------------------------------------------ BUNDLE ---
def criar_bundle_verificado(user_id: int, payload: dict) -> dict:
    """Cria bundle com pré-validação oficial (logística comum, janela) e verificação real."""
    nome = (payload.get("nome") or "Combo").strip()[:40]
    inicio, fim = int(payload["inicio"]), int(payload["fim"])
    rule = int(payload.get("rule_type") or 2)
    valor = float(payload.get("valor") or 10)
    min_itens = int(payload.get("min_itens") or 2)
    ids = [int(i) for i in (payload.get("item_ids") or []) if i]

    prob = _validar_janela("bundle", inicio, fim)
    if prob:
        raise shopee.ShopeeError(f"Janela inválida: {prob}.")
    if len(ids) < 1:
        raise shopee.ShopeeError("Escolha ao menos 1 produto para o combo (o ideal são 2+).")

    # REGRA OFICIAL: logística comum com o primeiro item
    recusados = []
    if len(ids) > 1:
        canais = _logisticas(user_id, ids)
        base = canais.get(ids[0], set())
        aceitos = [ids[0]]
        for i in ids[1:]:
            if base and canais.get(i) and not (base & canais[i]):
                recusados.append({"item_id": i, "motivo": "logística diferente do 1º item (regra do bundle)"})
            else:
                aceitos.append(i)
        ids = aceitos
    log.info("CAMPANHA[bundle] criando '%s' rule=%s valor=%s min=%s itens=%s pré-recusados=%d",
             nome, rule, valor, min_itens, ids, len(recusados))

    r = shopee.criar_bundle(user_id, nome, inicio, fim, rule, valor, min_itens, ids)
    bid = r.get("bundle_deal_id") or (r.get("response") or {}).get("bundle_deal_id")
    ok, dentro = 0, set()
    if bid:
        try:
            det = shopee.detalhe_bundle(user_id, bid)
            dentro = {int(x["item_id"]) for x in det.get("itens") or []}
            ok = len(dentro)
        except shopee.ShopeeError as e:
            log.warning("CAMPANHA[bundle] verificação falhou: %s", e)
    for i in ids:
        if dentro and i not in dentro:
            recusados.append({"item_id": i, "motivo": "recusado pela Shopee (já em outro combo/add-on no período, esgotado ou inativo)"})
    if ok < min_itens:
        log.warning("CAMPANHA[bundle] id=%s tem %d item(ns) — combo 'compre %d' precisa de itens suficientes para o cliente montar", bid, ok, min_itens)
    if recusados:
        log.warning("CAMPANHA[bundle] id=%s: %d itens ENTRARAM; recusados=%s", bid, ok, [x["item_id"] for x in recusados])
    else:
        log.info("CAMPANHA[bundle] id=%s: %d itens confirmados", bid, ok)
    out = _resultado("bundle", bid, ok, recusados)
    out["bundle_deal_id"] = bid
    return out


# ------------------------------------------------------------------ ADD-ON ---
def criar_addon_verificado(user_id: int, payload: dict) -> dict:
    """Cria add-on com pré-validação oficial (preço do adicional < corrente; logística;
    janela 1h–3meses) e verificação real de principais e adicionais."""
    nome = (payload.get("nome") or "Add-on").strip()[:40]
    inicio, fim = int(payload["inicio"]), int(payload["fim"])
    ptype = int(payload.get("promotion_type") or 0)
    principais = [int(i) for i in (payload.get("principais") or []) if i]
    adicionais_in = payload.get("adicionais") or []

    prob = _validar_janela("addon", inicio, fim)
    if prob:
        raise shopee.ShopeeError(f"Janela inválida: {prob}.")
    if not principais:
        raise shopee.ShopeeError("Escolha ao menos 1 produto principal.")
    if not adicionais_in:
        raise shopee.ShopeeError("Escolha ao menos 1 adicional/brinde.")

    # REGRA OFICIAL: preço do adicional < preço corrente (menor variação)
    recusados, adicionais = [], []
    for a in adicionais_in[:100]:
        iid = int(a.get("item_id"))
        preco_env = float(a.get("add_on_deal_price") or 0)
        corrente = _menor_preco_corrente(user_id, iid)
        if ptype == 1:
            adicionais.append({"item_id": iid, "add_on_deal_price": 0})
            continue
        if corrente > 0 and preco_env >= corrente:
            novo = round(corrente * 0.95, 2)
            log.info("CAMPANHA[addon] adicional #%s: preço %.2f >= corrente %.2f — ajustado para %.2f",
                     iid, preco_env, corrente, novo)
            preco_env = novo
        if preco_env <= 0:
            recusados.append({"item_id": iid, "motivo": "sem preço válido para o adicional"})
            continue
        adicionais.append({"item_id": iid, "add_on_deal_price": preco_env})
    if not adicionais:
        raise shopee.ShopeeError("Nenhum adicional com preço válido (precisa ser menor que o preço vigente).")

    # REGRA OFICIAL: logística comum entre mains e com adicionais
    canais = _logisticas(user_id, principais + [a["item_id"] for a in adicionais])
    base = canais.get(principais[0], set())
    principais_ok = [principais[0]]
    for i in principais[1:1000]:
        if base and canais.get(i) and not (base & canais[i]):
            recusados.append({"item_id": i, "motivo": "logística diferente do 1º principal (regra do add-on)"})
        else:
            principais_ok.append(i)

    log.info("CAMPANHA[addon] criando '%s' tipo=%s principais=%s adicionais=%s pré-recusados=%d",
             nome, ptype, principais_ok, [a["item_id"] for a in adicionais], len(recusados))
    r = shopee.criar_addon(user_id, nome, inicio, fim, principais_ok, adicionais, ptype)
    aid = r.get("add_on_deal_id") or (r.get("response") or {}).get("add_on_deal_id")
    p_ok = s_ok = 0
    if aid:
        try:
            det = shopee.detalhe_addon(user_id, aid)
            dentro_p = {int(x["item_id"]) for x in det.get("principais") or []}
            dentro_s = {int(x["item_id"]) for x in det.get("adicionais") or []}
            p_ok, s_ok = len(dentro_p), len(dentro_s)
            for i in principais_ok:
                if i not in dentro_p:
                    recusados.append({"item_id": i, "motivo": "principal recusado (em bundle/outro add-on no período, esgotado ou inativo)"})
            for a in adicionais:
                if a["item_id"] not in dentro_s:
                    recusados.append({"item_id": a["item_id"], "motivo": "adicional recusado (preço, flash sale no período, esgotado ou inativo)"})
        except shopee.ShopeeError as e:
            log.warning("CAMPANHA[addon] verificação falhou: %s", e)
    if recusados:
        log.warning("CAMPANHA[addon] id=%s: %d principais + %d adicionais ENTRARAM; recusados=%s",
                    aid, p_ok, s_ok, [x["item_id"] for x in recusados])
    else:
        log.info("CAMPANHA[addon] id=%s: %d principais + %d adicionais confirmados", aid, p_ok, s_ok)
    out = _resultado("addon", aid, p_ok + s_ok, recusados)
    out.update({"add_on_deal_id": aid, "principais_ok": p_ok, "adicionais_ok": s_ok})
    return out


# ------------------------------------------------------------------ FLASH ----
def habilitar_flash_itens(user_id: int, flash_sale_id: int) -> dict:
    """HABILITA os itens de uma Flash Sale existente (eles nascem desabilitados no slot,
    como no Seller Center) e depois ativa a própria oferta. Retorna contagens reais."""
    fid = int(flash_sale_id)
    habilitados, falhas = 0, []
    # 1) ler os itens/modelos aceitos na oferta
    try:
        ri = shopee._chamar(user_id, "/api/v2/shop_flash_sale/get_shop_flash_sale_items",
                            extra={"flash_sale_id": fid, "offset": 0, "limit": 100})
        resp = ri.get("response") or {}
        modelos = resp.get("models") or []
        item_info = resp.get("item_info") or []
    except shopee.ShopeeError as e:
        log.warning("CAMPANHA[flash] id=%s não consegui ler os itens p/ habilitar: %s", fid, e)
        modelos, item_info = [], []
    # 2) montar o update com status=1 por modelo (mantendo preço/estoque aceitos)
    por_item = {}
    for m in modelos:
        iid = int(m.get("item_id") or 0)
        por_item.setdefault(iid, []).append({
            "model_id": int(m.get("model_id") or 0),
            "status": 1,
            "input_promo_price": m.get("input_promotion_price") or m.get("input_promo_price") or m.get("promotion_price_with_tax"),
            "stock": m.get("campaign_stock") or m.get("stock") or 1,
        })
    if not por_item and item_info:  # itens sem variação podem vir só em item_info
        for it in item_info:
            por_item[int(it.get("item_id") or 0)] = []
    itens_upd = []
    limites = {int(i.get("item_id") or 0): int(i.get("purchase_limit") or 0) for i in item_info}
    for iid, models in por_item.items():
        ent = {"item_id": iid, "purchase_limit": limites.get(iid, 0)}
        if models:
            ent["models"] = [{k: v for k, v in m.items() if v is not None} for m in models]
        itens_upd.append(ent)
    if itens_upd:
        try:
            ru = shopee._chamar(user_id, "/api/v2/shop_flash_sale/update_shop_flash_sale_items",
                                metodo="POST", extra={"flash_sale_id": fid, "items": itens_upd})
            rresp = ru.get("response") or {}
            fitems = rresp.get("failed_items") or []
            for f in fitems:
                if isinstance(f, dict):
                    falhas.append({"item_id": f.get("item_id"),
                                   "motivo": f.get("fail_message") or f.get("err_msg") or f.get("unqualified_condition") or "não habilitado"})
            log.info("CAMPANHA[flash] id=%s habilitação de itens enviada (%d itens, %d falhas) | resp=%s",
                     fid, len(itens_upd), len(fitems), str(rresp)[:400])
        except shopee.ShopeeError as e:
            log.warning("CAMPANHA[flash] id=%s falha ao habilitar itens: %s | payload=%s", fid, e, str(itens_upd)[:400])
    # 3) ativar a oferta em si
    ativada = False
    try:
        ru = shopee._chamar(user_id, "/api/v2/shop_flash_sale/update_shop_flash_sale",
                            metodo="POST", extra={"flash_sale_id": fid, "status": 1})
        ativada = not ru.get("error")
        log.info("CAMPANHA[flash] id=%s oferta ATIVADA (status=1)", fid)
    except shopee.ShopeeError as e:
        log.warning("CAMPANHA[flash] id=%s falha ao ativar a oferta: %s", fid, e)
    # 4) verificação final
    item_count = enabled = None
    try:
        rg = shopee._chamar(user_id, "/api/v2/shop_flash_sale/get_shop_flash_sale",
                            extra={"flash_sale_id": fid})
        resp = rg.get("response") or {}
        item_count = resp.get("item_count")
        enabled = resp.get("enabled_item_count")
        habilitados = int(enabled or 0)
        log.info("CAMPANHA[flash] id=%s verificação: item_count=%s enabled_item_count=%s status=%s",
                 fid, item_count, enabled, resp.get("status"))
    except shopee.ShopeeError as e:
        log.warning("CAMPANHA[flash] id=%s verificação falhou: %s", fid, e)
    return {"ok": True, "flash_sale_id": fid, "ativada": ativada,
            "itens": item_count, "habilitados": habilitados, "falhas": falhas}


def criar_flash_verificado(user_id: int, timeslot_id: int, itens: list, reserva: int = 0) -> dict:
    """Cria Flash Sale no slot oficial, com os CRITÉRIOS NATIVOS do slot aplicados
    (estoque de campanha 1~1000, desconto 1~99%, máx 50 itens habilitados), preço POR
    VARIAÇÃO, e — regra oficial que faltava — ATIVA a oferta ao final
    (update_shop_flash_sale status=1: ela nasce desabilitada). Verifica por
    enabled_item_count/item_count e lê failed_items."""
    if not timeslot_id:
        raise shopee.ShopeeError("Escolha um horário (slot) oficial da Shopee.")
    from . import shopee_promo_auto as motor
    if itens and not (itens[0] or {}).get("models") and any(i.get("desconto_pct") for i in itens):
        itens = motor._flash_itens(user_id, itens, int(reserva or 0))

    # CRITÉRIOS DO SLOT (painel oficial): estoque de campanha 1~1000; desconto 1~99%; máx 50 itens
    itens = itens[:50]
    for it in itens:
        it["purchase_limit"] = int(it.get("purchase_limit") or 0)
        for m in it.get("models") or []:
            m["stock"] = max(1, min(1000, int(m.get("stock") or 1)))
        if "stock" in it and not it.get("models"):
            it["stock"] = max(1, min(1000, int(it.get("stock") or 1)))
    enviados = [int(i.get("item_id")) for i in itens if i.get("item_id")]
    log.info("CAMPANHA[flash] criando no slot %s com %d itens (critérios do slot aplicados)",
             timeslot_id, len(enviados))
    r = shopee.criar_flash(user_id, int(timeslot_id), itens)
    fid = (r.get("response") or {}).get("flash_sale_id") or r.get("flash_sale_id")
    falhas = (r.get("response") or {}).get("failed_items") or r.get("failed_items") or []
    recusados = []
    for f in falhas:
        if isinstance(f, dict):
            recusados.append({"item_id": f.get("item_id"),
                              "motivo": f.get("fail_message") or f.get("err_msg") or f.get("fail_error") or "recusado pela Shopee"})
    if not fid:
        log.warning("CAMPANHA[flash] Shopee não retornou flash_sale_id | resp=%s", r)
        raise shopee.ShopeeError("A Shopee não criou a Oferta Relâmpago (loja pode não estar elegível ou o slot expirou).")

    # REGRA OFICIAL (duas camadas): habilitar os ITENS (nascem desabilitados no slot)
    # e depois ATIVAR a oferta (status=1)
    hab = habilitar_flash_itens(user_id, int(fid))
    ativada = bool(hab.get("ativada"))
    habilitados = hab.get("habilitados")
    for f in hab.get("falhas") or []:
        recusados.append(f)

    ok = int(hab.get("itens") or 0)
    if ok == 0:
        try:
            det = shopee.detalhe_flash(user_id, fid)
            ok = len(det.get("itens") or [])
        except shopee.ShopeeError:
            pass
    if ok == 0 and not recusados:
        recusados = [{"item_id": i, "motivo": "recusado pela Shopee (fora dos critérios do slot: estoque 1~1000, desconto 1~99%, ou item em outra promoção)"}
                     for i in enviados]
    if recusados:
        log.warning("CAMPANHA[flash] id=%s: %d de %d itens ENTRARAM; recusados=%s",
                    fid, ok, len(enviados), [(x.get("item_id"), x.get("motivo")) for x in recusados[:5]])
    out = _resultado("flash", fid, ok, recusados)
    out["flash_sale_id"] = fid
    out["ativada"] = ativada
    out["habilitados"] = habilitados
    if not ativada:
        out["aviso"] = (out.get("aviso") or "") + (" | " if out.get("aviso") else "") + \
            "a oferta foi criada mas NÃO pôde ser ativada — ative no Seller Center"
    return out
