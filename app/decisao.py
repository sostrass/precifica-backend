"""Fase 4 — Motor de decisao de preco por concorrencia.

Dado o preco dos concorrentes (vindos do radar), o custo e um PISO de margem
liquida, decide um preco-alvo conforme a estrategia (igualar / ficar abaixo /
segurar / premium) que NUNCA fica abaixo da viabilidade (piso de margem).

Esta era a peca que faltava: o radar so mostrava preco; aqui o sistema decide.
"""

from . import pricing
from .pricing import preco_para_margem  # fonte única da fórmula (reexportado)


def decidir_preco(*, custo_base: float, preco_atual: float,
                  precos_concorrentes: list,
                  canal: str = "mercadolivre",
                  comissao=None, fixo=None,
                  imposto: float = 0.0, cartao: float = 0.0,
                  piso_margem: float = 15.0,
                  estrategia: str = "match", delta: float = 1.0,
                  delta_tipo: str = "pct", passo_min: float = 0.50) -> dict:
    """Decide o preco para um canal.

    estrategia: 'match' (igualar menor concorrente) | 'undercut' (ficar abaixo)
                | 'premium' (ficar acima) | 'hold' (segurar o atual)
    delta_tipo: 'pct' (delta em %) ou 'reais' (delta em R$)
    passo_min:  nao recomenda mexer se a diferenca for menor que isso (evita churn)
    """
    cfg = pricing.PLATAFORMAS.get(canal, {})
    comissao = cfg.get("comissao", 0.0) if comissao is None else float(comissao)
    fixo = cfg.get("fixo", 0.0) if fixo is None else float(fixo)

    precos = sorted(float(p) for p in (precos_concorrentes or []) if p and float(p) > 0)
    piso = preco_para_margem(custo_base, comissao, fixo, imposto, cartao, piso_margem)

    out = {
        "canal": canal,
        "estrategia": estrategia,
        "preco_atual": round(float(preco_atual), 2),
        "piso_margem": piso_margem,
        "preco_piso": round(piso, 2) if piso else None,
        "concorrentes": [round(p, 2) for p in precos],
    }

    if not precos:
        out.update(acao="manter", alvo=None,
                   preco_recomendado=round(float(preco_atual), 2),
                   margem_recomendado=round(
                       pricing.margem_liquida(preco_atual, custo_base, comissao,
                                              fixo, imposto, cartao), 2),
                   abaixo_do_piso=False,
                   motivo="Sem preço de concorrente: nada a decidir, mantém o atual.")
        return out

    menor = precos[0]
    if estrategia == "match":
        alvo = menor
    elif estrategia == "undercut":
        alvo = menor * (1 - delta / 100) if delta_tipo == "pct" else menor - delta
    elif estrategia == "premium":
        alvo = menor * (1 + delta / 100) if delta_tipo == "pct" else menor + delta
    else:  # hold
        alvo = float(preco_atual)

    abaixo_do_piso = piso is not None and alvo < piso
    recomendado = max(alvo, piso) if piso is not None else alvo
    margem = pricing.margem_liquida(recomendado, custo_base, comissao, fixo, imposto, cartao)

    diff = recomendado - float(preco_atual)
    if abs(diff) < passo_min:
        acao = "manter"
    elif diff > 0:
        acao = "subir"
    else:
        acao = "baixar"

    if abaixo_do_piso:
        motivo = (f"Menor concorrente (R$ {menor:.2f}) está abaixo do seu piso de "
                  f"viabilidade (R$ {piso:.2f}). Travado no piso — abaixo disso a margem "
                  f"cai de {piso_margem:.0f}%. Não vale a pena brigar nesse preço.")
    elif acao == "manter":
        motivo = (f"Preço atual (R$ {preco_atual:.2f}) já está no alvo "
                  f"(diferença menor que R$ {passo_min:.2f}).")
    else:
        verbo = "Subir" if acao == "subir" else "Baixar"
        motivo = (f"{verbo} para R$ {recomendado:.2f} (estratégia {estrategia} sobre o menor "
                  f"concorrente R$ {menor:.2f}) — margem líquida resultante {margem:.1f}%.")

    out.update(acao=acao, alvo=round(alvo, 2),
               preco_recomendado=round(recomendado, 2),
               margem_recomendado=round(margem, 2),
               abaixo_do_piso=abaixo_do_piso, motivo=motivo)
    return out
