"""Motor de precificação — matemática validada do protótipo, com indicador corrigido.

MÉTODOS DE PREÇO (mantidos do seu protótipo, porque estão corretos):
  preco_bling       = base / (1 - (ganho% + imposto% + cartao%)/100)   (markup divisor)
  preco_marketplace = (preco_bling + taxa_fixa) / (1 - comissao%/100)  (fixa antes da %)

INDICADOR CORRIGIDO:
  A "Margem Bruta Real" do protótipo usava (preco - custo)/preco e devolvia
  ganho+imposto+cartao (superestima o lucro). Aqui, margem_liquida desconta
  comissao + taxa fixa + imposto + cartao de verdade.
"""

# Taxas-padrão por canal (comissao% e taxa fixa R$ já "roladas": servico/devolucao/
# gateway entram no total). São defaults editáveis por produto (taxas_por_canal).
PLATAFORMAS = {
    "shopee": {"nome": "Shopee", "comissao": 14.0, "fixo": 3.0},
    "mercadolivre": {"nome": "Mercado Livre", "comissao": 16.0, "fixo": 6.0},
    "tiktok": {"nome": "TikTok Shop", "comissao": 5.0, "fixo": 0.0},
    "shein": {"nome": "Shein", "comissao": 18.0, "fixo": 0.0},
    "amazon": {"nome": "Amazon", "comissao": 15.0, "fixo": 0.0},
    "magalu": {"nome": "Magalu", "comissao": 16.0, "fixo": 0.0},
    "americanas": {"nome": "Americanas", "comissao": 16.0, "fixo": 0.0},
    "nuvemshop": {"nome": "Nuvemshop", "comissao": 2.0, "fixo": 0.0},
}


def custo_base(custo_produto, embalagem=0.0, frete=0.0) -> float:
    return float(custo_produto or 0) + float(embalagem or 0) + float(frete or 0)


def preco_bling(base, ganho_pct, imposto_pct, cartao_pct) -> float:
    denom = 1 - (float(ganho_pct) + float(imposto_pct) + float(cartao_pct)) / 100
    if denom <= 0:
        return base * 3
    return base / denom


def preco_marketplace(preco_base_bling, comissao_pct, fixo) -> float:
    p = preco_base_bling + float(fixo or 0)
    c = float(comissao_pct or 0)
    if 0 < c < 100:
        p = p / (1 - c / 100)
    return p


def margem_liquida(preco_final, base, comissao_pct=0.0, fixo=0.0,
                   imposto_pct=0.0, cartao_pct=0.0) -> float:
    if preco_final <= 0:
        return 0.0
    pct = (float(comissao_pct) + float(imposto_pct) + float(cartao_pct)) / 100
    lucro = preco_final * (1 - pct) - float(fixo or 0) - base
    return (lucro / preco_final) * 100


def simular_concorrente(preco_concorrente, base, comissao_pct=0.0, fixo=0.0,
                        imposto_pct=0.0, cartao_pct=0.0) -> dict:
    """Se você igualar o preço do concorrente NESTE canal, qual o lucro/margem REAL?

    (substitui a conta ingênua (preco-custo)/preco que o front fazia)
    """
    if preco_concorrente <= 0:
        return {"lucro": 0.0, "margem_liquida": 0.0, "saudavel": False}
    pct = (float(comissao_pct) + float(imposto_pct) + float(cartao_pct)) / 100
    lucro = preco_concorrente * (1 - pct) - float(fixo or 0) - base
    margem = (lucro / preco_concorrente) * 100
    return {
        "lucro": round(lucro, 2),
        "margem_liquida": round(margem, 2),
        "saudavel": margem >= 15,  # faixa de atenção; >= 30 é confortável
    }


def preco_para_margem(base, comissao, fixo, imposto, cartao, margem_alvo):
    """Preço de venda no canal que entrega exatamente `margem_alvo`% líquida.

    Fonte única desta fórmula (usada pelo motor de decisão e pelo markup reverso):
      preco = (base + fixo) / (1 - (comissao + imposto + cartao + margem_alvo)/100)
    Retorna None se o alvo for impossível (denominador <= 0).
    """
    pct = (float(comissao) + float(imposto) + float(cartao) + float(margem_alvo)) / 100
    den = 1 - pct
    if den <= 0:
        return None
    return (float(base) + float(fixo)) / den


def precificar_reverso(custo_base, imposto_pct, cartao_pct, margem_alvo,
                       taxas_por_canal: dict | None = None) -> dict:
    """Markup reverso: dado o custo e a margem alvo, calcula o preço de venda por
    canal e decompõe em R$ (Raio-X): quanto o canal morde, impostos/cartão e lucro.
    """
    taxas_por_canal = taxas_por_canal or {}
    canais = {}
    for cid, cfg in PLATAFORMAS.items():
        t = taxas_por_canal.get(cid, {})
        comissao = float(t.get("comissao", cfg["comissao"]))
        fixo = float(t.get("fixo", cfg["fixo"]))
        preco = preco_para_margem(custo_base, comissao, fixo, imposto_pct, cartao_pct, margem_alvo)
        if preco is None:
            canais[cid] = {"nome": cfg["nome"], "viavel": False,
                           "motivo": "Margem alvo + taxas passam de 100%."}
            continue
        taxa_canal = preco * comissao / 100 + fixo
        impostos = preco * (float(imposto_pct) + float(cartao_pct)) / 100
        lucro = preco * float(margem_alvo) / 100
        canais[cid] = {
            "nome": cfg["nome"], "viavel": True,
            "preco": round(preco, 2),
            "taxa_canal_reais": round(taxa_canal, 2),
            "impostos_cartao_reais": round(impostos, 2),
            "lucro_reais": round(lucro, 2),
        }
    return {"custo_base": round(float(custo_base), 2), "margem_alvo": margem_alvo, "canais": canais}


def precificar(produto: dict, custos_globais: dict, taxas_por_canal: dict | None = None) -> dict:
    g = custos_globais or {}
    emb = produto.get("embalagem") or g.get("embalagem", 0)
    frt = produto.get("frete") or g.get("frete", 0)
    base = custo_base(produto.get("custo", 0), emb, frt)

    pb = preco_bling(base, g.get("ganho", 0), g.get("imposto", 0), g.get("cartao", 0))

    taxas_por_canal = taxas_por_canal or {}
    canais = {}
    for cid, cfg in PLATAFORMAS.items():
        t = taxas_por_canal.get(cid, {})
        comissao = t.get("comissao", cfg["comissao"])
        fixo = t.get("fixo", cfg["fixo"])
        preco = preco_marketplace(pb, comissao, fixo)
        canais[cid] = {
            "nome": cfg["nome"],
            "preco": round(preco, 2),
            "margem_liquida": round(
                margem_liquida(preco, base, comissao, fixo,
                               g.get("imposto", 0), g.get("cartao", 0)), 2),
        }

    return {
        "custo_base": round(base, 2),
        "preco_bling": round(pb, 2),
        "margem_liquida_bling": round(
            margem_liquida(pb, base, 0, 0, g.get("imposto", 0), g.get("cartao", 0)), 2),
        "canais": canais,
    }
