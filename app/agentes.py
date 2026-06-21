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
from . import ai, bling, decisao, kpis, precificacao, qualidade, radar, scraper
import re
import time


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


# Cache de análise de vendas (ABC + demanda) por usuário — evita refetch a cada conselho.
_CACHE_VENDAS = {}     # user_id -> (timestamp, dias, abc_dict, pedidos)
_TTL_VENDAS = 1800     # 30 min


def analise_vendas(user_id: int, dias: int = 90):
    """Busca pedidos do período (com cache TTL) e devolve (abc_dict, pedidos)."""
    agora = time.time()
    c = _CACHE_VENDAS.get(user_id)
    if c and c[1] == dias and agora - c[0] < _TTL_VENDAS:
        return c[2], c[3]
    try:
        pedidos = bling.listar_pedidos_periodo(user_id, dias=dias, max_paginas=8)
    except Exception:
        pedidos = []
    abc = kpis.curva_abc(pedidos)
    _CACHE_VENDAS[user_id] = (agora, dias, abc, pedidos)
    return abc, pedidos


def conselho(user_id: int, produto_id) -> dict:
    raw = (bling.obter_produto(user_id, produto_id) or {}).get("data", {}) or {}
    nome = raw.get("nome") or ""
    preco = float(raw.get("preco") or 0)
    ncm = (raw.get("tributacao") or {}).get("ncm") or raw.get("ncm") or ""
    gtin = raw.get("gtin") or ""
    peso = raw.get("pesoBruto") or raw.get("pesoLiquido")
    fotos = [i.get("link") for i in (((raw.get("midia") or {}).get("imagens") or {}).get("externas") or []) if i.get("link")]
    descricao = raw.get("descricaoComplementar") or raw.get("descricaoCurta") or ""
    desc_len = len(_texto_puro(descricao))
    est = raw.get("estoque") or {}
    saldo = float(est.get("saldoVirtualTotal") or 0)
    minimo = float(est.get("minimo") or 0)

    saude = qualidade.score_cadastro({
        "nome": nome, "ean": gtin, "ncm": ncm, "peso": peso, "descricao": descricao,
    })
    cfg = precificacao.obter_config(user_id)
    canais = []
    for c in cfg.get("canais", []):
        if not c.get("ativo"):
            continue
        av = precificacao.avaliar_com_cfg(cfg, 0, preco, c["canal"])
        if av.get("preco_sugerido") is not None:
            canais.append({"canal": c["canal"], "nome": c["nome"], **av})

    # canais reais no Bling (preço/ativo por marketplace)
    try:
        vinc = bling.vinculos_multiloja(user_id, produto_id)
    except Exception:
        vinc = []

    plano = []
    diretores = []

    def D(nome_d, papel, icone, subs):
        diretores.append({"nome": nome_d, "papel": papel, "icone": icone, "subagentes": subs})

    # ---- DIRETOR COMERCIAL: Precificação + Posicionamento ----
    sub_com = []
    if canais and preco > 0:
        m = canais[0]
        sub_com.append({"nome": "Precificação", "status": "ok",
                        "texto": f"Base R$ {preco:.2f} (líquido seu). No {m['nome']} o anúncio deve sair a R$ {m['preco_sugerido']:.2f} para preservar o líquido."})
        for c in canais:
            plano.append({"tipo": "preço", "agente": "Comercial", "titulo": f"Aplicar preço no {c['nome']}",
                          "detalhe": f"R$ {c['preco_sugerido']:.2f} · líquido R$ {preco:.2f} · margem {c['margem_sugerida']}%",
                          "acao": {"campos": {"preco": c["preco_sugerido"]}}})
    elif preco <= 0:
        sub_com.append({"nome": "Precificação", "status": "acao",
                        "texto": "Produto sem preço de venda — defina a base para o conselho precificar os canais."})
    sub_com.append({"nome": "Posicionamento", "status": "info",
                    "texto": "Comparação com concorrentes disponível na aba Mercado (busca ao vivo no Mercado Livre)."})
    D("Comercial", "Diretor comercial", "dollar", sub_com)

    # ---- DIRETOR DE CATÁLOGO (QA): Auditor + Compliance ----
    gaps = [it for it in saude.get("itens", []) if not it.get("ok")]
    sub_cat = [{"nome": "Auditor", "status": "alerta" if gaps else "ok",
                "texto": f"Saúde do cadastro {saude.get('score')}/100." + (f" Pendências: {', '.join(it['label'] for it in gaps)}." if gaps else " Cadastro completo.")}]
    comp_falta = [k for k, v in (("EAN/GTIN", gtin), ("NCM", ncm), ("Peso", peso)) if not v]
    sub_cat.append({"nome": "Compliance", "status": "acao" if comp_falta else "ok",
                    "texto": (f"Falta para emitir/anunciar: {', '.join(comp_falta)}." if comp_falta else "Fiscais e dimensões OK (EAN, NCM, peso).")})
    for it in gaps:
        plano.append({"tipo": "cadastro", "agente": "QA", "titulo": it["label"],
                      "detalhe": it.get("dica") or "Complete no cadastro.", "acao": None})
    D("Catálogo", "Diretor de catálogo (QA)", "shield", sub_cat)

    # ---- DIRETOR DE MÍDIA ----
    sub_mid = [{"nome": "Imagens", "status": "acao" if not fotos else "ok",
                "texto": "Sem fotos cadastradas — prioridade alta." if not fotos else f"{len(fotos)} foto(s). Revise a capa: fundo limpo e produto centralizado."}]
    if not fotos:
        plano.append({"tipo": "mídia", "agente": "Mídia", "titulo": "Adicionar fotos", "detalhe": "Nenhuma imagem no produto.", "acao": None})
    D("Mídia", "Diretor de mídia", "image", sub_mid)

    # ---- DIRETOR DE CONTEÚDO: Copywriter + SEO ----
    sub_con = []
    if desc_len < 200:
        sub_con.append({"nome": "Copywriter", "status": "acao", "texto": f"Descrição com {desc_len} caracteres — curta para conversão."})
        plano.append({"tipo": "descrição", "agente": "Conteúdo", "titulo": "Reescrever descrição com IA",
                      "detalhe": "Otimizar palavras-chave e leitura.", "acao": {"ia_campo": "descrição complementar"}})
    else:
        sub_con.append({"nome": "Copywriter", "status": "ok", "texto": f"Descrição com {desc_len} caracteres — boa base."})
    titulo_len = len(nome)
    if titulo_len < 40 or titulo_len > 130:
        sub_con.append({"nome": "SEO", "status": "alerta", "texto": f"Título com {titulo_len} caracteres — ideal 40–130 com material e medida para busca."})
        plano.append({"tipo": "título", "agente": "Conteúdo", "titulo": "Recriar título com IA",
                      "detalhe": "Incluir material, medida e termos de busca.", "acao": {"ia_campo": "título do anúncio"}})
    else:
        sub_con.append({"nome": "SEO", "status": "ok", "texto": f"Título com {titulo_len} caracteres — bom tamanho para busca."})
    D("Conteúdo", "Diretor de conteúdo", "file", sub_con)

    # ---- DIRETOR DE MARKETPLACE: Cobertura + Saúde + Sync + Divergências Bling×marketplace ----
    sub_mkt = []
    if vinc:
        ativos = [v for v in vinc if v.get("ativo")]
        sub_mkt.append({"nome": "Cobertura", "status": "ok" if ativos else "alerta",
                        "texto": f"Ativo em {len(ativos)} de {len(vinc)} canal(is): {', '.join(v['nome'] for v in ativos) or '—'}."})
        precos = {v["canal"]: v["preco"] for v in vinc if v.get("canal") and v["preco"] > 0}
        divs = precificacao.divergencias(cfg, preco, precos)
        divergentes = [d for d in divs if d["status"] == "divergente"]
        prejuizo = [v for v in vinc if v["preco"] > 0 and preco > 0 and v["preco"] < preco]

        # Saúde dos canais: % saudáveis (ativo + preço ≥ líquido + alinhado ao alvo)
        canal_por_nome = {d["canal"]: d for d in divs}
        saudaveis = 0
        for v in vinc:
            ok_preco = v["preco"] >= preco if preco > 0 else True
            d = canal_por_nome.get(v.get("canal"))
            ok_sync = (d is None) or d["status"] != "divergente"
            if v.get("ativo") and ok_preco and ok_sync:
                saudaveis += 1
        pct_saude = round(saudaveis / len(vinc) * 100) if vinc else 0
        st_saude = "ok" if pct_saude >= 80 else ("alerta" if pct_saude >= 50 else "acao")
        sub_mkt.append({"nome": "Saúde dos canais", "status": st_saude,
                        "texto": f"{saudaveis}/{len(vinc)} canais saudáveis ({pct_saude}%)."
                                 + (f" Problemas: {', '.join(v['nome'] for v in vinc if not (v.get('ativo') and (v['preco'] >= preco if preco>0 else True)))[:120]}." if pct_saude < 100 else "")})

        if prejuizo:
            sub_mkt.append({"nome": "Sync", "status": "acao",
                            "texto": f"PREJUÍZO em {', '.join(v['nome'] for v in prejuizo)} (preço abaixo do líquido R$ {preco:.2f})."})
        elif divergentes:
            sub_mkt.append({"nome": "Sync", "status": "alerta",
                            "texto": f"Preço divergente em {', '.join(d['nome'] for d in divergentes)} — corrigir para o alvo."})
        else:
            sub_mkt.append({"nome": "Sync", "status": "ok", "texto": "Preços por canal alinhados ao alvo."})

        # Divergências Bling × marketplace: compara o preço do Bling com o preço AO VIVO no canal
        checados, divergencias_live = 0, []
        for v in vinc:
            ann = v.get("id_anuncio")
            if v.get("integracao", "").lower() == "mercadolivre" and ann and str(ann).upper().startswith("MLB"):
                live = scraper.preco_ml_por_id(ann)
                if live and live.get("preco") is not None:
                    checados += 1
                    if abs(live["preco"] - v["preco"]) > 0.01:
                        divergencias_live.append(f"{v['nome']}: Bling R$ {v['preco']:.2f} × ML R$ {live['preco']:.2f}")
        if checados:
            sub_mkt.append({"nome": "Bling × marketplace", "status": "acao" if divergencias_live else "ok",
                            "texto": ("Desalinhado — " + "; ".join(divergencias_live)) if divergencias_live
                                     else f"Preço do Bling bate com o anúncio ao vivo ({checados} verificado(s))."})
        else:
            sub_mkt.append({"nome": "Bling × marketplace", "status": "info",
                            "texto": "Comparação ao vivo precisa do ID do anúncio (leitura completa por canal / Mercado Livre)."})
    else:
        sub_mkt.append({"nome": "Cobertura", "status": "info",
                        "texto": "Sem leitura de canais ainda — rode 'Testar leitura por canal' na aba Sincronizar."})
    D("Marketplace", "Diretor de marketplace", "target", sub_mkt)

    # ---- DIRETOR DE OPERAÇÕES: Curva ABC + Demanda + Ruptura (histórico de vendas) ----
    sub_ops = []
    sku = raw.get("codigo")
    abc, pedidos = analise_vendas(user_id)
    cls = abc.get(str(sku)) if sku else None
    if cls:
        rotulo = {"A": "Curva A — top de faturamento", "B": "Curva B — intermediário", "C": "Curva C — cauda longa"}[cls["classe"]]
        st_abc = "ok" if cls["classe"] == "A" else ("info" if cls["classe"] == "B" else "alerta")
        sub_ops.append({"nome": "Curva ABC", "status": st_abc,
                        "texto": f"{rotulo}. #{cls['posicao']} de {cls['total_skus']} · {cls['pct']}% da receita (90 dias)."})
        dem = kpis.analise_demanda(pedidos, sku, saldo, 90)
        if dem["por_dia"] > 0:
            cob = dem["cobertura_dias"]
            st_dem = "acao" if (cob is not None and cob < 15) else ("alerta" if (cob is not None and cob < 30) else "ok")
            sub_ops.append({"nome": "Demanda", "status": st_dem,
                            "texto": f"{dem['unidades']:.0f} un em 90 dias ({dem['por_dia']}/dia). "
                                     + (f"Cobertura {cob} dias com o estoque atual." if cob is not None else "Estoque cobre o ritmo.")})
        else:
            sub_ops.append({"nome": "Demanda", "status": "alerta",
                            "texto": "Sem vendas nos últimos 90 dias — produto parado (capital empatado)."})
    elif pedidos:
        sub_ops.append({"nome": "Curva ABC", "status": "alerta",
                        "texto": "Sem vendas no período — fora da curva (não faturou nos últimos 90 dias)."})
    else:
        sub_ops.append({"nome": "Curva ABC", "status": "info",
                        "texto": "Sem histórico de pedidos lido (verifique a conexão do Bling)."})

    if minimo > 0 and saldo <= minimo:
        sub_ops.append({"nome": "Ruptura", "status": "acao", "texto": f"Estoque {saldo:.0f} ≤ mínimo {minimo:.0f} — reponha."})
        plano.append({"tipo": "estoque", "agente": "Operações", "titulo": "Repor estoque", "detalhe": f"Saldo {saldo:.0f} / mínimo {minimo:.0f}.", "acao": None})
    elif saldo <= 0:
        sub_ops.append({"nome": "Ruptura", "status": "alerta", "texto": "Sem saldo em estoque."})
    else:
        sub_ops.append({"nome": "Ruptura", "status": "ok", "texto": f"Estoque {saldo:.0f} un — sem risco imediato."})
    D("Operações", "Diretor de operações", "boxes", sub_ops)

    # ---- GERENTE GERAL consolida ----
    criticos = sum(1 for d in diretores for s in d["subagentes"] if s["status"] == "acao")
    resumo = (f"{len(plano)} ação(ões) priorizada(s); {criticos} crítica(s)." if plano
              else "Produto saudável — nada crítico agora.")
    gerente = {"nome": "Gerente Geral", "papel": "Consolidação", "icone": "crown",
               "subagentes": [{"nome": "Síntese", "status": "acao" if criticos else "ok", "texto": resumo}]}

    # falas planas (compatibilidade)
    falas = []
    for d in diretores + [gerente]:
        for s in d["subagentes"]:
            falas.append({"agente": d["nome"], "papel": d["papel"], "sub": s["nome"], "texto": s["texto"]})

    return {"produto": {"id": raw.get("id"), "nome": nome, "sku": raw.get("codigo")},
            "saude": saude.get("score"), "diretores": diretores + [gerente],
            "falas": falas, "plano": plano}
