"""Motor de promoções automáticas pelos agentes (Shopee).

O agente:
  1. escolhe produtos sozinho (estoque parado ou margem alta),
  2. calcula um desconto SEGURO que respeita o piso de margem e o teto de desconto,
  3. cria campanha de desconto e/ou oferta relâmpago,
  4. roda por agenda OU quando detecta queda de vendas.

Modos:
  - 'sugerir': monta propostas e devolve para você aprovar (padrão, mais seguro).
  - 'auto': cria as promoções sozinho dentro das regras.

Trava de segurança central: NUNCA descontar abaixo do piso de margem configurado.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

from . import shopee, precificacao
from .db import SessionLocal
from .models import (ShopeePromoConfig, ShopeeVendaSnapshot, ShopeePromoLog)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config(db, user_id: int) -> ShopeePromoConfig:
    c = db.query(ShopeePromoConfig).filter_by(user_id=user_id).first()
    if not c:
        c = ShopeePromoConfig(user_id=user_id)
        db.add(c)
        db.commit()
        db.refresh(c)
    return c


def _serializar(c: ShopeePromoConfig) -> dict:
    return {
        "ativo": bool(c.ativo), "modo": c.modo or "auto",
        "gatilho": c.gatilho or "agendado", "base_comparacao": c.base_comparacao or "dia",
        "estrategia": c.estrategia or "estoque_parado",
        "tipo": c.tipo or "desconto", "desconto_max": c.desconto_max or 15,
        "piso_margem": float(c.piso_margem if c.piso_margem is not None else 10.0),
        "max_produtos": c.max_produtos or 20, "estoque_minimo": c.estoque_minimo or 3,
        "reserva_estoque": c.reserva_estoque if c.reserva_estoque is not None else 1,
        "duracao_dias": c.duracao_dias or 3, "intervalo_dias": c.intervalo_dias or 7,
        "queda_limiar": c.queda_limiar or 30,
        "dias_analise": int(getattr(c, "dias_analise", None) or 30),
        "ultimo_ciclo": c.ultimo_ciclo.isoformat() if c.ultimo_ciclo else None,
    }


def obter_config(user_id: int) -> dict:
    db = SessionLocal()
    try:
        return _serializar(_config(db, user_id))
    finally:
        db.close()


def salvar_config(user_id: int, p: dict) -> dict:
    db = SessionLocal()
    try:
        c = _config(db, user_id)
        if "ativo" in p: c.ativo = bool(p["ativo"])
        if p.get("modo") in ("sugerir", "auto"): c.modo = p["modo"]
        if p.get("gatilho") in ("agendado", "queda"): c.gatilho = p["gatilho"]
        if p.get("base_comparacao") in ("dia", "horario"): c.base_comparacao = p["base_comparacao"]
        if p.get("estrategia") in ("estoque_parado", "margem_alta"): c.estrategia = p["estrategia"]
        if p.get("tipo") in ("desconto", "flash", "ambos"): c.tipo = p["tipo"]
        if "desconto_max" in p: c.desconto_max = max(1, min(int(p["desconto_max"]), 50))
        if "piso_margem" in p: c.piso_margem = max(0.0, min(float(p["piso_margem"]), 90.0))
        if "max_produtos" in p: c.max_produtos = max(1, min(int(p["max_produtos"]), 100))
        if "estoque_minimo" in p: c.estoque_minimo = max(0, int(p["estoque_minimo"]))
        if "reserva_estoque" in p: c.reserva_estoque = max(0, int(p["reserva_estoque"]))
        if "duracao_dias" in p: c.duracao_dias = max(1, min(int(p["duracao_dias"]), 30))
        if "intervalo_dias" in p: c.intervalo_dias = max(1, min(int(p["intervalo_dias"]), 60))
        if "queda_limiar" in p: c.queda_limiar = max(5, min(int(p["queda_limiar"]), 90))
        if "dias_analise" in p: c.dias_analise = max(7, min(int(p["dias_analise"]), 120))
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return _serializar(c)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Motor de margem real (sobre o preço de lista, descontadas as taxas Shopee)
# --------------------------------------------------------------------------- #
def _faixa_shopee(cfg_prec: dict, preco: float):
    canais = cfg_prec.get("canais") or []
    canal = next((c for c in canais if (c.get("canal") or "").lower() == "shopee"), None)
    if not canal:
        return None
    faixas = sorted(canal.get("faixas") or [],
                    key=lambda f: (f.get("ate") is None, f.get("ate") or 0))
    for f in faixas:
        ate = f.get("ate")
        if ate is None or preco <= ate:
            return f
    return faixas[-1] if faixas else None


def _preco_lista_shopee(cfg_prec: dict, base_venda: float):
    """Preço de LISTA na Shopee a partir do líquido-alvo (Preço Bling = base_venda),
    calculado pela precificação (modelo BASE-VENDA). É o preço que o cliente vê e sobre o
    qual o desconto incide — NÃO o Preço Bling cru. Idêntico ao 'pra_netar' do Catálogo."""
    if not base_venda or base_venda <= 0:
        return None
    canais = cfg_prec.get("canais") or []
    canal = next((c for c in canais if (c.get("canal") or "").lower() == "shopee"), None)
    if not canal or not canal.get("faixas"):
        return None
    r = precificacao.precificar_venda_canal(
        float(base_venda), canal["faixas"],
        float(cfg_prec.get("imposto", 0)), float(cfg_prec.get("cartao", 0)),
        float(cfg_prec.get("embalagem", 0)))
    return r.get("preco") if r else None


def margem_no_preco(cfg_prec: dict, preco: float, custo: float):
    """Margem líquida (% sobre o preço de lista) ao vender por `preco` na Shopee."""
    preco = float(preco or 0)
    if preco <= 0:
        return None
    f = _faixa_shopee(cfg_prec, preco)
    if f is None:
        return None
    pct = (float(f.get("comissao", 0)) + float(f.get("fixo_pct", 0))
           + float(cfg_prec.get("imposto", 0)) + float(cfg_prec.get("cartao", 0))) / 100.0
    fixos = float(f.get("fixo", 0)) + float(cfg_prec.get("embalagem", 0))
    liquido = preco * (1 - pct) - fixos
    lucro = liquido - float(custo or 0)
    return lucro / preco * 100.0


def _desconto_seguro(cfg_prec: dict, preco: float, custo: float, teto: int, piso: float) -> int:
    """Maior desconto inteiro (1..teto) que mantém a margem >= piso. 0 = não dá pra descontar."""
    melhor = 0
    for d in range(1, int(teto) + 1):
        m = margem_no_preco(cfg_prec, preco * (1 - d / 100.0), custo)
        if m is None or m < piso:
            break  # a margem só piora com mais desconto
        melhor = d
    return melhor


# --------------------------------------------------------------------------- #
# Seleção de produtos + propostas
# --------------------------------------------------------------------------- #
def _funil(user_id: int, cfg):
    """Monta os candidatos E um diagnóstico de onde o funil filtra, pra explicar
    com precisão quando nada fica elegível."""
    from . import catalogo
    diag = {"catalogo_skus": 0, "shopee_itens": 0, "anuncios_sem_sku": 0,
            "sku_casado": 0, "passaram_estoque": 0, "canal_shopee_configurado": False,
            "margem_calculavel": 0, "passaram_piso": 0, "com_desconto_seguro": 0,
            "elegiveis": 0}

    cat = {p["sku"]: p for p in catalogo.todos(user_id) if p.get("sku")}
    diag["catalogo_skus"] = len(cat)
    if not cat:
        return [], diag

    shop_itens = []
    for off in (0, 100):
        try:
            r = shopee.listar_itens(user_id, offset=off, limite=100)
        except shopee.ShopeeError:
            break
        lst = (r.get("response") or {}).get("item") or []
        shop_itens.extend(lst)
        if len(lst) < 100:
            break
    diag["shopee_itens"] = len(shop_itens)

    cfg_prec = precificacao.obter_config(user_id)
    # o canal Shopee está configurado na Precificação? (faixas de comissão)
    canais = cfg_prec.get("canais") or []
    diag["canal_shopee_configurado"] = any(
        (c.get("canal") or "").lower() == "shopee" and (c.get("faixas")) for c in canais)

    teto, piso = cfg.desconto_max or 15, float(cfg.piso_margem if cfg.piso_margem is not None else 10.0)
    est_min = cfg.estoque_minimo or 0
    estrategia = cfg.estrategia or "estoque_parado"
    dias_an = int(getattr(cfg, "dias_analise", None) or 30)
    # estoque parado = produtos SEM vendas. Cruza com o histórico real (por item_id, não SKU).
    vendas = shopee.vendas_por_item(user_id, dias_an) if estrategia != "margem_alta" else {}
    # produtos já em campanha ativa/agendada não podem entrar em outra — fora das sugestões.
    em_campanha = shopee.itens_em_campanha(user_id)
    diag["sem_vendas"] = 0
    diag["em_campanha"] = 0
    diag["dias_analise"] = dias_an
    out = []
    for it in shop_itens:
        iid = it.get("item_id")
        if iid and int(iid) in em_campanha:
            diag["em_campanha"] += 1
            continue
        sku = it.get("item_sku") or it.get("sku")
        if not sku:
            diag["anuncios_sem_sku"] += 1
            continue
        base = cat.get(sku)
        if not base:
            continue
        diag["sku_casado"] += 1
        estoque = int(float(base.get("saldo") or 0))
        if estoque < max(1, est_min):
            continue
        diag["passaram_estoque"] += 1
        base_venda = float(base.get("preco") or 0)   # Preço Bling = líquido-alvo (base da precificação)
        custo = float(base.get("custo") or 0)
        preco = _preco_lista_shopee(cfg_prec, base_venda) or 0.0   # preço de LISTA configurado na precificação
        if preco <= 0:
            continue
        margem_cheia = margem_no_preco(cfg_prec, preco, custo)
        if margem_cheia is None:
            continue  # sem faixa do canal Shopee: margem não é calculável
        diag["margem_calculavel"] += 1
        if margem_cheia < piso:
            continue  # já está no/abaixo do piso: não há espaço para desconto
        diag["passaram_piso"] += 1
        d = _desconto_seguro(cfg_prec, preco, custo, teto, piso)
        if d <= 0:
            continue
        diag["com_desconto_seguro"] += 1
        vendidos = int(vendas.get(int(iid), 0)) if iid else 0
        if vendidos == 0:
            diag["sem_vendas"] += 1
        preco_promo = round(preco * (1 - d / 100.0), 2)
        out.append({
            "item_id": str(it.get("item_id")), "nome": it.get("item_name") or sku, "sku": sku,
            "estoque": estoque, "preco_atual": round(preco, 2), "preco_promo": preco_promo,
            "preco_base": round(base_venda, 2),
            "desconto_pct": d, "margem_promo": round(margem_no_preco(cfg_prec, preco_promo, custo), 1),
            "margem_cheia": round(margem_cheia, 1), "vendidos": vendidos,
        })

    if estrategia == "margem_alta":
        out.sort(key=lambda x: x["margem_promo"], reverse=True)
    else:
        # ESTOQUE PARADO: menos vendas primeiro, depois mais estoque (capital parado na prateleira).
        # Assim os campeões de venda ("coringas") ficam por último e não entram no desconto.
        out.sort(key=lambda x: (x.get("vendidos", 0), -x["estoque"]))
    out = out[: cfg.max_produtos or 20]
    diag["elegiveis"] = len(out)
    return out, diag


def _candidatos(user_id: int, cfg) -> list:
    out, _ = _funil(user_id, cfg)
    return out


def _motivo_funil(diag: dict, cfg) -> str:
    """Traduz o diagnóstico do funil em uma explicação acionável (a causa mais provável)."""
    piso = float(cfg.piso_margem if cfg.piso_margem is not None else 10.0)
    est_min = max(1, cfg.estoque_minimo or 0)
    if diag["catalogo_skus"] == 0:
        return ("Seu catálogo não está sincronizado (0 produtos no cache). "
                "Sincronize o catálogo do Bling primeiro — as promoções leem custo, preço e estoque de lá.")
    if diag["shopee_itens"] == 0:
        return ("Não consegui listar seus anúncios da Shopee (0 itens). "
                "Verifique se a loja Shopee está conectada e tente reconectar.")
    if diag["sku_casado"] == 0:
        extra = (f" {diag['anuncios_sem_sku']} dos seus anúncios da Shopee estão sem SKU preenchido."
                 if diag["anuncios_sem_sku"] else "")
        return ("Nenhum anúncio da Shopee bateu com um SKU do seu catálogo." + extra +
                " Garanta que o SKU cadastrado no anúncio da Shopee é igual ao SKU do produto no catálogo.")
    if not diag["canal_shopee_configurado"]:
        return ("O canal Shopee não está configurado na Precificação (sem faixas de comissão), "
                "então a margem de cada produto não pôde ser calculada. "
                "Vá em Precificação → canais e configure a Shopee (comissão %, taxas). "
                "Sem isso, o motor não tem como garantir o piso de margem.")
    if diag["passaram_estoque"] == 0:
        return (f"Todos os produtos que casaram estão abaixo do estoque mínimo ({est_min}). "
                "Baixe o 'estoque mínimo' nas regras ou reponha estoque.")
    if diag["margem_calculavel"] == 0:
        return ("Não consegui calcular a margem de nenhum produto — confira as faixas de preço do "
                "canal Shopee na Precificação (o preço dos seus produtos precisa cair em alguma faixa).")
    if diag["passaram_piso"] == 0:
        return (f"Todos os produtos já estão no ou abaixo do piso de margem ({piso:.0f}%) no preço cheio — "
                "não há espaço para desconto. Reduza o piso de margem ou revise custos/preços.")
    if diag["com_desconto_seguro"] == 0:
        return (f"Há margem acima do piso, mas não cabe nem 1% de desconto mantendo o piso de {piso:.0f}%. "
                "Aumente o 'desconto máximo' ou reduza um pouco o piso de margem.")
    if diag.get("elegiveis", 0) == 0 and diag.get("em_campanha"):
        return (f"{diag['em_campanha']} anúncio(s) já estão em campanha ativa ou agendada e foram "
                "ignorados — a Shopee não deixa o mesmo produto em duas promoções. "
                "Espere essas campanhas terminarem (ou encerre-as) para liberar os produtos.")
    return ("Nenhum produto elegível dentro das regras. Revise estoque mínimo, piso de margem e "
            "desconto máximo.")


def diagnosticar_desconto(user_id: int) -> dict:
    """Testa criar UM desconto com 1 produto e devolve as respostas CRUAS da Shopee
    (get_model_list, add_discount, add_discount_item) pra revelar o motivo exato do
    '0 produtos'. Apaga o desconto de teste no fim."""
    out: dict = {"ok": False, "etapas": []}
    cfg_v = _ConfigView(obter_config(user_id))
    cands, diag = _funil(user_id, cfg_v)
    out["diagnostico_funil"] = diag
    if not cands:
        out["motivo"] = _motivo_funil(diag, cfg_v)
        return out
    cand = cands[0]
    out["produto"] = {k: cand.get(k) for k in ("item_id", "nome", "sku", "preco_atual", "preco_promo", "desconto_pct")}
    iid = int(cand["item_id"])

    try:
        ml = shopee._chamar(user_id, "/api/v2/product/get_model_list", extra={"item_id": iid})
        out["etapas"].append({"passo": "get_model_list", "resposta": ml})
    except Exception as e:
        out["etapas"].append({"passo": "get_model_list", "erro": str(e)})

    itens = shopee.itens_desconto_por_pct(user_id, [
        {"item_id": cand["item_id"], "desconto_pct": cand["desconto_pct"], "preco": cand["preco_atual"]}])
    out["payload_enviado"] = itens

    inicio = int(time.time()) + 3900
    fim = inicio + 86400
    nome = f"TESTE diag {datetime.now().strftime('%H:%M:%S')}"
    did = None
    try:
        ad = shopee._chamar(user_id, "/api/v2/discount/add_discount", metodo="POST",
                            extra={"discount_name": nome, "start_time": inicio, "end_time": fim})
        out["etapas"].append({"passo": "add_discount", "resposta": ad})
        did = (ad.get("response") or {}).get("discount_id")
    except Exception as e:
        out["etapas"].append({"passo": "add_discount", "erro": str(e)})

    if did and itens:
        try:
            adi = shopee._chamar(user_id, "/api/v2/discount/add_discount_item", metodo="POST",
                                 extra={"discount_id": did, "item_list": itens})
            out["etapas"].append({"passo": "add_discount_item", "resposta": adi})
            resp = adi.get("response") or {}
            out["count"] = resp.get("count")
            out["error_list"] = resp.get("error_list") or resp.get("fail_list")
            out["ok"] = bool(resp.get("count"))
        except Exception as e:
            out["etapas"].append({"passo": "add_discount_item", "erro": str(e)})
        try:
            shopee._chamar(user_id, "/api/v2/discount/delete_discount", metodo="POST",
                           extra={"discount_id": did})
            out["teste_apagado"] = True
        except Exception:
            out["teste_apagado"] = False
            out["aviso"] = f"Não consegui apagar o desconto de teste (id {did}); apague manualmente na Shopee se aparecer."
    return out


def diagnosticar_flash(user_id: int) -> dict:
    """Testa criar UMA oferta relâmpago (Flash Sale da loja) com 1 produto e devolve as
    respostas CRUAS da Shopee (slots, create, add_items) pra revelar o motivo exato de o
    relâmpago não ser criado. A causa quase sempre é a loja não ter horários (slots) liberados
    — a Shopee concede slots de Flash Sale da Loja por elegibilidade. Apaga a oferta de teste
    (criada num horário FUTURO, invisível ao comprador) no fim."""
    out: dict = {"ok": False, "etapas": []}
    cfg_v = _ConfigView(obter_config(user_id))
    cands, diag = _funil(user_id, cfg_v)
    out["diagnostico_funil"] = diag
    if not cands:
        out["motivo"] = _motivo_funil(diag, cfg_v)
        return out
    cand = cands[0]
    out["produto"] = {k: cand.get(k) for k in ("item_id", "nome", "sku", "preco_atual", "preco_promo", "desconto_pct", "estoque")}
    iid = int(cand["item_id"])

    # Passo 1 — slots disponíveis (é aqui que a elegibilidade aparece)
    slots_raw = None
    try:
        slots_raw = shopee.flash_slots(user_id, dias=3)
        out["etapas"].append({"passo": "get_time_slot", "resposta": slots_raw})
    except Exception as e:
        out["etapas"].append({"passo": "get_time_slot", "erro": str(e)})
    resp_s = (slots_raw or {}).get("response")
    lista = resp_s.get("timeslot_list") if isinstance(resp_s, dict) else (resp_s if isinstance(resp_s, list) else [])
    lista = lista or []
    out["slots_disponiveis"] = len(lista)
    slot = None
    if lista:
        primeiro = lista[0]
        slot = primeiro.get("timeslot_id") if isinstance(primeiro, dict) else primeiro
        out["primeiro_slot"] = primeiro
    if not slot:
        out["motivo"] = ("Sua loja não tem horários (slots) de Flash Sale liberados agora. A Shopee "
                         "concede slots de Flash Sale da Loja por elegibilidade (reputação e histórico "
                         "da loja). Sem slot, o relâmpago não pode ser criado — não é um erro do sistema, "
                         "é uma limitação da Shopee para a sua loja. O agente continua criando os "
                         "Descontos normalmente.")
        return out

    # Passo 2 — criar a flash sale no slot futuro
    fid = None
    try:
        cr = shopee._chamar(user_id, "/api/v2/shop_flash_sale/create_shop_flash_sale", metodo="POST",
                            extra={"timeslot_id": int(slot)})
        out["etapas"].append({"passo": "create_shop_flash_sale", "resposta": cr})
        fid = (cr.get("response") or {}).get("flash_sale_id")
    except Exception as e:
        out["etapas"].append({"passo": "create_shop_flash_sale", "erro": str(e)})
    if not fid:
        out["motivo"] = ("Havia slot disponível, mas a Shopee não criou a oferta relâmpago. Veja a "
                         "resposta crua de create_shop_flash_sale acima para o motivo exato.")
        return out

    # Passo 3 — adicionar 1 item
    reserva = int(getattr(cfg_v, "reserva_estoque", 0) or 0)
    estoque = int(cand.get("estoque") or 0)
    try:
        itens = shopee._expandir_itens_flash(user_id, [
            {"item_id": iid, "preco": cand["preco_promo"], "stock": max(1, estoque - reserva)}])
        out["payload_enviado"] = itens
        adi = shopee._chamar(user_id, "/api/v2/shop_flash_sale/add_shop_flash_sale_items", metodo="POST",
                             extra={"flash_sale_id": fid, "items": itens})
        out["etapas"].append({"passo": "add_shop_flash_sale_items", "resposta": adi})
        resp = adi.get("response") or {}
        out["failed_items"] = resp.get("failed_items") or resp.get("fail_list") or []
        out["ok"] = not out["failed_items"]
        if out["failed_items"]:
            out["motivo"] = ("A oferta foi criada, mas o produto foi rejeitado ao ser anexado (preço "
                             "promocional abaixo do permitido, estoque insuficiente, ou produto inelegível). "
                             "Veja failed_items acima.")
    except Exception as e:
        out["etapas"].append({"passo": "add_shop_flash_sale_items", "erro": str(e)})

    # limpeza — apaga a oferta de teste (está num horário futuro, invisível ao comprador)
    try:
        shopee._chamar(user_id, "/api/v2/shop_flash_sale/delete_shop_flash_sale", metodo="POST",
                       extra={"flash_sale_id": int(fid)})
        out["teste_apagado"] = True
    except Exception:
        out["teste_apagado"] = False
        out["aviso"] = f"Não consegui apagar a oferta de teste (id {fid}); apague manualmente na Shopee se aparecer (está em Ofertas Relâmpago futuras)."
    if out["ok"]:
        out["motivo"] = ("Tudo certo! Sua loja TEM slots e o produto foi aceito na Flash Sale. O agente "
                         "consegue criar relâmpagos — a oferta de teste foi apagada.")
    return out


def propor(user_id: int) -> dict:
    """Monta as propostas de promoção (não cria nada). Para revisão no modo sugerir."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        snap = _serializar(cfg)
    finally:
        db.close()
    cfgv = _ConfigView(snap)
    propostas, diag = _funil(user_id, cfgv)
    if not propostas:
        return {"acao": "vazio", "propostas": [], "diagnostico": diag,
                "msg": _motivo_funil(diag, cfgv)}
    # enriquece com foto do anúncio (uma chamada em lote, cacheada)
    try:
        ids = [int(p["item_id"]) for p in propostas if str(p.get("item_id") or "").isdigit()]
        meta = shopee.nomes_itens(user_id, ids) if ids else {}
        for p in propostas:
            m = meta.get(int(p["item_id"])) if str(p.get("item_id") or "").isdigit() else None
            if m:
                p["imagem"] = m.get("imagem")
    except Exception:  # noqa: BLE001
        pass
    return {"acao": "ok", "propostas": propostas, "config": snap, "diagnostico": diag}


