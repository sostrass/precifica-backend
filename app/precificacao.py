"""Precificação enterprise por canal, com FAIXAS DE PREÇO.

Em 2026 os marketplaces passaram a cobrar taxas diferentes por faixa de preço
(custo fixo e/ou comissão mudam conforme quanto o produto custa). Este módulo
modela isso de forma editável por tenant — porque NÃO existe API que entregue as
taxas (variam por categoria/reputação e mudam toda hora). Os padrões abaixo são
semeados de pesquisa atual (fontes públicas, fev–mai/2026) e servem de ponto de
partida; o lojista ajusta no painel.

Modelo de uma faixa: {ate, comissao, fixo, fixo_pct}
  ate      = teto da faixa em R$ (None = catch-all, sem teto)
  comissao = % sobre o preço de venda
  fixo     = R$ fixos por unidade
  fixo_pct = % do preço cobrado como tarifa (cobre a regra dos "50% do valor"
             em itens baratíssimos)

Custo da taxa na faixa = preco*(comissao+fixo_pct)/100 + fixo

Markup reverso (preço para bater margem) é CIRCULAR: a faixa depende do preço,
que depende da faixa. Resolvemos iterando as faixas e devolvendo aquela cujo
preço calculado cai dentro do próprio intervalo (consistente).
"""

import copy

from .db import SessionLocal
from .models import PrecificacaoConfig


# --------------------------------------------------------------------------- #
# PADRÕES SEMEADOS DE PESQUISA (fev–mai/2026) — todos editáveis pelo lojista
# --------------------------------------------------------------------------- #
CANAIS_PADRAO = [
    {
        "canal": "mercadolivre", "nome": "Mercado Livre", "ativo": True,
        # Clássico ~11-14% + custo fixo por faixa abaixo de R$79 (frete grátis acima)
        "faixas": [
            {"ate": 12.50, "comissao": 14.0, "fixo": 0.00, "fixo_pct": 50.0},
            {"ate": 29.00, "comissao": 14.0, "fixo": 6.25, "fixo_pct": 0.0},
            {"ate": 50.00, "comissao": 14.0, "fixo": 6.50, "fixo_pct": 0.0},
            {"ate": 79.00, "comissao": 14.0, "fixo": 6.75, "fixo_pct": 0.0},
            {"ate": None,  "comissao": 14.0, "fixo": 0.00, "fixo_pct": 0.0},
        ],
    },
    {
        "canal": "shopee", "nome": "Shopee", "ativo": True,
        # Desde mar/2026: faixa <=R$79,99 paga 20% + R$4; acima cai p/ 14% + fixo maior
        "faixas": [
            {"ate": 8.00,  "comissao": 20.0, "fixo": 0.0,  "fixo_pct": 50.0},
            {"ate": 79.99, "comissao": 20.0, "fixo": 4.0,  "fixo_pct": 0.0},
            {"ate": None,  "comissao": 14.0, "fixo": 16.0, "fixo_pct": 0.0},
        ],
    },
    {
        "canal": "amazon", "nome": "Amazon", "ativo": True,
        # Comissão ~10-15% por categoria + tarifa fixa por faixa
        "faixas": [
            {"ate": 30.00, "comissao": 15.0, "fixo": 4.50, "fixo_pct": 0.0},
            {"ate": 50.00, "comissao": 15.0, "fixo": 6.50, "fixo_pct": 0.0},
            {"ate": 79.00, "comissao": 15.0, "fixo": 6.75, "fixo_pct": 0.0},
            {"ate": None,  "comissao": 15.0, "fixo": 0.00, "fixo_pct": 0.0},
        ],
    },
    # Demais canais: padrão simples (uma faixa), desligados — ative e ajuste se usar
    {"canal": "magalu", "nome": "Magalu", "ativo": False,
     "faixas": [{"ate": None, "comissao": 16.0, "fixo": 0.0, "fixo_pct": 0.0}]},
    {"canal": "americanas", "nome": "Americanas", "ativo": False,
     "faixas": [{"ate": None, "comissao": 16.0, "fixo": 0.0, "fixo_pct": 0.0}]},
    {"canal": "shein", "nome": "Shein", "ativo": False,
     "faixas": [{"ate": None, "comissao": 18.0, "fixo": 0.0, "fixo_pct": 0.0}]},
    {"canal": "tiktok", "nome": "TikTok Shop", "ativo": False,
     "faixas": [{"ate": None, "comissao": 5.0, "fixo": 0.0, "fixo_pct": 0.0}]},
    {"canal": "nuvemshop", "nome": "Loja própria (Nuvemshop)", "ativo": False,
     "faixas": [{"ate": None, "comissao": 2.0, "fixo": 0.0, "fixo_pct": 0.0}]},
]

