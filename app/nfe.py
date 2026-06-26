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

import re
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
    bruto_r = _r2(bruto)
    desc_r = _r2(desc)
    # líquido SEMPRE = bruto − desconto, ambos arredondados (evita divergir 1 centavo do total)
    return {"bruto": bruto_r, "desconto_reais": desc_r, "liquido": _r2(bruto_r - desc_r)}


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
def normalizar_nfe(raw: dict, lojas_map: dict | None = None) -> dict:
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
    contato = nfe.get("contato") or {}
    return {
        "id": nfe.get("id"),
        "numero": nfe.get("numero"),
        "serie": nfe.get("serie"),
        "situacao": nfe.get("situacao"),
        "contato": contato.get("nome", ""),
        "documento": contato.get("numeroDocumento", ""),
        "frete": _num(frete),
        "itens": itens,
        "parcelas": [{"data": p.get("data"), "valor": _num(p.get("valor"))}
                     for p in (nfe.get("parcelas") or [])],
        "valor_nota": _num(nfe.get("valorNota")),
        "plataforma": plataforma_nota(nfe, lojas_map),
        "pedido_loja": nfe.get("numeroPedidoLoja"),
    }


# Mapa de situações da NF-e no Bling v3 (rótulos legíveis).
SITUACOES_NFE = {
    1: "Pendente", 2: "Cancelada", 3: "Aguardando recibo", 4: "Rejeitada",
    5: "Autorizada", 6: "Emitida DANFE", 7: "Registrada", 8: "Aguardando protocolo",
    9: "Denegada", 10: "Em digitação", 11: "Bloqueada",
}

MODELO_NFE = {"55": "NF-e (modelo 55)", "65": "NFC-e (modelo 65)"}
FINALIDADE_NFE = {"1": "Normal", "2": "Complementar", "3": "Ajuste", "4": "Devolução"}

# Marketplace pela raiz do CNPJ do intermediador (8 primeiros dígitos)
_PLATAFORMA_CNPJ = {
    "35635824": "Shopee",
    "10573521": "Mercado Livre",
    "03361252": "Mercado Livre",
    "15436940": "Amazon",
    "47960950": "Magalu",
    "09358108": "Magalu",
    "00776574": "Americanas",
    "01882369": "TikTok Shop",
}


# Palavras-chave no nome/integração da loja do Bling → plataforma (fallback quando a nota
# não traz intermediador — caso de e-commerce próprio como NuvemShop).
_LOJA_KW_PLATAFORMA = [
    ("shopee", "Shopee"),
    ("mercadolivre", "Mercado Livre"), ("mercado livre", "Mercado Livre"), ("meli", "Mercado Livre"),
    ("nuvemshop", "NuvemShop"), ("nuvem shop", "NuvemShop"), ("tiendanube", "NuvemShop"), ("nuvem", "NuvemShop"),
    ("amazon", "Amazon"),
    ("magalu", "Magalu"), ("magazine", "Magalu"),
    ("americanas", "Americanas"), ("b2w", "Americanas"), ("submarino", "Americanas"),
    ("tiktok", "TikTok Shop"),
    ("shein", "Shein"),
    ("olist", "Olist"),
    ("tray", "Tray"),
    ("woocommerce", "WooCommerce"), ("woo", "WooCommerce"),
    ("loja integrada", "Loja Integrada"), ("lojaintegrada", "Loja Integrada"),
    ("vtex", "VTEX"),
    ("shopify", "Shopify"),
    ("site", "Site próprio"), ("e-commerce", "Site próprio"), ("ecommerce", "Site próprio"),
]


