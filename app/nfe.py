"""Módulo de Nota Fiscal (NF-e) — Enterprise.

Fluxo (confirmado contra a ajuda oficial do Bling):
  1. Lê as NF-e com situação PENDENTE no Bling (só Pendente/Rejeitada são editáveis).
  2. O usuário seleciona quais notas e informa o desconto (por item, com inputs
     editáveis) e se remove o frete.
  3. O motor recalcula os totais e monta a alteração.
  4. Devolve a nota alterada para o Bling (PUT). O envio ao Sefaz é feito DENTRO do
     Bling (lá está o certificado A1) — este módulo não precisa de certificado.

A MATEMÁTICA (desconto por item + zerar frete + recálculo) é pura e testável aqui.
As chamadas ao Bling ficam em bling.py e não são testáveis sem token/rede.

IMPORTANTE sobre o schema do Bling: os nomes exatos de alguns campos da NF-e v3
(itens, valor, desconto, frete) podem variar. `normalizar_nfe` e `montar_alteracao`
são defensivos e concentram esse mapeamento num lugar só, fácil de ajustar.
"""

from typing import Optional


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _r2(v) -> float:
    return round(_num(v) + 0.0, 2)


# --------------------------------------------------------------------------- #
# MATEMÁTICA PURA (testável sem Bling)
# --------------------------------------------------------------------------- #
def desconto_do_item(valor_unitario, quantidade, desconto, desconto_tipo="valor") -> dict:
    """Calcula o desconto de UM item.

    desconto_tipo='percentual' -> desconto é % sobre o bruto do item.
    desconto_tipo='valor'      -> desconto é R$ absoluto no item (na linha inteira).
    O desconto nunca passa do valor bruto nem fica negativo.
    """
    bruto = _num(valor_unitario) * _num(quantidade)
    if desconto_tipo == "percentual":
        desc = bruto * _num(desconto) / 100
    else:
        desc = _num(desconto)
    desc = min(max(desc, 0.0), bruto)
    return {"bruto": _r2(bruto), "desconto_reais": _r2(desc), "liquido": _r2(bruto - desc)}


def aplicar_edicao(itens, *, desconto_tipo="percentual", desconto_valor=0.0,
                   descontos_por_item: Optional[dict] = None,
                   remover_frete=True, frete_atual=0.0) -> dict:
    """Aplica a regra de edição sobre uma lista de itens normalizados.

    itens: [{indice, descricao, quantidade, valor_unitario, desconto?}]
    descontos_por_item: {indice: desconto} sobrescreve o desconto padrão naquele item
        (são os inputs editáveis por linha). A chave casa com 'indice'.
    Devolve os itens recalculados + os totais (antes/depois) para revisão.
    """
    descontos_por_item = descontos_por_item or {}
    linhas = []
    total_bruto = 0.0
    total_desconto = 0.0
    for it in itens:
        idx = it.get("indice")
        # input editável da linha tem prioridade; senão usa o desconto padrão do lote
        if idx in descontos_por_item:
            d = descontos_por_item[idx]
        elif str(idx) in descontos_por_item:
            d = descontos_por_item[str(idx)]
        else:
            d = desconto_valor
        calc = desconto_do_item(it.get("valor_unitario"), it.get("quantidade"),
                                d, desconto_tipo)
        total_bruto += calc["bruto"]
        total_desconto += calc["desconto_reais"]
        linhas.append({**it, "desconto_aplicado": _num(d),
                       "bruto": calc["bruto"], "desconto_reais": calc["desconto_reais"],
                       "liquido": calc["liquido"]})

    frete_antes = _r2(frete_atual)
    frete_depois = 0.0 if remover_frete else frete_antes
    total_produtos = _r2(total_bruto - total_desconto)
    total_nota = _r2(total_produtos + frete_depois)
    return {
        "itens": linhas,
        "resumo": {
            "total_bruto": _r2(total_bruto),
            "total_desconto": _r2(total_desconto),
            "total_produtos": total_produtos,
            "frete_antes": frete_antes,
            "frete_depois": _r2(frete_depois),
            "frete_removido": _r2(frete_antes - frete_depois),
            "total_nota": total_nota,
        },
    }