class _ConfigView:
    """Adaptador leve para usar o dict de config onde o código espera atributos."""
    def __init__(self, d: dict):
        self.__dict__.update(d)


# --------------------------------------------------------------------------- #
# Aplicar (criar de fato) — desconto e/ou flash
# --------------------------------------------------------------------------- #
def _registrar_log(user_id, tipo, ref_id, nome, qtd, desconto, motivo):
    db = SessionLocal()
    try:
        db.add(ShopeePromoLog(user_id=user_id, tipo=tipo, ref_id=str(ref_id or ""),
                              nome=nome, qtd_itens=qtd, desconto_pct=desconto, motivo=motivo))
        db.commit()
    finally:
        db.close()


def aplicar(user_id: int, propostas: list, tipo: str | None = None, motivo: str = "manual") -> dict:
    """Cria as promoções a partir das propostas. tipo: desconto | flash | ambos (default = config)."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        snap = _serializar(cfg)
    finally:
        db.close()
    tipo = tipo or snap["tipo"]
    if not propostas:
        return {"acao": "vazio", "criadas": []}

    d_medio = round(sum(p.get("desconto_pct", 0) for p in propostas) / len(propostas))
    criadas = []
    erros = []

    if tipo in ("desconto", "ambos"):
        try:
            # a Shopee exige start_time >= 1h no futuro; senão cria a campanha mas REJEITA anexar itens
            inicio = int(time.time()) + 3900  # 1h05min de folga
            fim = inicio + max(1, snap["duracao_dias"]) * 86400
            itens = shopee.itens_desconto_por_pct(user_id, [
                {"item_id": p["item_id"], "desconto_pct": p["desconto_pct"], "preco": p["preco_atual"]}
                for p in propostas])
            nome = f"Auto · {datetime.now().strftime('%d/%m %H:%M')}"
            r = shopee.criar_desconto(user_id, nome, inicio, fim, itens)
            did = (r.get("response") or {}).get("discount_id")
            entraram = r.get("itens_adicionados", len(itens))
            enviados = r.get("enviados", len(itens))
            _registrar_log(user_id, "desconto", did, nome, entraram, d_medio, motivo)
            criadas.append({"tipo": "desconto", "id": did, "itens": entraram, "nome": nome,
                            "enviados": enviados})
            itens_erros = r.get("item_erros") or []
            if did and not entraram:
                motivo_top = itens_erros[0] if itens_erros else "a Shopee não detalhou o motivo"
                erros.append(f"desconto: a campanha foi criada mas nenhum dos {enviados} produto(s) entrou. "
                             f"Motivo: {motivo_top}")
            for ie in itens_erros[:4]:
                erros.append(f"desconto/item — {ie}")
        except shopee.ShopeeError as e:
            erros.append(f"desconto: {e}")

    if tipo in ("flash", "ambos"):
        try:
            slots = shopee.flash_slots(user_id, dias=2)
            resp_s = slots.get("response")
            if isinstance(resp_s, dict):
                lista = resp_s.get("timeslot_list") or []
            elif isinstance(resp_s, list):
                lista = resp_s
            else:
                lista = []
            slot = None
            if lista:
                primeiro = lista[0]
                slot = primeiro.get("timeslot_id") if isinstance(primeiro, dict) else primeiro
            if slot:
                reserva = snap["reserva_estoque"]
                itens_flash = [{"item_id": p["item_id"], "preco": p["preco_promo"],
                                "stock": max(1, p["estoque"] - reserva)} for p in propostas]
                r = shopee.criar_flash(user_id, slot, itens_flash)
                fid = (r.get("response") or {}).get("flash_sale_id")
                if fid:
                    _registrar_log(user_id, "flash", fid, "Flash auto", len(itens_flash), d_medio, motivo)
                    criadas.append({"tipo": "flash", "id": fid, "itens": len(itens_flash)})
                else:
                    erros.append("flash: a Shopee não criou a oferta relâmpago (a loja pode não estar "
                                 "elegível a Flash Sale própria, ou o slot expirou).")
            else:
                erros.append("flash: nenhum horário (slot) de Flash Sale disponível agora — "
                             "a Shopee libera slots por período e por elegibilidade da loja.")
        except shopee.ShopeeError as e:
            erros.append(f"flash: {e}")

    if criadas:
        try:
            from . import notificacoes as notif
            notif.criar(user_id, "precificacao",
                        f"{len(criadas)} promoção(ões) criada(s) na Shopee",
                        (f"Origem: {motivo}. " if motivo and motivo != "manual" else "")
                        + (f"{len(erros)} aviso(s)." if erros else "Tudo certo."),
                        ok=True, modulo="promocoes")
        except Exception:  # noqa: BLE001
            pass
    return {"acao": "ok" if criadas else "erro", "criadas": criadas, "erros": erros}


# --------------------------------------------------------------------------- #
# Detecção de queda de vendas
# --------------------------------------------------------------------------- #
BUCKETS = ["madrugada (0-6h)", "manhã (6-12h)", "tarde (12-18h)", "noite (18-24h)"]
_MIN_AMOSTRAS = 4      # amostras mínimas antes de avaliar
_MIN_VOLUME = 3        # base mínima de pedidos p/ não disparar em ruído de número pequeno


def _bucket_agora() -> int:
    # horário do Brasil (UTC-3) para a faixa do dia fazer sentido
    return ((datetime.utcnow() - timedelta(hours=3)).hour) // 6


def snapshot_vendas(user_id: int) -> dict:
    """Foto das vendas: pedidos nas últimas 24h e na janela de 6h, com a faixa do dia."""
    try:
        p24 = shopee.contar_pedidos_horas(user_id, 24)
        p6 = shopee.contar_pedidos_horas(user_id, 6)
    except shopee.ShopeeError as e:
        return {"erro": str(e)}
    bucket = _bucket_agora()
    db = SessionLocal()
    try:
        db.add(ShopeeVendaSnapshot(user_id=user_id, pedidos_24h=p24, pedidos_6h=p6, bucket=bucket))
        db.commit()
    finally:
        db.close()
    return {"pedidos_24h": p24, "pedidos_6h": p6, "bucket": bucket}


def detectar_queda(user_id: int, limiar: int | None = None) -> dict:
    """Compara as vendas recentes com a linha de base.
    base_comparacao='dia': últimas 24h x média dos dias anteriores. Usa snapshots se houver;
      senão cai pra contagem de PEDIDOS REAIS (funciona sem esperar dias acumulando).
    base_comparacao='horario': janela de 6h x média da MESMA faixa nos dias anteriores (só snapshot)."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        lim = limiar if limiar is not None else (cfg.queda_limiar or 30)
        base_cmp = cfg.base_comparacao or "dia"
        desde = datetime.utcnow() - timedelta(days=14)
        amostras = (db.query(ShopeeVendaSnapshot)
                    .filter(ShopeeVendaSnapshot.user_id == user_id,
                            ShopeeVendaSnapshot.criado_em >= desde)
                    .order_by(ShopeeVendaSnapshot.criado_em.desc()).all())
    finally:
        db.close()

    if base_cmp == "horario":
        if len(amostras) < _MIN_AMOSTRAS:
            return {"queda": False, "motivo": "coletando", "amostras": len(amostras), "base_modo": "horario",
                    "msg": "Ainda coletando histórico por horário (precisa de mais amostras). "
                           "Dica: troque para 'Por dia' — esse já consegue avaliar com os pedidos reais."}
        atual_snap = amostras[0]
        b = atual_snap.bucket or 0
        mesmos = [a.pedidos_6h for a in amostras[1:] if (a.bucket or 0) == b]
        if len(mesmos) < 2:
            return {"queda": False, "motivo": "coletando_horario", "base_modo": "horario",
                    "rotulo": BUCKETS[b], "amostras": len(amostras),
                    "msg": f"Coletando histórico da faixa {BUCKETS[b]} (precisa de mais dias nesse horário). "
                           "Dica: 'Por dia' já avalia agora pelos pedidos reais."}
        atual = atual_snap.pedidos_6h
        base = sum(mesmos) / len(mesmos)
        rotulo = BUCKETS[b]
        fonte = "snapshot"
    else:
        # modo dia: snapshot se já houver histórico suficiente; senão pedidos reais
        if len(amostras) >= _MIN_AMOSTRAS:
            atual = amostras[0].pedidos_24h
            base_vals = [a.pedidos_24h for a in amostras[1:9]]
            base = sum(base_vals) / len(base_vals) if base_vals else 0
            fonte = "snapshot"
        else:
            try:
                dias_reais = shopee.pedidos_por_dia(user_id, 8)
            except shopee.ShopeeError:
                dias_reais = []
            anteriores = dias_reais[1:] if len(dias_reais) > 1 else []
            if not anteriores or sum(anteriores) <= 0:
                return {"queda": False, "motivo": "coletando", "amostras": len(amostras), "base_modo": "dia",
                        "msg": "Ainda sem histórico de vendas suficiente para comparar (poucos pedidos nos últimos dias)."}
            atual = dias_reais[0]
            base = sum(anteriores) / len(anteriores)
            fonte = "pedidos_reais"
        rotulo = "dia"

    if base < _MIN_VOLUME:
        return {"queda": False, "motivo": "volume_baixo", "atual": atual, "base": round(base, 1),
                "base_modo": base_cmp, "rotulo": rotulo, "amostras": len(amostras), "fonte": fonte,
                "msg": "Volume ainda baixo pra disparar com segurança (evita falso alarme)."}

    pct = (base - atual) / base * 100.0
    return {"queda": pct >= lim, "atual": atual, "base": round(base, 1),
            "queda_pct": round(pct, 1), "limiar": lim, "amostras": len(amostras),
            "base_modo": base_cmp, "rotulo": rotulo, "fonte": fonte}


