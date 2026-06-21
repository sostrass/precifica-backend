"""Camada de agentes I.A. — o LLM decide e orquestra; as ferramentas executam.

Regra de ouro (do AGENTES.md):
- Nenhum agente calcula dinheiro/fisco "de cabeça": ele chama as ferramentas
  determinísticas (precificacao, decisao, radar, qualidade, ai), que fazem a conta.
- Nenhum agente altera nada ao vivo (preço no canal, nota no Bling): os agentes
  PROPÕEM, o lojista aplica. Não há ferramenta destrutiva aqui — por segurança.

Usa function-calling automático do Gemini: as funções abaixo são expostas ao modelo;
quando ele decide usar uma, o SDK executa e devolve o resultado pro modelo.
"""

from fastapi import HTTPException

from .config import settings
from . import ai, bling, decisao, precificacao, qualidade, radar
import re


AGENTES = {
    "conteudo": {
        "nome": "Conteúdo",
        "descricao": "Cria descrições que vendem e reduzem devolução, e avalia a qualidade do cadastro.",
        "ferramentas": ["gerar_descricao", "score_cadastro"],
        "persona": (
            "Você é o agente de Conteúdo da Sóstrass Acessórios e Pedrarias, especialista em "
            "copy de e-commerce para armarinho (miçangas, pérolas, strass, caixas organizadoras). "
            "Seu trabalho: criar descrições que vendem e reduzem devolução, e avaliar a qualidade "
            "do cadastro. SEMPRE use a ferramenta gerar_descricao para escrever descrições (não "
            "escreva na mão) e score_cadastro para avaliar fichas. Nunca invente medidas, EAN ou "
            "NCM — se faltar, peça ao lojista. Responda em português, tom prático e parceiro."
        ),
    },
    "atendimento": {
        "nome": "Atendimento",
        "descricao": "Responde clientes no tom da Sóstrass, com empatia e autoridade técnica.",
        "ferramentas": ["gerar_sac"],
        "persona": (
            "Você é o agente de Atendimento da Sóstrass. Ajuda o lojista a responder clientes com "
            "empatia e autoridade técnica em artesanato. Use a ferramenta gerar_sac para redigir as "
            "respostas ao cliente, mantendo o tom acolhedor e resolutivo da Sóstrass. Se precisar de "
            "contexto (qual produto, qual o problema), pergunte antes de responder."
        ),
    },
    "comercial": {
        "nome": "Comercial",
        "descricao": "Estrategista de preço e concorrência. Propõe, nunca aplica sozinho.",
        "ferramentas": ["precificar", "decidir_preco", "radar_recomendar", "radar_historico"],
        "persona": (
            "Você é o agente Comercial da Sóstrass, estrategista de preço e concorrência. Você PROPÕE, "
            "nunca aplica nada sozinho. Para QUALQUER número de preço, margem ou taxa, use as "
            "ferramentas (precificar, decidir_preco, radar_recomendar, radar_historico) — nunca calcule "
            "de cabeça. Sempre respeite o piso de viabilidade que as ferramentas retornam e deixe a "
            "decisão final com o lojista. Explique a recomendação de forma clara e curta."
        ),
    },
    "gerente": {
        "nome": "Gerente",
        "descricao": "Coordena conteúdo, atendimento e comercial e resolve pedidos gerais.",
        "ferramentas": ["gerar_descricao", "score_cadastro", "gerar_sac",
                        "precificar", "decidir_preco", "radar_recomendar", "radar_historico"],
        "persona": (
            "Você é o Gerente da Sóstrass AI, que coordena conteúdo, atendimento e comercial. Entenda "
            "o que o lojista precisa e use as ferramentas certas para resolver. Para números, use "
            "SEMPRE as ferramentas determinísticas (nunca calcule de cabeça). Você PROPÕE, nunca aplica "
            "mudanças ao vivo: se algo exigir aplicar preço no canal ou editar nota fiscal, oriente o "
            "lojista a confirmar manualmente na tela correspondente. Responda em português, direto."
        ),
    },
}


