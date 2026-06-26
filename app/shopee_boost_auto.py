"""Boost condicional pelo Radar.

Cruza o Radar (preço do concorrente) com o motor de boost: quando um concorrente
fura o seu preço (além de um gatilho), o produto correspondente é fixado em boost
(fixo + condicional) e o ciclo o impulsiona com prioridade. Quando a ameaça passa
(o concorrente sobe o preço ou some), o item é liberado automaticamente.

A regra da Shopee continua valendo: no máx 5 impulsionados ao mesmo tempo. Por isso
existe um teto separado (cond_max) pra que o boost condicional não engula todos os
slots — sobra espaço pro rodízio normal.
"""
from . import shopee, shopee_boost
from .db import SessionLocal
from .models import ShopeeBoostConfig, ShopeeBoostItem


def config(user_id: int) -> dict:
    db = SessionLocal()
    try:
        cfg = db.query(ShopeeBoostConfig).filter_by(user_id=user_id).first()
        if not cfg:
            cfg = ShopeeBoostConfig(user_id=user_id)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)
        return {
            "cond_ativo": bool(getattr(cfg, "cond_ativo", False)),
            "cond_gatilho_pct": float(getattr(cfg, "cond_gatilho_pct", 0) or 0),
            "cond_max": int(getattr(cfg, "cond_max", 3) or 3),
        }
    finally:
        db.close()


def salvar_config(user_id: int, dados: dict) -> dict:
    db = SessionLocal()
    try:
        cfg = db.query(ShopeeBoostConfig).filter_by(user_id=user_id).first()
        if not cfg:
            cfg = ShopeeBoostConfig(user_id=user_id)
            db.add(cfg)
        if "cond_ativo" in dados:
            cfg.cond_ativo = bool(dados["cond_ativo"])
        if "cond_gatilho_pct" in dados:
            cfg.cond_gatilho_pct = max(0.0, min(90.0, float(dados["cond_gatilho_pct"])))
        if "cond_max" in dados:
            cfg.cond_max = max(1, min(5, int(dados["cond_max"])))
        db.commit()
    finally:
        db.close()
    return config(user_id)


def _mapa_sku_item(user_id: int) -> dict:
    """{item_sku: {item_id, nome}} a partir dos anúncios da Shopee (1-2 páginas)."""
    mapa = {}
    for off in (0, 100):
        r = shopee.listar_itens(user_id, offset=off, limite=100)
        lst = (r.get("response") or {}).get("item") or []
        for it in lst:
            s = it.get("item_sku") or it.get("sku")
            if s:
                mapa[s] = {"item_id": str(it.get("item_id")), "nome": it.get("item_name") or s}
        if len(lst) < 100:
            break
    return mapa


def avaliar(user_id: int) -> dict:
    """Diagnóstico: quais produtos estão ameaçados (concorrente furou o preço além do gatilho).
    Não altera nada — só calcula. Usado pelo painel e pelo aplicar."""
    from . import catalogo, radar
    cfg = config(user_id)
    gatilho = cfg["cond_gatilho_pct"]

    skus = radar.skus_monitorados(user_id)
    diag = {"skus_monitorados": len(skus), "com_preco_meu": 0, "com_preco_concorrente": 0,
            "ameacados": 0, "com_anuncio": 0}
    if not skus:
        return {"ameacados": [], "gatilho_pct": gatilho, "diagnostico": diag,
                "motivo": "Nenhum SKU monitorado no Radar. Adicione concorrentes na aba Radar."}

    cat = {p["sku"]: p for p in catalogo.todos(user_id) if p.get("sku")}
    try:
        sku_item = _mapa_sku_item(user_id)
    except shopee.ShopeeError as e:
        return {"ameacados": [], "gatilho_pct": gatilho, "erro": str(e), "diagnostico": diag}

    ameacados = []
    for sku in skus:
        meu = cat.get(sku)
        meu_preco = float(meu.get("preco") or 0) if meu else 0
        if meu_preco <= 0:
            continue
        diag["com_preco_meu"] += 1
        conc = radar.menor_preco_concorrente(user_id, sku)
        if conc is None:
            continue
        diag["com_preco_concorrente"] += 1
        limite = meu_preco * (1 - gatilho / 100.0)
        if conc < limite:
            diag["ameacados"] += 1
            it = sku_item.get(sku)
            if it:
                diag["com_anuncio"] += 1
            diff_pct = round((meu_preco - conc) / meu_preco * 100, 1) if meu_preco else 0
            ameacados.append({
                "sku": sku, "nome": (it or {}).get("nome") or (meu.get("nome") if meu else sku) or sku,
                "item_id": (it or {}).get("item_id"), "tem_anuncio": it is not None,
                "meu_preco": round(meu_preco, 2), "concorrente": round(conc, 2),
                "diferenca_pct": diff_pct,
            })
    ameacados.sort(key=lambda x: -x["diferenca_pct"])
    return {"ameacados": ameacados, "gatilho_pct": gatilho, "diagnostico": diag}