# --------------------------------------------------------------------------- #
# Ciclo automático (chamado pelo agendador)
# --------------------------------------------------------------------------- #
def aplicar_agora(user_id: int) -> dict:
    """Roda o agente AGORA, aplicando de fato (sem aprovação), respeitando todas as
    travas (piso de margem, estoque). É o que o botão 'Aplicar agora' chama no modo auto."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        if not cfg.ativo:
            return {"acao": "inativo", "msg": "Ligue o motor de promoções (chave 'Ativo') primeiro."}
        snap = _serializar(cfg)
    finally:
        db.close()
    cfgv = _ConfigView(snap)
    propostas, diag = _funil(user_id, cfgv)
    if not propostas:
        return {"acao": "vazio", "diagnostico": diag, "msg": _motivo_funil(diag, cfgv)}
    res = aplicar(user_id, propostas, snap["tipo"], "manual")
    db = SessionLocal()
    try:
        c = _config(db, user_id)
        c.ultimo_ciclo = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    return {"acao": "aplicado", "propostas": propostas, **res}


def auto_ciclo_forcado(user_id: int) -> dict:
    """Igual ao auto_ciclo, mas IGNORA o intervalo (usado quando o usuário acabou de
    ligar o modo automático — dispara já). Mantém todas as travas (piso, estoque)."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        if not cfg.ativo or cfg.modo != "auto":
            return {"acao": "ocioso"}
        snap = _serializar(cfg)
    finally:
        db.close()
    propostas = _candidatos(user_id, _ConfigView(snap))
    if not propostas:
        return {"acao": "sem_candidatos"}
    res = aplicar(user_id, propostas, snap["tipo"], "agendado")
    db = SessionLocal()
    try:
        c = _config(db, user_id)
        c.ultimo_ciclo = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    return {"acao": "aplicado", "motivo": "agendado", **res}