# --------------------------------------------------------------------------- #
# MAPEAMENTO Bling <-> visão normalizada (concentra os nomes de campo)
# --------------------------------------------------------------------------- #
def normalizar_nfe(raw: dict) -> dict:
    """Extrai do payload do Bling uma visão simples e editável da nota.

    Defensivo: aceita tanto {data:{...}} quanto o objeto direto, e nomes de campo
    alternativos de item (valor/valorUnitario, quantidade) e frete.
    """
    nfe = raw.get("data", raw) if isinstance(raw, dict) else {}
    itens_raw = nfe.get("itens") or nfe.get("itensNota") or []
    itens = []
    for i, it in enumerate(itens_raw):
        prod = it.get("produto", it) if isinstance(it, dict) else {}
        valor_unit = it.get("valor", it.get("valorUnitario", prod.get("valor", 0)))
        itens.append({
            "indice": i,
            "descricao": it.get("descricao") or prod.get("descricao") or prod.get("nome") or f"Item {i+1}",
            "codigo": it.get("codigo") or prod.get("codigo") or "",
            "quantidade": _num(it.get("quantidade", 1)),
            "valor_unitario": _num(valor_unit),
            "desconto": _num(it.get("desconto", 0)),
        })
    transporte = nfe.get("transporte") or {}
    frete = transporte.get("frete", transporte.get("valorFrete",
            nfe.get("frete", nfe.get("valorFrete", 0))))
    return {
        "id": nfe.get("id"),
        "numero": nfe.get("numero"),
        "serie": nfe.get("serie"),
        "situacao": nfe.get("situacao"),
        "contato": (nfe.get("contato") or {}).get("nome", ""),
        "frete": _num(frete),
        "itens": itens,
    }


# Mapa de situações da NF-e no Bling v3 (rótulos legíveis).
SITUACOES_NFE = {
    1: "Pendente", 2: "Cancelada", 3: "Aguardando recibo", 4: "Rejeitada",
    5: "Autorizada", 6: "Emitida DANFE", 7: "Registrada", 8: "Aguardando protocolo",
    9: "Denegada", 10: "Em digitação", 11: "Bloqueada",
}


def situacao_label(cod) -> str:
    try:
        return SITUACOES_NFE.get(int(cod), f"Situação {cod}")
    except (TypeError, ValueError):
        return "—"


def nota_editavel(cod) -> bool:
    """Só nota NÃO autorizada (pendente/rejeitada) pode ser alterada. Autorizada é imutável."""
    try:
        return int(cod or 0) in (1, 4)
    except (TypeError, ValueError):
        return False


def detalhar_nfe(raw: dict) -> dict:
    """Visão COMPLETA da nota (destinatário, totais, impostos, transporte, links).

    Validada contra o payload real do Bling v3 (NF-e modelo 55).
    """
    n = raw.get("data", raw) if isinstance(raw, dict) else {}
    contato = n.get("contato") or {}
    end = contato.get("endereco") or {}
    transporte = n.get("transporte") or {}
    itens = []
    for it in (n.get("itens") or []):
        imp = it.get("impostos") or {}
        itens.append({
            "codigo": it.get("codigo"),
            "descricao": it.get("descricao"),
            "quantidade": _num(it.get("quantidade")),
            "valor": _num(it.get("valor")),
            "valor_total": _num(it.get("valorTotal")),
            "ncm": it.get("classificacaoFiscal"),
            "cfop": it.get("cfop"),
            "tributos_aprox": _num(imp.get("valorAproximadoTotalTributos")),
        })
    partes = []
    if end.get("endereco"):
        partes.append(f"{end.get('endereco')}, {end.get('numero', 's/n')}")
    if end.get("complemento"):
        partes.append(end["complemento"])
    if end.get("bairro"):
        partes.append(end["bairro"])
    if end.get("municipio"):
        partes.append(f"{end.get('municipio')}/{end.get('uf', '')}")
    if end.get("cep"):
        partes.append(f"CEP {end['cep']}")
    sit = n.get("situacao")
    return {
        "id": n.get("id"),
        "numero": n.get("numero"),
        "serie": n.get("serie"),
        "situacao": sit,
        "situacao_label": situacao_label(sit),
        "editavel": nota_editavel(sit),
        "data_emissao": n.get("dataEmissao"),
        "chave_acesso": n.get("chaveAcesso"),
        "valor_nota": _num(n.get("valorNota")),
        "valor_frete": _num(n.get("valorFrete")),
        "simples_nacional": bool(n.get("optanteSimplesNacional")),
        "pedido_loja": n.get("numeroPedidoLoja"),
        "link_danfe": n.get("linkDanfe"),
        "link_pdf": n.get("linkPDF"),
        "link_xml": n.get("xml"),
        "destinatario": {
            "nome": contato.get("nome"),
            "documento": contato.get("numeroDocumento"),
            "telefone": contato.get("telefone") or "",
            "email": contato.get("email") or "",
            "endereco": ", ".join(partes),
        },
        "transporte": {
            "frete_por_conta": transporte.get("fretePorConta"),
            "transportador": (transporte.get("transportador") or {}).get("nome") or "—",
        },
        "itens": itens,
        "parcelas": [{"data": p.get("data"), "valor": _num(p.get("valor"))}
                     for p in (n.get("parcelas") or [])],
    }