def _motivo(a: dict) -> str:
    return f"concorrente R$ {a['concorrente']:.2f} ({a['diferenca_pct']}% abaixo do seu)"


def aplicar(user_id: int, forcar: bool = False) -> dict:
    """Avalia e aplica: fixa em boost os ameaçados (até cond_max) e libera os que não estão
    mais ameaçados. Depois roda um ciclo pra impulsionar de fato.
    forcar=True ignora o cond_ativo (usado pelo botão 'aplicar agora')."""
    cfg = config(user_id)
    if not cfg["cond_ativo"] and not forcar:
        return {"acao": "desligado", "msg": "Boost condicional está desligado."}

    ev = avaliar(user_id)
    if ev.get("erro"):
        return {"acao": "erro", "erro": ev["erro"], "diagnostico": ev.get("diagnostico")}

    ameacados = ev["ameacados"]
    com_anuncio = [a for a in ameacados if a["item_id"]][: cfg["cond_max"]]
    alvo_ids = {a["item_id"] for a in com_anuncio}

    db = SessionLocal()
    impulsionados, liberados = [], []
    try:
        existentes = db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()
        by_id = {i.item_id: i for i in existentes}
        for a in com_anuncio:
            it = by_id.get(a["item_id"])
            mot = _motivo(a)
            if it:
                it.fixo = True
                it.condicional = True
                it.motivo = mot
                it.prioridade = max(it.prioridade or 0, 100)
            else:
                db.add(ShopeeBoostItem(user_id=user_id, item_id=a["item_id"], nome=a["nome"],
                                       fixo=True, condicional=True, motivo=mot, prioridade=100, impulsos=0))
            impulsionados.append(a["item_id"])
        # libera os que entraram por condição mas não estão mais ameaçados
        for i in existentes:
            if getattr(i, "condicional", False) and i.item_id not in alvo_ids:
                i.fixo = False
                i.condicional = False
                i.motivo = None
                liberados.append(i.item_id)
        db.commit()
    finally:
        db.close()

    ciclo_res = shopee_boost.ciclo(user_id, notificar=False)
    sem_anuncio = [a for a in ameacados if not a["item_id"]]
    if impulsionados or liberados:
        try:
            from . import notificacoes as notif
            notif.criar(user_id, "concorrencia",
                        f"Boost condicional: {len(impulsionados)} produto(s) sob ameaça",
                        (f"{len(impulsionados)} priorizado(s) em boost"
                         + (f", {len(liberados)} liberado(s)" if liberados else "")
                         + ". Concorrentes pressionando — confira no Boost."),
                        ok=True, modulo="boost")
        except Exception:  # noqa: BLE001
            pass
    return {
        "acao": "aplicado",
        "ameacados": len(ameacados),
        "impulsionados": impulsionados,
        "liberados": liberados,
        "sem_anuncio": len(sem_anuncio),
        "diagnostico": ev.get("diagnostico"),
        "ciclo": ciclo_res,
        "msg": (f"{len(impulsionados)} produto(s) sob ameaça em boost prioritário"
                + (f", {len(liberados)} liberado(s)" if liberados else "")
                + (f". {len(sem_anuncio)} ameaçado(s) sem anúncio Shopee casado." if sem_anuncio else ".")),
    }