def auto_ciclo(user_id: int) -> dict:
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        if not cfg.ativo or cfg.modo != "auto":
            return {"acao": "ocioso"}
        gatilho = cfg.gatilho or "agendado"
        intervalo = cfg.intervalo_dias or 7
        ultimo = cfg.ultimo_ciclo
        snap = _serializar(cfg)
    finally:
        db.close()

    motivo = None
    if gatilho == "agendado":
        if ultimo and (datetime.utcnow() - ultimo) < timedelta(days=intervalo):
            return {"acao": "aguardando_intervalo"}
        motivo = "agendado"
    else:  # queda
        # cooldown: não re-disparar em sequência (senão cria campanha a cada 30 min na queda)
        if ultimo and (datetime.utcnow() - ultimo) < timedelta(hours=8):
            return {"acao": "aguardando_cooldown"}
        q = detectar_queda(user_id)
        if not q.get("queda"):
            return {"acao": "sem_queda", "detalhe": q}
        motivo = "queda"

    propostas = _candidatos(user_id, _ConfigView(snap))
    if not propostas:
        return {"acao": "sem_candidatos"}
    res = aplicar(user_id, propostas, snap["tipo"], motivo)

    db = SessionLocal()
    try:
        c = _config(db, user_id)
        c.ultimo_ciclo = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    return {"acao": "aplicado", "motivo": motivo, **res}