def montar_alteracao(raw: dict, edicao: dict) -> dict:
    """Monta o payload de alteração (PUT) a partir do original do Bling + a edição.

    Estratégia: parte do objeto original (preservando todos os campos fiscais) e
    sobrescreve só o desconto de cada item e o frete. Os nomes de campo seguem o
    mapeamento assumido — ajuste aqui se a sua conta usar outro schema.
    """
    nfe = dict(raw.get("data", raw))
    linhas = edicao["itens"]
    itens_raw = nfe.get("itens") or nfe.get("itensNota") or []
    for linha in linhas:
        idx = linha["indice"]
        if 0 <= idx < len(itens_raw):
            # grava o desconto em R$ da linha (campo 'desconto' do item da NF-e)
            itens_raw[idx]["desconto"] = linha["desconto_reais"]
    if "itens" in nfe:
        nfe["itens"] = itens_raw
    elif "itensNota" in nfe:
        nfe["itensNota"] = itens_raw
    # zera o frete no transporte (e no total, conforme o schema)
    transporte = dict(nfe.get("transporte") or {})
    if edicao["resumo"]["frete_depois"] == 0:
        if "frete" in transporte:
            transporte["frete"] = 0
        transporte["valorFrete"] = 0
        nfe["transporte"] = transporte
        if "frete" in nfe:
            nfe["frete"] = 0
        if "valorFrete" in nfe:
            nfe["valorFrete"] = 0
    return nfe


# --------------------------------------------------------------------------- #
# ORQUESTRAÇÃO (usa os adaptadores do Bling)
# --------------------------------------------------------------------------- #
def editar_nota(user_id: int, nfe_id, *, desconto_tipo, desconto_valor,
                descontos_por_item=None, remover_frete=True, enviar=False):
    """Busca a nota no Bling, aplica a edição e devolve a revisão.

    enviar=False -> só recalcula e devolve para revisão (não toca no Bling de volta).
    enviar=True  -> também envia a alteração (PUT) para o Bling.
    """
    from . import bling  # import tardio (evita ciclo)

    raw = bling.obter_nfe(user_id, nfe_id)
    view = normalizar_nfe(raw)
    edicao = aplicar_edicao(
        view["itens"], desconto_tipo=desconto_tipo, desconto_valor=desconto_valor,
        descontos_por_item=descontos_por_item, remover_frete=remover_frete,
        frete_atual=view["frete"],
    )
    resultado = {"id": view["id"], "numero": view["numero"], "serie": view["serie"],
                 "contato": view["contato"], "enviado": False, **edicao}
    if enviar:
        payload = montar_alteracao(raw, edicao)
        bling.atualizar_nfe(user_id, nfe_id, payload)
        resultado["enviado"] = True
    return resultado


def processar_automatico(user_id: int, cfg) -> dict:
    """Modo automático: aplica a regra padrão a TODAS as NF-e pendentes e devolve.

    Pensado para um gatilho (webhook de NF do Bling ou polling agendado). Cada nota
    é editada e enviada de volta. Retorna um relatório por nota.
    """
    from . import bling

    pendentes = bling.listar_nfe(user_id, situacao=cfg.situacao_pendente)
    notas = pendentes.get("data", []) if isinstance(pendentes, dict) else []
    relatorio = []
    for n in notas:
        nid = n.get("id")
        try:
            r = editar_nota(user_id, nid, desconto_tipo=cfg.desconto_tipo,
                            desconto_valor=cfg.desconto_valor,
                            remover_frete=cfg.remover_frete, enviar=True)
            relatorio.append({"id": nid, "ok": True,
                              "total_nota": r["resumo"]["total_nota"]})
        except Exception as e:  # noqa: BLE001
            relatorio.append({"id": nid, "ok": False, "erro": str(e)})
    return {"processadas": len(relatorio), "relatorio": relatorio}