def _mapear_lojas_plataforma(user_id: int) -> dict:
    """Mapa {loja_id(str): plataforma} a partir das lojas da conta no Bling.
    NÃO bloqueia: usa só o cache já pronto. Se o cache estiver frio, dispara a descoberta
    em segundo plano e retorna {} agora (o badge aparece no próximo carregamento)."""
    from . import bling
    try:
        lojas = bling.lojas_cacheadas(user_id)
    except Exception:  # noqa: BLE001
        return {}
    if lojas is None:
        # aquece o cache em background — não trava esta requisição
        try:
            import threading
            threading.Thread(target=lambda: _warm_lojas_silencioso(user_id), daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
        return {}
    out = {}
    for lid, info in (lojas.items() if isinstance(lojas, dict) else []):
        texto = f"{(info or {}).get('nome', '')} {(info or {}).get('integracao', '')}".lower()
        plat = None
        for kw, nome in _LOJA_KW_PLATAFORMA:
            if kw in texto:
                plat = nome
                break
        out[str(lid)] = plat or (info or {}).get("nome") or None
    return out


def _warm_lojas_silencioso(user_id: int) -> None:
    """Popula o cache de lojas (faz a descoberta no Bling). Roda em background; engole erros."""
    try:
        from . import bling
        bling.lojas_da_conta(user_id)
    except Exception:  # noqa: BLE001
        pass


def plataforma_nota(note: dict, lojas_map: dict | None = None):
    """Detecta a plataforma da nota: 1) pelo CNPJ do intermediador (Shopee, ML…);
    2) se não houver, pela loja da conta no Bling (resolve NuvemShop, site próprio etc.)."""
    inter = note.get("intermediador") or {}
    cnpj = re.sub(r"\D", "", str(inter.get("cnpj") or ""))
    if cnpj[:8] in _PLATAFORMA_CNPJ:
        return _PLATAFORMA_CNPJ[cnpj[:8]]
    if lojas_map:
        loja = note.get("loja") or {}
        lid = str(loja.get("id") if isinstance(loja, dict) else loja or "")
        if lid and lojas_map.get(lid):
            return lojas_map[lid]
    return None


def diagnosticar_edicao(user_id: int, nfe_id, *, desconto_tipo, desconto_valor, remover_frete=True) -> dict:
    """Dry-run: mostra a estrutura real da nota e o payload que SERIA enviado (sem enviar).
    Serve para ver as parcelas, os campos da nota e se a soma das parcelas bate com o total."""
    from . import bling
    import copy as _copy
    raw = bling.obter_nfe(user_id, nfe_id)
    note = raw.get("data", raw) if isinstance(raw, dict) else {}
    view = normalizar_nfe(raw)
    edicao = aplicar_edicao(
        view["itens"], desconto_tipo=desconto_tipo, desconto_valor=desconto_valor,
        remover_frete=remover_frete, frete_atual=view["frete"],
    )
    # cópia profunda: montar_alteracao altera itens in-place; não pode poluir o 'note' original
    payload = montar_alteracao(_copy.deepcopy(raw), edicao)
    parc_pay = payload.get("parcelas") or []
    itens_pay = payload.get("itens") or payload.get("itensNota") or []
    soma_parc = _r2(sum(_parcela_val(p) for p in parc_pay))
    # o Bling valida soma(parcelas) == valorNota do envio (ele usa o valorNota direto)
    total_calc = _r2(_num(payload.get("valorNota")))

    def _resumo_itens(itens):
        """Campos relevantes de cada item para conferência (preço, bruto, desconto, tributo)."""
        out = []
        for it in (itens or []):
            imp = it.get("impostos") or {}
            out.append({
                "codigo": it.get("codigo"),
                "descricao": it.get("descricao"),
                "quantidade": _num(it.get("quantidade")),
                "valor": _num(it.get("valor")),
                "valorTotal": _num(it.get("valorTotal")),
                "desconto": it.get("desconto"),
                "valorAproximadoTotalTributos": imp.get("valorAproximadoTotalTributos"),
            })
        return out

    def _resumo_parcelas(parcelas):
        return [{"data": p.get("data"), "valor": _parcela_val(p),
                 "formaPagamento": p.get("formaPagamento"), "observacoes": p.get("observacoes")}
                for p in (parcelas or [])]

    soma_itens_pay = _r2(sum(_num(it.get("valorTotal")) - _num(it.get("desconto")) for it in itens_pay)
                         + _num(payload.get("valorFrete")))

    return {
        "bate": abs(soma_parc - total_calc) < 0.01,
        "consistente_itens": abs(soma_itens_pay - total_calc) < 0.01,
        "soma_parcelas_payload": soma_parc,
        "soma_itens_payload": soma_itens_pay,
        "total_calculado": total_calc,            # = valorNota do envio
        # comparação lado a lado: o que está na nota HOJE × o que SERIA enviado
        "comparacao": {
            "original": {
                "valorNota": _num(note.get("valorNota")),
                "valorFrete": _num(note.get("valorFrete")),
                "desconto_nota": _desconto_nota_valor(note),
                "itens": _resumo_itens(note.get("itens") or note.get("itensNota")),
                "parcelas": _resumo_parcelas(note.get("parcelas")),
            },
            "enviado": {
                "valorNota": _num(payload.get("valorNota")),
                "valorFrete": _num(payload.get("valorFrete")),
                "desconto_nota": _desconto_nota_valor(payload),
                "itens": _resumo_itens(itens_pay),
                "parcelas": _resumo_parcelas(parc_pay),
            },
        },
        "raw": {
            "valorNota": note.get("valorNota"),
            "desconto_nota": _desconto_nota_valor(note),
            "frete_nota": _num(note.get("valorFrete") or (note.get("transporte") or {}).get("frete") or (note.get("transporte") or {}).get("valorFrete")),
            "intermediador": note.get("intermediador"),
            "loja": note.get("loja"),
            "tem_parcelas": bool(note.get("parcelas")),
            "chaves_nota": sorted([k for k in note.keys()]),
            "campos_numericos_raiz": {k: v for k, v in note.items()
                                      if isinstance(v, (int, float)) and not isinstance(v, bool)},
        },
        # objetos COMPLETOS para conferência total (o botão "copiar" leva tudo)
        "payload_completo": payload,
        "nota_original": note,
        "resumo": edicao["resumo"],
    }


def _motivo_rejeicao(n: dict):
    """Tenta extrair o motivo de rejeição da nota (o Bling nem sempre expõe na própria NF-e)."""
    for k in ("motivo", "mensagemRetorno", "xMotivo", "motivoRejeicao", "mensagem", "observacoes"):
        v = n.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def resumo_extra_raw(raw: dict, lojas_map: dict | None = None) -> dict:
    """Extrai do payload bruto tudo que a lista não traz: valor, plataforma, UF, tributos
    aproximados (IBPT), pedido, chave de acesso, links e motivo de rejeição."""
    n = raw.get("data", raw) if isinstance(raw, dict) else {}
    end = (n.get("contato") or {}).get("endereco") or {}
    trib = 0.0
    for it in (n.get("itens") or []):
        trib += _num((it.get("impostos") or {}).get("valorAproximadoTotalTributos"))
    return {
        "valor": valor_total_raw(raw),
        "plataforma": plataforma_nota(n, lojas_map),
        "uf": end.get("uf") or None,
        "municipio": end.get("municipio") or None,
        "tributos": _r2(trib),
        "pedido": n.get("numeroPedidoLoja"),
        "chave": n.get("chaveAcesso") or None,
        "link_xml": n.get("xml") or None,
        "link_danfe": n.get("linkDanfe") or None,
        "link_pdf": n.get("linkPDF") or None,
        "motivo": _motivo_rejeicao(n),
    }


def valor_total_raw(raw: dict) -> float:
    """Total da nota a partir do payload bruto do Bling (GET individual)."""
    n = raw.get("data", raw) if isinstance(raw, dict) else {}
    v = n.get("valorNota")
    if v is None:
        v = n.get("total")
    if v is None:
        v = n.get("valor")
    return _num(v)


def enriquecer_valores(user_id: int, notas: list, limite: int = 60) -> list:
    """O endpoint /nfe (lista) do Bling NÃO traz o valor total — só o GET individual.
    Para as notas sem valor, busca o total real (respeitando o rate limit, com teto)."""
    from . import bling  # import tardio (evita ciclo)
    for n in notas[:limite]:
        if not n.get("id"):
            continue
        if n.get("valor") and n.get("plataforma"):
            continue
        try:
            raw = bling.obter_nfe(user_id, n["id"])
            note = raw.get("data", raw) if isinstance(raw, dict) else {}
            if not n.get("valor"):
                n["valor"] = valor_total_raw(raw)
            n["plataforma"] = plataforma_nota(note)
        except Exception:
            pass  # mantém o que tiver; não derruba a listagem por causa de uma nota
    return notas


def resumir_lista(raw: dict) -> list:
    """Normaliza a lista de NF-e do Bling para a UI: id, número, situação (+rótulo),
    se é editável, cliente, valor e data. Defensivo quanto aos nomes de campo."""
    notas = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
    out = []
    for n in notas:
        if not isinstance(n, dict):
            continue
        contato = n.get("contato") or {}
        nome_cli = contato.get("nome") if isinstance(contato, dict) else (contato or None)
        cod = n.get("situacao")
        loja = n.get("loja")
        out.append({
            "id": n.get("id"),
            "numero": n.get("numero"),
            "serie": n.get("serie"),
            "situacao": cod,
            "situacao_label": situacao_label(cod),
            "editavel": nota_editavel(cod),
            "cliente": nome_cli,
            "documento": contato.get("numeroDocumento") if isinstance(contato, dict) else None,
            "valor": _num(n.get("valorNota") if n.get("valorNota") is not None else (n.get("total") or n.get("valor"))),
            "data": n.get("dataEmissao") or n.get("dataOperacao") or n.get("data"),
            "loja_id": loja.get("id") if isinstance(loja, dict) else loja,
            "tipo": n.get("tipo"),
        })
    return out


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


def detalhar_nfe(raw: dict, lojas_map: dict | None = None) -> dict:
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
    _tipo = n.get("tipo")
    _modelo = n.get("modelo")
    _fin = n.get("finalidade")
    _nat = n.get("naturezaOperacao")
    natureza = _nat.get("descricao") if isinstance(_nat, dict) else (_nat or None)
    _inter = n.get("intermediador") or {}
    intermediador_nome = (_inter.get("nomeUsuario") or _inter.get("nome")
                          or plataforma_nota(n, lojas_map)) if _inter else None
    valor_produtos = _r2(sum(_num(it.get("valorTotal")) for it in (n.get("itens") or [])))
    return {
        "id": n.get("id"),
        "numero": n.get("numero"),
        "serie": n.get("serie"),
        "situacao": sit,
        "situacao_label": situacao_label(sit),
        "editavel": nota_editavel(sit),
        "modelo": _modelo,
        "modelo_label": MODELO_NFE.get(str(_modelo), f"Modelo {_modelo}" if _modelo not in (None, "") else None),
        "tipo": _tipo,
        "tipo_label": {0: "Entrada", 1: "Saída"}.get(_tipo) if _tipo is not None else None,
        "finalidade": _fin,
        "finalidade_label": FINALIDADE_NFE.get(str(_fin)) if _fin not in (None, "") else None,
        "data_emissao": n.get("dataEmissao"),
        "data_operacao": n.get("dataOperacao"),
        "natureza_operacao": natureza,
        "observacoes": (n.get("observacoes") or n.get("informacoesAdicionais") or "").strip() or None,
        "vendedor": (n.get("vendedor") or {}).get("nome") if isinstance(n.get("vendedor"), dict) else None,
        "intermediador": intermediador_nome,
        "chave_acesso": n.get("chaveAcesso"),
        "valor_produtos": valor_produtos,
        "valor_nota": _num(n.get("valorNota")),
        "valor_frete": _num(n.get("valorFrete")),
        "desconto_nota": _desconto_nota_valor(n),
        "simples_nacional": bool(n.get("optanteSimplesNacional")),
        "pedido_loja": n.get("numeroPedidoLoja"),
        "plataforma": plataforma_nota(n, lojas_map),
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


_NFE_READONLY = {
    "id", "situacao", "chaveAcesso", "linkDanfe", "linkPDF", "xml", "recibo",
    "protocolo", "digestValue", "dataAutorizacao", "dataInclusao", "dataAlteracao",
    "tipoIntegracao",
}


def _ref_id(v):
    """Reduz um objeto de referência {id, descricao, ...} para apenas {id}."""
    if isinstance(v, dict) and v.get("id"):
        return {"id": v["id"]}
    return v


_PARCELA_VAL_KEYS = ("valor", "valorParcela", "valorParc", "vlr")


def _parcela_val(p: dict) -> float:
    for k in _PARCELA_VAL_KEYS:
        if k in p:
            return _num(p.get(k))
    return 0.0


def _set_parcela_val(p: dict, v: float):
    # grava no mesmo campo que existia; se nenhum existir, usa 'valor'
    for k in _PARCELA_VAL_KEYS:
        if k in p:
            p[k] = v
            return
    p["valor"] = v


_CAMPOS_DESCONTO_NOTA = ("desconto", "valorDesconto", "descontoTotal", "valorDescontos")


def _desconto_nota_valor(nfe: dict) -> float:
    """Lê o desconto no nível da nota (escalar ou objeto {valor/percentual}), se houver."""
    total = 0.0
    for campo in _CAMPOS_DESCONTO_NOTA:
        v = nfe.get(campo)
        if isinstance(v, dict):
            total += _num(v.get("valor") or v.get("valorDesconto") or 0)
        elif v is not None:
            total += _num(v)
    return _r2(total)


def _zerar_desconto_nota(nfe: dict) -> None:
    """Zera qualquer desconto no nível da nota (in-place)."""
    for campo in _CAMPOS_DESCONTO_NOTA:
        if campo not in nfe:
            continue
        v = nfe[campo]
        if isinstance(v, dict):
            nfe[campo] = {k: (0 if k in ("valor", "percentual", "valorDesconto") else val)
                          for k, val in v.items()}
        elif v not in (None, 0):
            nfe[campo] = 0


def _reescalar_parcelas(parcelas: list, novo_total: float) -> list:
    """Reescala as parcelas para somarem exatamente o novo total da nota.
    O Bling recusa o PUT com 'Total das parcelas difere do total da nota' se não bater.
    Robusto ao nome do campo de valor da parcela."""
    if not parcelas or novo_total <= 0:
        return parcelas
    soma = sum(_parcela_val(p) for p in parcelas)
    if soma <= 0:
        for i, p in enumerate(parcelas):
            _set_parcela_val(p, _r2(novo_total) if i == 0 else 0.0)
        return parcelas
    if abs(soma - novo_total) < 0.01:
        return parcelas  # já bate
    acc = 0.0
    n = len(parcelas)
    for i, p in enumerate(parcelas):
        if i < n - 1:
            v = round(_parcela_val(p) * novo_total / soma, 2)
            _set_parcela_val(p, v)
            acc += v
        else:
            _set_parcela_val(p, _r2(novo_total - acc))  # última absorve o arredondamento
    return parcelas


def _liquido_item(it: dict) -> float:
    """Total líquido de um item, arredondado a 2 casas — como o Bling calcula por item."""
    v = it.get("valor")
    if v is None:
        v = it.get("valorUnitario")
    q = it.get("quantidade") if it.get("quantidade") is not None else 1
    d = _num(it.get("desconto"))
    return _r2(_num(v) * _num(q) - d)


def montar_alteracao(raw: dict, edicao: dict) -> dict:
    """Monta o payload de alteração (PUT) a partir do original do Bling + a edição.

    O DESCONTO vai no CAMPO CORRETO de cada item (`desconto` = vDesc da NF-e), que é onde
    o cálculo de aplicação acontece. O preço de venda (`valor`) é PRESERVADO e `valorTotal`
    = bruto (valor × qtd). Assim fica fiscalmente coerente:
        item líquido = valorTotal − desconto   (260 − 234 = 26)
        valorNota    = Σ(valorTotal − desconto) + frete   (26 + 0 = 26)
        parcelas     = valorNota                (26)
    Essa é a forma que transmite certo pra SEFAZ (vProd 260 − vDesc 234 = vNF 26).
    O tributo aproximado (IBPT) é escalado proporcionalmente (campo informativo).
    """
    nfe = dict(raw.get("data", raw))
    resumo = edicao.get("resumo", {})
    linhas = edicao["itens"]
    itens_raw = nfe.get("itens") or nfe.get("itensNota") or []

    frete_depois = _r2(_num(resumo.get("frete_depois")))
    novo_total_resumo = _r2(_num(resumo.get("total_nota")))
    valor_original = _num(nfe.get("valorNota")) or 0.0
    ratio = (novo_total_resumo / valor_original) if valor_original > 0 else 1.0

    for linha in linhas:
        idx = linha["indice"]
        if not (0 <= idx < len(itens_raw)):
            continue
        it = itens_raw[idx]
        qty = _num(it.get("quantidade")) or 1
        it["valor"] = _r2(_num(it.get("valor")))             # PRESERVA o preço de venda (vUnCom)
        it["valorTotal"] = _r2(_num(it.get("valor")) * qty)  # bruto (valor × qtd) = vProd
        it["desconto"] = _r2(linha["desconto_reais"])        # <-- DESCONTO no campo correto (vDesc)
        imp = it.get("impostos")
        if isinstance(imp, dict) and imp.get("valorAproximadoTotalTributos") is not None and ratio != 1.0:
            imp["valorAproximadoTotalTributos"] = _r2(_num(imp["valorAproximadoTotalTributos"]) * ratio)
    if "itens" in nfe:
        nfe["itens"] = itens_raw
    elif "itensNota" in nfe:
        nfe["itensNota"] = itens_raw

    # zera qualquer desconto no NÍVEL DA NOTA (o desconto agora está nos itens; evita dupla contagem)
    _zerar_desconto_nota(nfe)

    # total da nota = Σ(valorTotal − desconto) + frete — consistente com os itens enviados.
    # Definimos DIRETAMENTE (o Bling confia nesses campos e valida soma(parcelas) == valorNota).
    total_itens = _r2(sum(_num(it.get("valorTotal")) - _num(it.get("desconto")) for it in itens_raw))
    novo_total = _r2(total_itens + frete_depois)
    nfe["valorNota"] = novo_total
    nfe["valorFrete"] = frete_depois
    transporte = nfe.get("transporte")
    if isinstance(transporte, dict) and frete_depois == 0:
        if "frete" in transporte:
            transporte["frete"] = 0
        if "valorFrete" in transporte:
            transporte["valorFrete"] = 0

    # parcelas somam exatamente o novo total (== valorNota)
    if nfe.get("parcelas"):
        nfe["parcelas"] = _reescalar_parcelas(nfe["parcelas"], novo_total)

    # remove campos gerados pelo Bling (read-only) — MAS mantém valorNota e valorFrete
    for campo in list(nfe.keys()):
        if campo in _NFE_READONLY:
            nfe.pop(campo, None)
    # objetos de referência: o Bling espera {id}, não o objeto inteiro
    for ref in ("naturezaOperacao", "loja", "categoria", "vendedor", "deposito"):
        if ref in nfe:
            nfe[ref] = _ref_id(nfe[ref])
    return nfe


# --------------------------------------------------------------------------- #
# ORQUESTRAÇÃO (usa os adaptadores do Bling)
# --------------------------------------------------------------------------- #
def conciliar_shopee(user_id: int, nfe_id) -> dict:
    """Concilia o valor FISCAL da NF-e com o que de fato ocorreu na Shopee (escrow):
    quanto o comprador pagou pelo produto, o repasse líquido recebido e as taxas da Shopee.
    Ajuda a conferir se o desconto aplicado na nota reflete a venda real."""
    from . import bling
    raw = bling.obter_nfe(user_id, nfe_id)
    note = raw.get("data", raw) if isinstance(raw, dict) else {}
    plataforma = plataforma_nota(note)
    valor_nota = valor_total_raw(raw)
    order_sn = note.get("numeroPedidoLoja")

    if plataforma != "Shopee":
        return {"ok": False, "erro": f"Esta nota não é da Shopee (plataforma: {plataforma or 'não identificada'}).",
                "plataforma": plataforma}
    if not order_sn:
        return {"ok": False, "erro": "Nota sem número do pedido Shopee (numeroPedidoLoja)."}

    try:
        from . import shopee
        det = shopee.pedido_detalhe(user_id, order_sn)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"Não foi possível buscar o pedido na Shopee: {e}", "order_sn": order_sn}

    fin = det.get("financeiro") or {}
    pago_produto = float(det.get("total_pago") or 0)
    diff = _r2(valor_nota - pago_produto)
    # divergência relevante: acima de R$0,05 e de 1% do valor pago
    divergente = abs(diff) > 0.05 and (pago_produto == 0 or abs(diff) / pago_produto > 0.01)
    return {
        "ok": True, "order_sn": order_sn, "status": det.get("status"),
        "comprador": det.get("comprador"),
        "valor_nota": _r2(valor_nota),
        "pago_produto": _r2(pago_produto),
        "recebido_liquido": fin.get("liquido"),
        "taxas": fin.get("taxas"),
        "tem_escrow": fin.get("tem_escrow", False),
        "diferenca": diff, "divergente": divergente,
        "itens": det.get("itens"),
    }


def conciliar_shopee_lote(user_id: int, ids: list) -> dict:
    """Concilia várias notas Shopee de uma vez. Retorna resumo + itens divergentes."""
    linhas = []
    for nid in ids[:60]:
        try:
            r = conciliar_shopee(user_id, nid)
            r["id"] = nid
            linhas.append(r)
        except Exception as e:  # noqa: BLE001
            linhas.append({"id": nid, "ok": False, "erro": str(e)})
    ok = [l for l in linhas if l.get("ok")]
    divergentes = [l for l in ok if l.get("divergente")]
    return {
        "total": len(linhas), "conferidas": len(ok), "divergentes": len(divergentes),
        "soma_nota": _r2(sum(l.get("valor_nota") or 0 for l in ok)),
        "soma_pago": _r2(sum(l.get("pago_produto") or 0 for l in ok)),
        "soma_recebido": _r2(sum(l.get("recebido_liquido") or 0 for l in ok if l.get("recebido_liquido") is not None)),
        "linhas": linhas,
    }


_TETO_SIMPLES = 4_800_000.0       # teto anual do Simples Nacional (RBT12)
_SUBLIMITE_SIMPLES = 3_600_000.0  # sublimite p/ ICMS/ISS dentro do Simples


def _meses_trailing(qtd: int = 12):
    """Lista [(ano, mes)] dos últimos `qtd` meses, do mais antigo ao mês atual."""
    import datetime as _dt
    hoje = _dt.date.today()
    out = []
    a, m = hoje.year, hoje.month
    for _ in range(qtd):
        out.append((a, m))
        m -= 1
        if m == 0:
            m = 12; a -= 1
    return list(reversed(out))


def _ultimo_dia(ano: int, mes: int) -> int:
    import calendar
    return calendar.monthrange(ano, mes)[1]


def recalcular_faturamento(user_id: int, meses: int = 12,
                           amostra_max: int = 30, max_paginas: int = 40) -> dict:
    """Job PESADO (rodar em background): para cada um dos últimos `meses`, conta as NF-e de
    saída autorizadas (situação 5/6) do mês e estima o faturamento por média amostral
    (a lista do Bling não traz o valor). Grava um snapshot por mês. Idempotente."""
    from . import bling
    from .db import SessionLocal
    from .models import NfeFaturamentoMes

    resumo_meses = []
    for ano, mes in _meses_trailing(meses):
        d_ini = f"{ano:04d}-{mes:02d}-01"
        d_fim = f"{ano:04d}-{mes:02d}-{_ultimo_dia(ano, mes):02d}"
        autorizadas_ids = []
        parcial = False
        pagina = 1
        while pagina <= max_paginas:
            try:
                raw = bling.listar_nfe(user_id, pagina=pagina, limite=100,
                                       data_ini=d_ini, data_fim=d_fim)
            except Exception:  # noqa: BLE001
                break
            linhas = resumir_lista(raw)
            if not linhas:
                break
            for n in linhas:
                tipo = n.get("tipo")
                eh_saida = (tipo in (1, "1", None))
                if n.get("situacao") in (5, 6) and eh_saida and n.get("id"):
                    autorizadas_ids.append(n["id"])
            if len(linhas) < 100:
                break
            pagina += 1
        else:
            parcial = True

        qtd = len(autorizadas_ids)
        # média amostral do valor (busca nota a nota só na amostra)
        soma_amostra, lidas = 0.0, 0
        for nid in autorizadas_ids[:amostra_max]:
            try:
                soma_amostra += valor_total_raw(bling.obter_nfe(user_id, nid))
                lidas += 1
            except Exception:  # noqa: BLE001
                continue
        media = (soma_amostra / lidas) if lidas else 0.0
        total_est = _r2(media * qtd)

        with SessionLocal() as db:
            row = (db.query(NfeFaturamentoMes)
                     .filter_by(user_id=user_id, ano=ano, mes=mes).first())
            if row is None:
                row = NfeFaturamentoMes(user_id=user_id, ano=ano, mes=mes)
                db.add(row)
            row.qtd = qtd
            row.amostra = lidas
            row.total_estimado = total_est
            row.parcial = parcial
            db.commit()
        resumo_meses.append({"ano": ano, "mes": mes, "qtd": qtd, "amostra": lidas,
                             "total_estimado": total_est, "parcial": parcial})

    return {"ok": True, "meses": resumo_meses, "atualizado": True}


def resumo_faturamento(user_id: int) -> dict:
    """Lê os snapshots e monta o painel: RBT12, total do ano, projeção e % do teto/sublimite."""
    import datetime as _dt
    from .db import SessionLocal
    from .models import NfeFaturamentoMes

    with SessionLocal() as db:
        rows = (db.query(NfeFaturamentoMes)
                  .filter_by(user_id=user_id)
                  .order_by(NfeFaturamentoMes.ano, NfeFaturamentoMes.mes).all())
        dados = [{"ano": r.ano, "mes": r.mes, "qtd": r.qtd, "amostra": r.amostra,
                  "total_estimado": float(r.total_estimado or 0), "parcial": bool(r.parcial),
                  "atualizado_em": r.atualizado_em.isoformat() if r.atualizado_em else None}
                 for r in rows]

    if not dados:
        return {"tem_dados": False, "teto": _TETO_SIMPLES, "sublimite": _SUBLIMITE_SIMPLES}

    hoje = _dt.date.today()
    trailing = set(_meses_trailing(12))
    rbt12 = _r2(sum(d["total_estimado"] for d in dados if (d["ano"], d["mes"]) in trailing))
    total_ano = _r2(sum(d["total_estimado"] for d in dados if d["ano"] == hoje.year))
    meses_ano = max(1, hoje.month)
    projecao_ano = _r2(total_ano / meses_ano * 12)

    pct_teto = _r2(rbt12 / _TETO_SIMPLES * 100)
    pct_sublimite = _r2(rbt12 / _SUBLIMITE_SIMPLES * 100)
    if rbt12 >= _TETO_SIMPLES:
        alerta = "estourou"
    elif rbt12 >= 0.9 * _TETO_SIMPLES:
        alerta = "critico"
    elif rbt12 >= _SUBLIMITE_SIMPLES:
        alerta = "sublimite"
    elif rbt12 >= 0.8 * _SUBLIMITE_SIMPLES:
        alerta = "atencao"
    else:
        alerta = "ok"

    atualizado = max((d["atualizado_em"] for d in dados if d["atualizado_em"]), default=None)
    parcial = any(d["parcial"] for d in dados if (d["ano"], d["mes"]) in trailing)
    return {
        "tem_dados": True, "rbt12": rbt12, "total_ano": total_ano, "ano": hoje.year,
        "projecao_ano": projecao_ano, "teto": _TETO_SIMPLES, "sublimite": _SUBLIMITE_SIMPLES,
        "pct_teto": pct_teto, "pct_sublimite": pct_sublimite, "alerta": alerta,
        "parcial": parcial, "atualizado_em": atualizado,
        "meses": dados[-12:],
    }


def editar_nota(user_id: int, nfe_id, *, desconto_tipo, desconto_valor,
                descontos_por_item=None, remover_frete=True, enviar=False,
                desconto_plataformas=None):
    """Busca a nota no Bling, aplica a edição e devolve a revisão.

    enviar=False -> só recalcula e devolve para revisão (não toca no Bling de volta).
    enviar=True  -> também envia a alteração (PUT) para o Bling.
    desconto_plataformas -> dict opcional {plataforma: {tipo, valor}}. Se a nota for de uma
        plataforma com regra própria, ela substitui o desconto padrão (usado no lote/automático).
    """
    from . import bling  # import tardio (evita ciclo)

    raw = bling.obter_nfe(user_id, nfe_id)

    # regra por plataforma (sobrepõe a padrão só quando há override para a plataforma da nota)
    plataforma = None
    if desconto_plataformas:
        note = raw.get("data", raw) if isinstance(raw, dict) else {}
        plataforma = plataforma_nota(note)
        regra = desconto_plataformas.get(plataforma) if plataforma else None
        if regra and regra.get("tipo") in ("percentual", "valor"):
            desconto_tipo = regra["tipo"]
            desconto_valor = regra.get("valor") or 0

    view = normalizar_nfe(raw)
    edicao = aplicar_edicao(
        view["itens"], desconto_tipo=desconto_tipo, desconto_valor=desconto_valor,
        descontos_por_item=descontos_por_item, remover_frete=remover_frete,
        frete_atual=view["frete"],
    )
    resultado = {"id": view["id"], "numero": view["numero"], "serie": view["serie"],
                 "contato": view["contato"], "plataforma": plataforma, "enviado": False, **edicao}
    if enviar:
        payload = montar_alteracao(raw, edicao)
        bling.atualizar_nfe(user_id, nfe_id, payload)
        resultado["enviado"] = True
    return resultado


def processar_evento(user_id, nfe_id, cfg) -> dict:
    """Reage a um evento de NF-e do Bling (chamado pelo webhook, em background).

    Se a nota é editável (pendente) e o modo automático está ligado, aplica o desconto
    padrão e devolve ao Bling. NUNCA levanta — devolve um dict de status, pra não derrubar
    o processamento do webhook e pra o painel mostrar exatamente o que aconteceu.
    """
    from . import bling

    base = {"id": str(nfe_id)}
    try:
        raw = bling.obter_nfe(user_id, nfe_id)
    except bling.BlingNotFound as e:
        return {**base, "ok": False, "motivo": "nao_encontrada", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "motivo": "erro_busca", "detalhe": str(e)}

    n = raw.get("data", raw) if isinstance(raw, dict) else {}
    sit = n.get("situacao")
    base.update({"numero": n.get("numero"), "situacao": sit, "situacao_label": situacao_label(sit)})

    if not nota_editavel(sit):
        return {**base, "ok": False, "motivo": "nao_editavel"}
    if not getattr(cfg, "auto", False):
        return {**base, "ok": False, "motivo": "auto_desligado"}

    try:
        r = editar_nota(user_id, nfe_id, desconto_tipo=cfg.desconto_tipo,
                        desconto_valor=cfg.desconto_valor,
                        remover_frete=cfg.remover_frete, enviar=True)
        resumo = r.get("resumo", {})
        return {**base, "ok": True, "aplicado": True,
                "total_nota": resumo.get("total_nota"),
                "total_desconto": resumo.get("total_desconto")}
    except Exception as e:  # noqa: BLE001
        return {**base, "ok": False, "motivo": "erro_aplicar", "detalhe": str(e)}


def processar_ids(user_id: int, ids: list, cfg) -> dict:
    """Aplica a regra padrão de desconto a uma lista ESPECÍFICA de notas (seleção em massa).
    Reusa exatamente a mesma lógica do editar_nota. Retorna um relatório por nota."""
    relatorio = []
    aplicadas = 0
    for nid in ids:
        try:
            r = editar_nota(user_id, nid, desconto_tipo=cfg.desconto_tipo,
                            desconto_valor=cfg.desconto_valor,
                            remover_frete=cfg.remover_frete, enviar=True,
                            desconto_plataformas=getattr(cfg, "desconto_plataformas", None))
            aplicadas += 1
            relatorio.append({"id": nid, "numero": r.get("numero"), "ok": True,
                              "total_nota": r["resumo"]["total_nota"],
                              "total_desconto": r["resumo"].get("total_desconto")})
        except Exception as e:  # noqa: BLE001
            relatorio.append({"id": nid, "numero": None, "ok": False, "erro": str(e)})
    return {"processadas": len(ids), "aplicadas": aplicadas, "relatorio": relatorio}


def processar_automatico(user_id: int, cfg) -> dict:
    """Modo automático: aplica a regra padrão a TODAS as NF-e pendentes e devolve.

    Pensado para um gatilho (webhook de NF do Bling ou polling agendado). Cada nota
    é editada e enviada de volta. Retorna um relatório por nota.
    """
    from . import bling

    pendentes = bling.listar_nfe(user_id, situacao=cfg.situacao_pendente)
    notas = pendentes.get("data", []) if isinstance(pendentes, dict) else []
    relatorio = []
    aplicadas = 0
    for n in notas:
        nid = n.get("id")
        numero = n.get("numero")
        try:
            r = editar_nota(user_id, nid, desconto_tipo=cfg.desconto_tipo,
                            desconto_valor=cfg.desconto_valor,
                            remover_frete=cfg.remover_frete, enviar=True,
                            desconto_plataformas=getattr(cfg, "desconto_plataformas", None))
            aplicadas += 1
            relatorio.append({"id": nid, "numero": numero, "ok": True,
                              "total_nota": r["resumo"]["total_nota"],
                              "total_desconto": r["resumo"].get("total_desconto")})
        except Exception as e:  # noqa: BLE001
            relatorio.append({"id": nid, "numero": numero, "ok": False, "erro": str(e)})
    return {"processadas": len(relatorio), "aplicadas": aplicadas, "relatorio": relatorio}