CUSTOS_PADRAO = {"imposto": 12.0, "cartao": 2.5, "embalagem": 0.0,
                 "frete": 0.0, "margem_padrao": 20.0}


def _r2(v):
    return round(float(v), 2)


# --------------------------------------------------------------------------- #
# MOTOR (puro, testável)
# --------------------------------------------------------------------------- #
def _ordenar(faixas):
    return sorted(faixas, key=lambda f: float("inf") if f.get("ate") is None else float(f["ate"]))


def _preco_na_faixa(base, faixa, imposto, cartao, margem):
    """Preço que bate a margem assumindo as taxas DESTA faixa. None se inviável."""
    pct = (float(faixa.get("comissao", 0)) + float(faixa.get("fixo_pct", 0))
           + imposto + cartao + margem)
    denom = 1.0 - pct / 100.0
    if denom <= 0:
        return None
    return (base + float(faixa.get("fixo", 0))) / denom


def _raio_x(base, preco, faixa, imposto, cartao):
    comissao = float(faixa.get("comissao", 0))
    fixo = float(faixa.get("fixo", 0))
    fixo_pct = float(faixa.get("fixo_pct", 0))
    taxa_canal = preco * comissao / 100.0
    fixo_total = fixo + preco * fixo_pct / 100.0
    imp = preco * imposto / 100.0
    cart = preco * cartao / 100.0
    lucro = preco - taxa_canal - fixo_total - imp - cart - base
    margem_real = (lucro / preco * 100.0) if preco else 0.0
    return {"custo": _r2(base), "taxa_canal": _r2(taxa_canal), "fixo": _r2(fixo_total),
            "impostos": _r2(imp), "cartao": _r2(cart), "lucro": _r2(lucro),
            "margem_real": round(margem_real, 1)}


def precificar_canal(base, faixas, imposto, cartao, margem):
    """Markup reverso escolhendo a faixa pelo preço final. Devolve preço + Raio-X."""
    fx = _ordenar(faixas)
    prev = 0.0
    fallback = None  # (distancia, preco, faixa)
    for f in fx:
        hi = float("inf") if f.get("ate") is None else float(f["ate"])
        preco = _preco_na_faixa(base, f, imposto, cartao, margem)
        if preco is None:
            prev = hi
            continue
        if prev < preco <= hi:
            r = {"preco": _r2(preco), "consistente": True,
                 "faixa": _faixa_dict(f), "raio_x": _raio_x(base, _r2(preco), f, imposto, cartao)}
            return r
        dist = (prev - preco) if preco <= prev else (preco - hi)
        if fallback is None or dist < fallback[0]:
            fallback = (dist, preco, f)
        prev = hi
    if fallback is None:
        return None
    _, preco, f = fallback
    return {"preco": _r2(preco), "consistente": False,
            "faixa": _faixa_dict(f), "raio_x": _raio_x(base, _r2(preco), f, imposto, cartao)}


def _faixa_dict(f):
    return {"ate": f.get("ate"), "comissao": float(f.get("comissao", 0)),
            "fixo": float(f.get("fixo", 0)), "fixo_pct": float(f.get("fixo_pct", 0))}


# --------------------------------------------------------------------------- #
# CONFIG POR TENANT (persistência)
# --------------------------------------------------------------------------- #
def _config_dict(cfg):
    return {"imposto": cfg.imposto, "cartao": cfg.cartao, "embalagem": cfg.embalagem,
            "frete": cfg.frete, "margem_padrao": cfg.margem_padrao,
            "canais": cfg.canais or []}


def _sanear_canais(canais):
    out = []
    for c in canais:
        faixas = []
        for f in c.get("faixas", []):
            ate = f.get("ate")
            ate = None if ate in (None, "", "null") else float(ate)
            faixas.append({"ate": ate,
                           "comissao": float(f.get("comissao", 0) or 0),
                           "fixo": float(f.get("fixo", 0) or 0),
                           "fixo_pct": float(f.get("fixo_pct", 0) or 0)})
        out.append({"canal": str(c.get("canal", "")).strip(),
                    "nome": str(c.get("nome") or c.get("canal", "")).strip(),
                    "ativo": bool(c.get("ativo", True)),
                    "faixas": faixas})
    return out