def _ferramentas(user_id: int, registro: list) -> dict:
    """Funções-ferramenta com o user_id embutido; `registro` coleta os nomes chamados.

    As docstrings e os type hints viram o schema que o Gemini enxerga — por isso são
    descritivas. Tudo aqui é leitura/proposta: nada altera Bling nem aplica preço.
    """

    def gerar_descricao(nome_produto: str, caracteristicas: str = "", blindar: bool = False) -> str:
        """Gera uma descrição comercial do produto que reduz devolução. Use blindar=True para anexar o rodapé de blindagem jurídica."""
        registro.append("gerar_descricao")
        return ai.gerar_descricao(user_id, nome_produto, caracteristicas, blindar=blindar)

    def score_cadastro(nome: str = "", ean: str = "", ncm: str = "", peso: float = 0.0, descricao: str = "") -> dict:
        """Avalia de 0 a 100 a completude do cadastro do produto e aponta exatamente o que falta."""
        registro.append("score_cadastro")
        return qualidade.score_cadastro({"nome": nome, "ean": ean, "ncm": ncm, "peso": peso, "descricao": descricao})

    def gerar_sac(relato: str) -> str:
        """Escreve uma resposta de atendimento ao cliente no tom da Sóstrass a partir do relato/mensagem do cliente."""
        registro.append("gerar_sac")
        return ai.gerar_sac(user_id, relato)

    def precificar(custo: float, margem: float = 20.0) -> dict:
        """Calcula o preço de venda sugerido por canal a partir do custo, usando as taxas por faixa configuradas. Retorna preço e detalhamento (Raio-X) por marketplace."""
        registro.append("precificar")
        return precificacao.precificar(user_id, custo, margem)

    def decidir_preco(custo: float, preco_atual: float, precos_concorrentes: list[float],
                      canal: str = "mercadolivre", piso_margem: float = 15.0) -> dict:
        """Decide o que fazer com o preço diante dos concorrentes (baixar/segurar/manter), sempre travado no piso de viabilidade."""
        registro.append("decidir_preco")
        cfg = precificacao.obter_config(user_id)
        return decisao.decidir_preco(custo_base=custo, preco_atual=preco_atual,
                                     precos_concorrentes=precos_concorrentes, canal=canal,
                                     imposto=cfg["imposto"], cartao=cfg["cartao"],
                                     piso_margem=piso_margem)

    def radar_recomendar(sku: str, custo: float, preco_atual: float,
                         canal: str = "mercadolivre", piso_margem: float = 15.0) -> dict:
        """Recomenda preço por concorrente para um SKU, usando os últimos preços capturados pelo radar e o piso de viabilidade."""
        registro.append("radar_recomendar")
        cfg = precificacao.obter_config(user_id)
        return radar.recomendar(user_id, sku, custo_base=custo, preco_atual=preco_atual,
                                canal=canal, imposto=cfg["imposto"], cartao=cfg["cartao"],
                                piso_margem=piso_margem)

    def radar_historico(sku: str, dias: int = 7) -> dict:
        """Retorna o histórico de preços dos concorrentes de um SKU e as estatísticas do período (menor/maior/média)."""
        registro.append("radar_historico")
        return radar.historico(user_id, sku, dias)

    return {
        "gerar_descricao": gerar_descricao, "score_cadastro": score_cadastro,
        "gerar_sac": gerar_sac, "precificar": precificar, "decidir_preco": decidir_preco,
        "radar_recomendar": radar_recomendar, "radar_historico": radar_historico,
    }


def listar() -> list:
    return [{"id": k, "nome": v["nome"], "descricao": v["descricao"],
             "ferramentas": v["ferramentas"]} for k, v in AGENTES.items()]


def conversar(user_id: int, agente_id: str, mensagem: str, historico=None) -> dict:
    """Roda um turno do agente. `historico` = [{autor:'user'|'agente', texto}]."""
    cfg = AGENTES.get(agente_id)
    if not cfg:
        raise HTTPException(status_code=404, detail="Agente não encontrado.")
    if not (mensagem or "").strip():
        raise HTTPException(status_code=422, detail="Mensagem vazia.")
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY não configurada.")
    ai._checar_e_incrementar_cota(user_id)

    import google.generativeai as genai  # import tardio

    genai.configure(api_key=settings.gemini_api_key)
    usadas: list = []
    todas = _ferramentas(user_id, usadas)
    tools = [todas[n] for n in cfg["ferramentas"]]

    model = genai.GenerativeModel(settings.gemini_model, tools=tools,
                                  system_instruction=cfg["persona"])
    hist = []
    for h in (historico or []):
        role = "user" if h.get("autor") == "user" else "model"
        texto = (h.get("texto") or "").strip()
        if texto:
            hist.append({"role": role, "parts": [texto]})

    chat = model.start_chat(history=hist, enable_automatic_function_calling=True)
    try:
        resp = chat.send_message(mensagem)
        texto = (resp.text or "").strip()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Falha no agente: {e}")

    return {"agente": agente_id, "resposta": texto,
            "ferramentas_usadas": list(dict.fromkeys(usadas))}


