"""Agregação de KPIs a partir dos pedidos de venda e produtos do Bling.

Funções puras (recebem listas, devolvem dicionário) — fáceis de testar.
GMV, ticket médio, venda por canal/loja, mais vendidos, tendência e risco de
ruptura. 'mais_vendidos' só sai quando os pedidos trazem itens (o detalhe);
a listagem resumida do Bling pode não incluir itens — nesse caso fica vazio.
"""


def _f(v) -> float:
    if isinstance(v, str):
        s = v.strip()
        if "," in s:  # formato BR: 1.234,56
            s = s.replace(".", "").replace(",", ".")
        v = s
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _itens_pedido(p: dict) -> list:
    its = p.get("itens") or []
    return its if isinstance(its, list) else []


def vendas_por_sku(pedidos: list) -> dict:
    """{sku: {unidades, receita, descricao}} a partir dos itens dos pedidos."""
    out = {}
    for p in pedidos:
        for it in _itens_pedido(p):
            q = _f(it.get("quantidade")) or 1
            v = _f(it.get("valor")) * q
            prod = it.get("produto") or {}
            sku = prod.get("codigo") or it.get("codigo") or it.get("descricao") or "?"
            s = out.setdefault(str(sku), {"descricao": it.get("descricao") or str(sku), "unidades": 0.0, "receita": 0.0})
            s["unidades"] += q
            s["receita"] += v
    return out


def curva_abc(pedidos: list) -> dict:
    """Classifica cada SKU em A/B/C por participação na receita (Pareto 80/15/5).
    Devolve {sku: {classe, receita, unidades, pct, pct_acumulado, posicao}}."""
    vendas = vendas_por_sku(pedidos)
    total = sum(v["receita"] for v in vendas.values()) or 1.0
    ordenado = sorted(vendas.items(), key=lambda kv: kv[1]["receita"], reverse=True)
    out, acum = {}, 0.0
    for i, (sku, v) in enumerate(ordenado, start=1):
        pct = v["receita"] / total * 100
        antes = acum          # acumulado ANTES deste SKU define a faixa
        acum += pct
        classe = "A" if antes < 80 else ("B" if antes < 95 else "C")
        out[sku] = {"classe": classe, "receita": round(v["receita"], 2),
                    "unidades": v["unidades"], "pct": round(pct, 2),
                    "pct_acumulado": round(acum, 2), "posicao": i, "total_skus": len(ordenado)}
    return out


def analise_demanda(pedidos: list, sku: str, saldo: float, dias: int) -> dict:
    """Velocidade de venda e cobertura de estoque de um SKU no período."""
    v = vendas_por_sku(pedidos).get(str(sku), {"unidades": 0.0, "receita": 0.0})
    unidades = v["unidades"]
    por_dia = unidades / dias if dias else 0.0
    cobertura = (saldo / por_dia) if por_dia > 0 else None  # dias até zerar
    return {"unidades": unidades, "receita": round(v["receita"], 2),
            "por_dia": round(por_dia, 2), "cobertura_dias": round(cobertura, 1) if cobertura is not None else None,
            "saldo": saldo}


def calcular(pedidos: list, produtos: list | None = None) -> dict:
    produtos = produtos or []
    gmv = 0.0
    n = 0
    por_canal: dict = {}      # loja_id -> agregados
    por_dia: dict = {}        # 'YYYY-MM-DD' -> valor
    vendas_sku: dict = {}     # sku -> agregados

    for p in pedidos:
        total = _f(p.get("total")) or _f(p.get("totalProdutos"))
        gmv += total
        n += 1
        loja = str((p.get("loja") or {}).get("id") or "sem_loja")
        c = por_canal.setdefault(loja, {"pedidos": 0, "valor": 0.0, "unidades": 0.0})
        c["pedidos"] += 1
        c["valor"] += total
        data = str(p.get("data") or "")[:10]
        if data:
            por_dia[data] = por_dia.get(data, 0.0) + total
        for it in (p.get("itens") or []):
            q = _f(it.get("quantidade"))
            v = _f(it.get("valor")) * q
            prod = it.get("produto") or {}
            sku = prod.get("codigo") or it.get("codigo") or it.get("descricao") or "?"
            s = vendas_sku.setdefault(str(sku), {
                "descricao": it.get("descricao") or str(sku), "unidades": 0.0, "valor": 0.0})
            s["unidades"] += q
            s["valor"] += v
            c["unidades"] += q

    ticket = gmv / n if n else 0.0
    mais_vendidos = sorted(
        ({"sku": k, **v, "valor": round(v["valor"], 2)} for k, v in vendas_sku.items()),
        key=lambda x: x["unidades"], reverse=True)[:10]

    risco = []
    for pr in produtos:
        est = pr.get("estoque") or {}
        saldo = _f(est.get("saldoVirtualTotal"))
        minimo = _f(est.get("minimo"))
        if minimo > 0 and saldo <= minimo:
            risco.append({"sku": pr.get("codigo"), "nome": pr.get("nome"),
                          "saldo": saldo, "minimo": minimo})

    # Aging: produtos COM estoque que não venderam no período (capital parado)
    vendidos = {str(k) for k in vendas_sku}
    parados = []
    for pr in produtos:
        sku = pr.get("codigo")
        est = pr.get("estoque") or {}
        saldo = _f(est.get("saldoVirtualTotal"))
        preco = _f(pr.get("preco"))
        if sku and str(sku) not in vendidos and saldo > 0:
            parados.append({"sku": sku, "nome": pr.get("nome"), "saldo": saldo,
                            "preco": preco, "capital": round(saldo * preco, 2)})
    parados.sort(key=lambda x: x["capital"], reverse=True)
    capital_parado = round(sum(p["capital"] for p in parados), 2)

    return {
        "gmv": round(gmv, 2),
        "pedidos": n,
        "ticket_medio": round(ticket, 2),
        "por_canal": [{"loja": k, "pedidos": v["pedidos"], "unidades": v["unidades"],
                       "valor": round(v["valor"], 2)}
                      for k, v in sorted(por_canal.items(), key=lambda x: -x[1]["valor"])],
        "mais_vendidos": mais_vendidos,
        "tendencia": [{"data": k, "valor": round(por_dia[k], 2)} for k in sorted(por_dia)],
        "risco_ruptura": risco[:20],
        "parados": parados[:20],
        "qtd_parados": len(parados),
        "capital_parado": capital_parado,
    }