def historico(user_id: int, limite: int = 20) -> list:
    db = SessionLocal()
    try:
        regs = (db.query(ShopeePromoLog).filter_by(user_id=user_id)
                .order_by(ShopeePromoLog.criado_em.desc()).limit(limite).all())
        return [{"tipo": r.tipo, "ref_id": r.ref_id, "nome": r.nome, "qtd_itens": r.qtd_itens,
                 "desconto_pct": r.desconto_pct, "motivo": r.motivo,
                 "criado_em": r.criado_em.isoformat() if r.criado_em else None} for r in regs]
    finally:
        db.close()


def resumo(user_id: int, dias: int = 30) -> dict:
    """Resumo real do diário (auditoria) para o cabeçalho do painel: totais,
    distribuição por tipo/gatilho e série diária dos últimos 14 dias."""
    from datetime import timedelta
    db = SessionLocal()
    try:
        desde = datetime.utcnow() - timedelta(days=dias)
        regs = (db.query(ShopeePromoLog)
                .filter(ShopeePromoLog.user_id == user_id, ShopeePromoLog.criado_em >= desde)
                .order_by(ShopeePromoLog.criado_em.desc()).all())
    finally:
        db.close()
    por_tipo, por_motivo, por_dia = {}, {}, {}
    itens_total, descs = 0, []
    for r in regs:
        t = r.tipo or "outro"
        m = r.motivo or "manual"
        por_tipo[t] = por_tipo.get(t, 0) + 1
        por_motivo[m] = por_motivo.get(m, 0) + 1
        itens_total += (r.qtd_itens or 0)
        if r.desconto_pct:
            descs.append(r.desconto_pct)
        if r.criado_em:
            d = r.criado_em.strftime("%Y-%m-%d")
            por_dia[d] = por_dia.get(d, 0) + 1
    hoje = datetime.utcnow().date()
    serie = [{"dia": (hoje - timedelta(days=i)).strftime("%Y-%m-%d"),
              "qtd": por_dia.get((hoje - timedelta(days=i)).strftime("%Y-%m-%d"), 0)}
             for i in range(13, -1, -1)]
    return {
        "total": len(regs),
        "itens_total": itens_total,
        "desconto_medio": round(sum(descs) / len(descs)) if descs else None,
        "por_tipo": por_tipo,
        "por_motivo": por_motivo,
        "serie_14d": serie,
        "ultima": regs[0].criado_em.isoformat() if regs and regs[0].criado_em else None,
        "janela_dias": dias,
    }