# --------------------------------------------------------------------------- #
# CONSELHO DE IA — diretoria que delibera sobre um produto e devolve um plano.
# Os achados são FUNDAMENTADOS em dados reais (motor de preço, saúde do
# cadastro, mídia) — nada de número "de cabeça". A IA (Gemini) entra na hora
# de gerar conteúdo, via /api/ia/campo, quando você aplica uma melhoria.
# Nenhuma ação é executada aqui; o conselho PROPÕE, o usuário aplica.
# --------------------------------------------------------------------------- #
def _texto_puro(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()


def conselho(user_id: int, produto_id) -> dict:
    raw = (bling.obter_produto(user_id, produto_id) or {}).get("data", {}) or {}
    nome = raw.get("nome") or ""
    preco = float(raw.get("preco") or 0)
    ncm = (raw.get("tributacao") or {}).get("ncm") or raw.get("ncm") or ""
    fotos = [i.get("link") for i in (((raw.get("midia") or {}).get("imagens") or {}).get("externas") or []) if i.get("link")]
    descricao = raw.get("descricaoComplementar") or raw.get("descricaoCurta") or ""
    desc_len = len(_texto_puro(descricao))

    saude = qualidade.score_cadastro({
        "nome": nome, "ean": raw.get("gtin"), "ncm": ncm,
        "peso": raw.get("pesoBruto") or raw.get("pesoLiquido"),
        "descricao": descricao,
    })
    cfg = precificacao.obter_config(user_id)
    canais = []
    for c in cfg.get("canais", []):
        if not c.get("ativo"):
            continue
        av = precificacao.avaliar_com_cfg(cfg, 0, preco, c["canal"])
        if av.get("preco_sugerido") is not None:
            canais.append({"canal": c["canal"], "nome": c["nome"], **av})

    falas, plano = [], []

    # Diretor comercial — preço base-venda por canal
    if canais and preco > 0:
        m = canais[0]
        falas.append({"agente": "Comercial", "papel": "Diretor comercial",
                      "texto": f"Base de venda R$ {preco:.2f} (líquido seu). No {m['nome']} o anúncio deve sair a "
                               f"R$ {m['preco_sugerido']:.2f} para preservar esse líquido."})
        for c in canais:
            plano.append({"tipo": "preço", "agente": "Comercial",
                          "titulo": f"Aplicar preço no {c['nome']}",
                          "detalhe": f"R$ {c['preco_sugerido']:.2f} · líquido R$ {preco:.2f} · margem {c['margem_sugerida']}%",
                          "acao": {"campos": {"preco": c["preco_sugerido"]}}})
    elif preco <= 0:
        falas.append({"agente": "Comercial", "papel": "Diretor comercial",
                      "texto": "Produto sem preço de venda definido — defina a base para o conselho precificar os canais."})

    # Diretor de catálogo (QA) — saúde do cadastro
    gaps = [it for it in saude.get("itens", []) if not it.get("ok")]
    if gaps:
        falas.append({"agente": "QA", "papel": "Diretor de catálogo",
                      "texto": f"Saúde do cadastro {saude.get('score')}/100. Pendências: "
                               + ", ".join(it["label"] for it in gaps) + "."})
        for it in gaps:
            plano.append({"tipo": "cadastro", "agente": "QA", "titulo": it["label"],
                          "detalhe": it.get("dica") or "Complete no cadastro.", "acao": None})
    else:
        falas.append({"agente": "QA", "papel": "Diretor de catálogo",
                      "texto": f"Cadastro completo ({saude.get('score')}/100)."})

    # Diretor de mídia
    if not fotos:
        falas.append({"agente": "Mídia", "papel": "Diretor de mídia", "texto": "Sem fotos cadastradas — prioridade alta."})
        plano.append({"tipo": "mídia", "agente": "Mídia", "titulo": "Adicionar fotos",
                      "detalhe": "Nenhuma imagem no produto.", "acao": None})
    else:
        falas.append({"agente": "Mídia", "papel": "Diretor de mídia",
                      "texto": f"{len(fotos)} foto(s). Revise a capa para fundo limpo e padrão."})

    # Diretor de conteúdo
    if desc_len < 200:
        falas.append({"agente": "Conteúdo", "papel": "Diretor de conteúdo",
                      "texto": f"Descrição com {desc_len} caracteres — reescrever para busca e conversão."})
        plano.append({"tipo": "descrição", "agente": "Conteúdo", "titulo": "Reescrever descrição com IA",
                      "detalhe": "Otimizar palavras-chave e leitura.", "acao": {"ia_campo": "descrição complementar"}})
    else:
        falas.append({"agente": "Conteúdo", "papel": "Diretor de conteúdo",
                      "texto": f"Descrição com {desc_len} caracteres — boa base; dá para enriquecer com palavras-chave."})

    # Gerente geral — consolida
    falas.append({"agente": "Gerente", "papel": "Gerente geral",
                  "texto": f"Consolidado: {len(plano)} melhoria(s) priorizada(s)." if plano
                           else "Produto saudável — nenhuma ação necessária agora."})

    return {"produto": {"id": raw.get("id"), "nome": nome, "sku": raw.get("codigo")},
            "saude": saude.get("score"), "falas": falas, "plano": plano}
