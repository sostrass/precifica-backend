"""Score de qualidade/completude do cadastro — o "Insight Engine" do front.

Dá uma nota 0-100 indicando se a ficha do produto está pronta para publicar,
e devolve, item a item, o que falta e uma dica. Útil para varrer os 5.240 SKUs
e achar anúncios incompletos antes de subir.

Onde o front era leniente (EAN >= 8 chars, NCM >= 4), aqui validamos de verdade:
GTIN com 8/12/13/14 dígitos e NCM com 8 dígitos (exigência de NF-e).
"""


def _digitos(v) -> str:
    return "".join(c for c in str(v or "") if c.isdigit())


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# (chave, rótulo, peso, validador, dica quando falha)
CHECKS_PADRAO = [
    ("nome", "Título com 30+ caracteres", 20,
     lambda p: len((p.get("nome") or "").strip()) >= 30,
     "Capriche no título: 30+ caracteres com material e medida ajudam a indexar."),
    ("ean", "EAN/GTIN válido", 20,
     lambda p: len(_digitos(p.get("ean"))) in (8, 12, 13, 14),
     "Informe um GTIN/EAN com 8, 12, 13 ou 14 dígitos para não travar no marketplace."),
    ("ncm", "NCM faturável (8 dígitos)", 20,
     lambda p: len(_digitos(p.get("ncm"))) == 8,
     "NCM precisa de 8 dígitos para emitir NF-e."),
    ("peso", "Peso logístico informado", 20,
     lambda p: _num(p.get("peso")) > 0,
     "Sem peso, o frete não calcula direito."),
    ("descricao", "Descrição rica (200+ caracteres)", 20,
     lambda p: len(p.get("descricao_longa") or p.get("descricao") or "") >= 200,
     "Descrição detalhada com medidas exatas reduz devolução por expectativa errada."),
]


def score_cadastro(produto: dict, checks=None) -> dict:
    """Retorna {score, completo, itens:[{chave,label,peso,ok,dica}]}."""
    checks = checks or CHECKS_PADRAO
    itens = []
    score = 0
    for chave, label, peso, valida, dica in checks:
        ok = bool(valida(produto or {}))
        if ok:
            score += peso
        itens.append({"chave": chave, "label": label, "peso": peso,
                      "ok": ok, "dica": None if ok else dica})
    return {"score": score, "completo": score >= 100, "itens": itens}