def obter_config(user_id):
    with SessionLocal() as db:
        cfg = db.query(PrecificacaoConfig).filter_by(user_id=user_id).first()
        if not cfg:
            cfg = PrecificacaoConfig(user_id=user_id, canais=copy.deepcopy(CANAIS_PADRAO),
                                     **CUSTOS_PADRAO)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)
        return _config_dict(cfg)


def salvar_config(user_id, dados):
    with SessionLocal() as db:
        cfg = db.query(PrecificacaoConfig).filter_by(user_id=user_id).first()
        if not cfg:
            cfg = PrecificacaoConfig(user_id=user_id, canais=copy.deepcopy(CANAIS_PADRAO),
                                     **CUSTOS_PADRAO)
            db.add(cfg)
        for campo in ("imposto", "cartao", "embalagem", "frete", "margem_padrao"):
            if dados.get(campo) is not None:
                setattr(cfg, campo, float(dados[campo]))
        if isinstance(dados.get("canais"), list):
            cfg.canais = _sanear_canais(dados["canais"])
        db.commit()
        db.refresh(cfg)
        return _config_dict(cfg)


def restaurar_padrao(user_id):
    return salvar_config(user_id, {**CUSTOS_PADRAO, "canais": copy.deepcopy(CANAIS_PADRAO)})


def precificar(user_id, custo, margem=None, apenas_ativos=True):
    """Preço sugerido por canal (markup reverso + faixa + Raio-X), usando a config salva."""
    cfg = obter_config(user_id)
    base = float(custo) + cfg["embalagem"] + cfg["frete"]
    m = float(margem) if margem is not None else cfg["margem_padrao"]
    resultados = []
    for c in cfg["canais"]:
        if apenas_ativos and not c.get("ativo", True):
            continue
        r = precificar_canal(base, c["faixas"], cfg["imposto"], cfg["cartao"], m)
        if r is None:
            resultados.append({"canal": c["canal"], "nome": c["nome"],
                               "erro": "Nenhuma faixa viável para essa margem."})
        else:
            r.update({"canal": c["canal"], "nome": c["nome"]})
            resultados.append(r)
    return {"custo": float(custo), "base": _r2(base), "margem": m,
            "imposto": cfg["imposto"], "cartao": cfg["cartao"], "canais": resultados}


# --------------------------------------------------------------------------- #
# AVALIAÇÃO p/ Catálogo/Dashboard: margem REAL no preço atual + preço sugerido
# (mesma config de faixas — a casa toda fala a mesma língua de preço)
# --------------------------------------------------------------------------- #
def _faixa_para_preco(faixas, preco):
    """Retorna a faixa cujo intervalo (prev, ate] contém o preço."""
    fx = _ordenar(faixas)
    prev = 0.0
    for f in fx:
        hi = float("inf") if f.get("ate") is None else float(f["ate"])
        if prev < preco <= hi:
            return f
        prev = hi
    return fx[-1] if fx else {"ate": None, "comissao": 0.0, "fixo": 0.0, "fixo_pct": 0.0}


def _canal_cfg(cfg, canal=None):
    canais = cfg.get("canais") or []
    if canal:
        c = next((x for x in canais if x.get("canal") == canal), None)
        if c:
            return c
    return next((x for x in canais if x.get("ativo")), canais[0] if canais else None)


def avaliar_com_cfg(cfg, custo, preco_atual=0.0, canal=None) -> dict:
    """Sem ler o banco (recebe a config). Devolve preço sugerido + margem real no preço atual."""
    base = float(custo) + cfg["embalagem"] + cfg["frete"]
    c = _canal_cfg(cfg, canal)
    if not c:
        return {"canal": canal, "preco_sugerido": None, "margem_sugerida": None, "margem_atual": None}
    sug = precificar_canal(base, c["faixas"], cfg["imposto"], cfg["cartao"], cfg["margem_padrao"])
    margem_atual = None
    if preco_atual and float(preco_atual) > 0:
        faixa = _faixa_para_preco(c["faixas"], float(preco_atual))
        margem_atual = _raio_x(base, float(preco_atual), faixa, cfg["imposto"], cfg["cartao"])["margem_real"]
    return {
        "canal": c["canal"],
        "preco_sugerido": sug["preco"] if sug else None,
        "margem_sugerida": sug["raio_x"]["margem_real"] if sug else None,
        "margem_atual": margem_atual,
    }


def avaliar(user_id, custo, preco_atual=0.0, canal=None) -> dict:
    return avaliar_com_cfg(obter_config(user_id), custo, preco_atual, canal)
