from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import ai, agentes, auth, bling, decisao, nfe, precificacao, pricing, qualidade, radar, scraper
from .config import settings
from .db import init_db, SessionLocal
from .models import NfeConfig, User


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BlingAI Manager — Backend", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.frontend_origin == "*" else [settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------- Modelos ---------------------------------- #
class RegistroIn(BaseModel):
    email: str
    senha: str
    nome: str | None = None


class LoginIn(BaseModel):
    email: str
    senha: str


@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------- Auth ------------------------------------- #
@app.post("/auth/register")
def register(body: RegistroIn):
    user = auth.registrar(body.email, body.senha, body.nome)
    return {"id": user.id, "email": user.email, "token": auth.create_access_token(user.id)}


@app.post("/auth/login")
def login(body: LoginIn):
    user = auth.autenticar(body.email, body.senha)
    return {"id": user.id, "email": user.email, "token": auth.create_access_token(user.id)}


@app.get("/auth/me")
def me(user: User = Depends(auth.get_current_user)):
    return {"id": user.id, "email": user.email, "nome": user.nome}


# --------------------------- OAuth Bling (por tenant) --------------------- #
@app.get("/auth/bling/login")
def bling_login(user: User = Depends(auth.get_current_user)):
    # Retorna a URL para o front redirecionar (o user_id viaja assinado no state).
    return {"url": bling.get_authorize_url(user.id)}


@app.get("/auth/bling/callback")
def bling_callback(code: str = Query(...), state: str = Query(...)):
    try:
        uid = bling.user_id_from_state(state)
        bling.exchange_code(uid, code)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mensagem": "Conta Bling autorizada com sucesso."}


@app.get("/auth/bling/status")
def bling_status(user: User = Depends(auth.get_current_user)):
    return bling.token_status(user.id)


# ------------------------------- Produtos --------------------------------- #
@app.get("/api/produtos")
def listar_produtos(pagina: int = 1, limite: int = 100,
                    user: User = Depends(auth.get_current_user)):
    try:
        return bling.listar_produtos(user.id, pagina=pagina, limite=limite)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/produtos/{produto_id}")
def obter_produto(produto_id: int, user: User = Depends(auth.get_current_user)):
    try:
        return bling.obter_produto(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ------------------------------ Precificação ------------------------------ #
@app.post("/api/precificar")
def precificar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {produto, custos_globais, taxas_por_canal?}"""
    return pricing.precificar(
        payload.get("produto", {}),
        payload.get("custos_globais", {}),
        payload.get("taxas_por_canal", {}),
    )


@app.post("/api/precificar/lote")
def precificar_lote(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Precificação em massa.

    Body: {
      custos_globais, taxas_por_canal?, canal: 'mercadolivre'|'shopee',
      itens: [{produto_id, custo, embalagem?, frete?}],
      aplicar: bool   # se true, grava no Bling
    }
    """
    canal = payload.get("canal", "mercadolivre")
    aplicar = bool(payload.get("aplicar", False))
    cfg = precificacao.obter_config(user.id)
    resultados = []
    for item in payload.get("itens", []):
        av = precificacao.avaliar_com_cfg(cfg, float(item.get("custo", 0) or 0), 0, canal)
        preco = av["preco_sugerido"]
        linha = {
            "produto_id": item.get("produto_id"),
            "preco": preco,
            "margem_liquida": av["margem_sugerida"],
            "aplicado": False,
        }
        if aplicar and preco and item.get("produto_id"):
            try:
                bling.atualizar_preco(user.id, int(item["produto_id"]), preco)
                linha["aplicado"] = True
            except Exception as e:  # noqa: BLE001 — registra falha por item, segue o lote
                linha["erro"] = str(e)
        resultados.append(linha)
    return {"canal": canal, "aplicado": aplicar, "itens": resultados}


# ------------------------ Monitoramento (bolsa de valores) ---------------- #
def _status(margem: float) -> str:
    if margem >= 30:
        return "lucro_ideal"
    if margem >= 15:
        return "atencao"
    return "critico"


@app.post("/api/monitoramento")
def monitoramento(payload: dict = Body(default={}),
                  user: User = Depends(auth.get_current_user)):
    """Lista produtos do Bling com margem líquida e status tipado por canal.

    Body opcional: {custos_globais, taxas_por_canal?, canal, pagina, limite}
    O front pinta os badges a partir do 'status' (enum), não de texto.
    """
    canal = payload.get("canal", "mercadolivre")
    try:
        bruto = bling.listar_produtos(user.id, pagina=payload.get("pagina", 1),
                                      limite=payload.get("limite", 100))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    cfg = precificacao.obter_config(user.id)
    itens = []
    for p in bruto.get("data", []):
        custo = float(p.get("precoCusto") or 0)
        preco_atual = float(p.get("preco") or 0)
        av = precificacao.avaliar_com_cfg(cfg, custo, preco_atual, canal)
        margem = av["margem_atual"] if av["margem_atual"] is not None else (av["margem_sugerida"] or 0)
        itens.append({
            "id": p.get("id"),
            "sku": p.get("codigo"),
            "nome": p.get("nome"),
            "custo": custo,
            "preco_atual": preco_atual,
            "preco_sugerido": av["preco_sugerido"],
            "margem_liquida": margem,
            "status": _status(margem),
        })
    return {"canal": canal, "itens": itens}


# ------------------------------ Concorrência ------------------------------ #
@app.post("/api/concorrencia/preco")
def concorrencia_preco(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Busca o preço do concorrente e simula o impacto na SUA margem real.

    Body: {url, seletor?, custo_base, canal, taxas_por_canal?, imposto?, cartao?}
    """
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=422, detail="Informe 'url'.")
    achado = scraper.buscar_preco(url, payload.get("seletor"))
    preco = achado.get("preco")
    resposta = {"preco_concorrente": preco, "fonte": achado.get("fonte")}
    if achado.get("erro"):
        resposta["erro"] = achado["erro"]

    if preco:
        canal = payload.get("canal", "mercadolivre")
        cfg = pricing.PLATAFORMAS.get(canal, {})
        taxas = (payload.get("taxas_por_canal") or {}).get(canal, {})
        sim = pricing.simular_concorrente(
            preco,
            float(payload.get("custo_base", 0)),
            taxas.get("comissao", cfg.get("comissao", 0)),
            taxas.get("fixo", cfg.get("fixo", 0)),
            float(payload.get("imposto", 0)),
            float(payload.get("cartao", 0)),
        )
        resposta["simulacao"] = sim
    return resposta


@app.post("/api/concorrencia/precos")
def concorrencia_precos(payload: dict = Body(...),
                        user: User = Depends(auth.get_current_user)):
    """Radar multi-concorrentes: lista de URLs -> preço + margem real de cada um.

    Body: {urls: [..], custo_base, canal, taxas_por_canal?, imposto?, cartao?}
    """
    urls = payload.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise HTTPException(status_code=422, detail="Informe uma lista 'urls'.")
    canal = payload.get("canal", "mercadolivre")
    cfg = pricing.PLATAFORMAS.get(canal, {})
    taxas = (payload.get("taxas_por_canal") or {}).get(canal, {})
    base = float(payload.get("custo_base", 0))
    imp = float(payload.get("imposto", 0))
    cartao = float(payload.get("cartao", 0))

    resultados = []
    for achado in scraper.buscar_precos(urls):
        linha = {"url": achado.get("url"), "preco_concorrente": achado.get("preco"),
                 "fonte": achado.get("fonte")}
        if achado.get("erro"):
            linha["erro"] = achado["erro"]
        if achado.get("preco"):
            linha["simulacao"] = pricing.simular_concorrente(
                achado["preco"], base,
                taxas.get("comissao", cfg.get("comissao", 0)),
                taxas.get("fixo", cfg.get("fixo", 0)), imp, cartao)
        resultados.append(linha)
    return {"canal": canal, "resultados": resultados}


@app.post("/api/decisao/preco")
def decisao_preco(payload: dict = Body(...),
                  user: User = Depends(auth.get_current_user)):
    """Motor de decisão (Fase 4): dado o preço dos concorrentes + custo + piso de
    margem, decide subir/baixar/manter — nunca abaixo da viabilidade.

    Body: {custo_base, preco_atual, precos_concorrentes:[..], canal, comissao?, fixo?,
           imposto?, cartao?, piso_margem?, estrategia?, delta?, delta_tipo?, passo_min?}
    """
    try:
        return decisao.decidir_preco(
            custo_base=float(payload.get("custo_base", 0)),
            preco_atual=float(payload.get("preco_atual", 0)),
            precos_concorrentes=payload.get("precos_concorrentes") or [],
            canal=payload.get("canal", "mercadolivre"),
            comissao=payload.get("comissao"),
            fixo=payload.get("fixo"),
            imposto=float(payload.get("imposto", 0)),
            cartao=float(payload.get("cartao", 0)),
            piso_margem=float(payload.get("piso_margem", 15)),
            estrategia=payload.get("estrategia", "match"),
            delta=float(payload.get("delta", 1)),
            delta_tipo=payload.get("delta_tipo", "pct"),
            passo_min=float(payload.get("passo_min", 0.5)),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Parâmetros inválidos: {e}")


@app.post("/api/precificar/reverso")
def precificar_reverso(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Markup reverso por canal: dado custo + margem alvo, devolve o preço de venda
    em cada marketplace e o Raio-X em R$ (taxa do canal, impostos/cartão, lucro).

    Body: {custo_base, imposto?, cartao?, margem_alvo?, taxas_por_canal?}
    """
    try:
        return pricing.precificar_reverso(
            custo_base=float(payload.get("custo_base", 0)),
            imposto_pct=float(payload.get("imposto", 0)),
            cartao_pct=float(payload.get("cartao", 0)),
            margem_alvo=float(payload.get("margem_alvo", 30)),
            taxas_por_canal=payload.get("taxas_por_canal"),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Parâmetros inválidos: {e}")


@app.post("/api/qualidade/cadastro")
def qualidade_cadastro(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Score 0-100 de completude da ficha (Insight Engine).

    Body: {nome, ean, ncm, peso, descricao|descricao_longa}
    """
    return qualidade.score_cadastro(payload)


# --------------------------------- IA ------------------------------------- #
@app.post("/api/ia/descricao")
def ia_descricao(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    nome = payload.get("nome_produto")
    if not nome:
        raise HTTPException(status_code=422, detail="Informe 'nome_produto'.")
    texto = ai.gerar_descricao(user.id, nome, payload.get("caracteristicas", ""),
                               blindar=bool(payload.get("blindar", False)),
                               modelo=payload.get("model"))
    return {"descricao_gerada": texto}


@app.post("/api/ia/sac")
def ia_sac(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Resposta de atendimento humanizada (tom Sóstrass)."""
    texto = ai.gerar_sac(user.id, payload.get("relato", ""), modelo=payload.get("model"))
    return {"resposta": texto}


@app.post("/api/estudio/imagem")
def estudio_imagem(payload: dict = Body(...),
                   user: User = Depends(auth.get_current_user)):
    """Estúdio criativo: gera foto de produto (Gemini). Devolve imagem em base64.

    Vídeo (Veo) não tem endpoint aqui — é assíncrono/caro e fica para um fluxo próprio.
    """
    return ai.gerar_imagem(user.id, payload.get("prompt", ""),
                           payload.get("negativo", ""), payload.get("modelo"))


# ------------------------------- Agentes ---------------------------------- #
@app.get("/api/agentes")
def agentes_listar(user: User = Depends(auth.get_current_user)):
    """Lista os agentes disponíveis e as ferramentas de cada um."""
    return {"agentes": agentes.listar()}


@app.post("/api/agentes/{agente}/mensagem")
def agentes_mensagem(agente: str, payload: dict = Body(...),
                     user: User = Depends(auth.get_current_user)):
    """Conversa com um agente. Body: {mensagem, historico?:[{autor,texto}]}.

    Os agentes propõem e calculam via ferramentas determinísticas — não alteram
    preço no canal nem nota no Bling.
    """
    return agentes.conversar(user.id, agente, payload.get("mensagem", ""),
                             historico=payload.get("historico"))


# ================= Precificação por canal (faixas de preço) =============== #
@app.get("/api/precificacao/config")
def precificacao_get_config(user: User = Depends(auth.get_current_user)):
    """Custos globais + taxas por canal com faixas (cria padrão na primeira vez)."""
    return precificacao.obter_config(user.id)


@app.put("/api/precificacao/config")
def precificacao_put_config(payload: dict = Body(...),
                            user: User = Depends(auth.get_current_user)):
    """Salva custos globais e/ou canais. Body: {imposto?, cartao?, embalagem?, frete?,
    margem_padrao?, canais?:[{canal,nome,ativo,faixas:[{ate,comissao,fixo,fixo_pct}]}]}"""
    return precificacao.salvar_config(user.id, payload)


@app.post("/api/precificacao/restaurar")
def precificacao_restaurar(user: User = Depends(auth.get_current_user)):
    """Restaura os padrões pesquisados (sobrescreve a config atual)."""
    return precificacao.restaurar_padrao(user.id)


@app.post("/api/precificacao/calcular")
def precificacao_calcular(payload: dict = Body(...),
                          user: User = Depends(auth.get_current_user)):
    """Preço sugerido por canal a partir do custo. Body: {custo, margem?, apenas_ativos?}"""
    if payload.get("custo") is None:
        raise HTTPException(status_code=422, detail="Informe 'custo'.")
    return precificacao.precificar(user.id, float(payload["custo"]),
                                   margem=payload.get("margem"),
                                   apenas_ativos=payload.get("apenas_ativos", True))


# ======================= Radar (histórico de mercado) ===================== #
@app.post("/api/radar/alvos")
def radar_add_alvo(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Cadastra um anúncio de concorrente para monitorar. Body: {sku, url, nome?, marketplace?}"""
    sku = payload.get("sku")
    url = payload.get("url")
    if not sku or not url:
        raise HTTPException(status_code=422, detail="Informe 'sku' e 'url'.")
    return radar.adicionar_alvo(user.id, sku, url, payload.get("nome"),
                                payload.get("marketplace"))


@app.get("/api/radar/alvos")
def radar_list_alvos(sku: str | None = None, user: User = Depends(auth.get_current_user)):
    return {"alvos": radar.listar_alvos(user.id, sku)}


@app.delete("/api/radar/alvos/{alvo_id}")
def radar_del_alvo(alvo_id: int, user: User = Depends(auth.get_current_user)):
    if not radar.remover_alvo(user.id, alvo_id):
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    return {"removido": True}


@app.post("/api/radar/snapshot")
def radar_snapshot(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Registra manualmente uma foto de preço. Body: {alvo_id, preco_oferta?, preco_normal?}"""
    alvo_id = payload.get("alvo_id")
    if alvo_id is None:
        raise HTTPException(status_code=422, detail="Informe 'alvo_id'.")
    r = radar.registrar_snapshot(user.id, int(alvo_id),
                                 preco_oferta=payload.get("preco_oferta"),
                                 preco_normal=payload.get("preco_normal"))
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("erro"))
    return r


@app.post("/api/radar/varrer")
def radar_varrer(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Roda o scraper em todos os alvos do SKU e guarda os snapshots. Body: {sku}"""
    sku = payload.get("sku")
    if not sku:
        raise HTTPException(status_code=422, detail="Informe 'sku'.")
    return radar.varrer(user.id, sku)


@app.get("/api/radar/historico")
def radar_historico(sku: str, dias: int = 7, user: User = Depends(auth.get_current_user)):
    """Série por concorrente + estatísticas (menor/maior/moda/último) do período."""
    return radar.historico(user.id, sku, dias)


@app.post("/api/radar/recomendacao")
def radar_recomendacao(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Radar + motor de decisão: recomendação de preço por concorrente (e geral).

    Body: {sku, custo_base, preco_atual, canal?, comissao?, fixo?, imposto?,
           cartao?, piso_margem?, estrategia?, delta?, delta_tipo?}
    """
    if (not payload.get("sku") or payload.get("custo_base") is None
            or payload.get("preco_atual") is None):
        raise HTTPException(status_code=422,
                            detail="Informe 'sku', 'custo_base' e 'preco_atual'.")
    return radar.recomendar(
        user.id, payload["sku"],
        custo_base=float(payload["custo_base"]), preco_atual=float(payload["preco_atual"]),
        canal=payload.get("canal", "mercadolivre"),
        comissao=payload.get("comissao"), fixo=payload.get("fixo"),
        imposto=float(payload.get("imposto", 0)), cartao=float(payload.get("cartao", 0)),
        piso_margem=float(payload.get("piso_margem", 15)),
        estrategia=payload.get("estrategia", "match"),
        delta=float(payload.get("delta", 1)), delta_tipo=payload.get("delta_tipo", "pct"),
    )


# ============================ Nota Fiscal (NF-e) =========================== #
def _nfe_cfg(user_id: int) -> NfeConfig:
    """Pega (ou cria) a config de NF-e do tenant."""
    with SessionLocal() as db:
        cfg = db.query(NfeConfig).filter(NfeConfig.user_id == user_id).first()
        if cfg is None:
            cfg = NfeConfig(user_id=user_id)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)
        db.expunge(cfg)
        return cfg


def _cfg_dict(cfg: NfeConfig) -> dict:
    return {"auto": bool(cfg.auto), "desconto_tipo": cfg.desconto_tipo,
            "desconto_valor": cfg.desconto_valor, "remover_frete": bool(cfg.remover_frete),
            "situacao_pendente": cfg.situacao_pendente}


@app.get("/api/nfe/config")
def nfe_get_config(user: User = Depends(auth.get_current_user)):
    return _cfg_dict(_nfe_cfg(user.id))


@app.put("/api/nfe/config")
def nfe_set_config(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Liga/desliga o modo automático e define a regra padrão (desconto + frete)."""
    with SessionLocal() as db:
        cfg = db.query(NfeConfig).filter(NfeConfig.user_id == user.id).first()
        if cfg is None:
            cfg = NfeConfig(user_id=user.id)
            db.add(cfg)
        if "auto" in payload:
            cfg.auto = bool(payload["auto"])
        if "desconto_tipo" in payload and payload["desconto_tipo"] in ("percentual", "valor"):
            cfg.desconto_tipo = payload["desconto_tipo"]
        if "desconto_valor" in payload:
            cfg.desconto_valor = float(payload["desconto_valor"])
        if "remover_frete" in payload:
            cfg.remover_frete = bool(payload["remover_frete"])
        if "situacao_pendente" in payload:
            cfg.situacao_pendente = int(payload["situacao_pendente"])
        db.commit()
        db.refresh(cfg)
        return _cfg_dict(cfg)


@app.get("/api/nfe/pendentes")
def nfe_pendentes(pagina: int = 1, limite: int = 100,
                  situacao: int | None = None,
                  user: User = Depends(auth.get_current_user)):
    """Lista as NF-e pendentes (editáveis). Usa o código de situação da config."""
    cfg = _nfe_cfg(user.id)
    sit = situacao if situacao is not None else cfg.situacao_pendente
    try:
        return bling.listar_nfe(user.id, pagina=pagina, limite=limite, situacao=sit)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/simular")
def nfe_simular(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Revisão pura (sem tocar no Bling): recalcula desconto/frete sobre os itens.

    Body: {itens:[{indice,descricao,quantidade,valor_unitario}], frete,
           desconto_tipo, desconto_valor, descontos_por_item?, remover_frete}
    """
    return nfe.aplicar_edicao(
        payload.get("itens", []),
        desconto_tipo=payload.get("desconto_tipo", "percentual"),
        desconto_valor=float(payload.get("desconto_valor", 0)),
        descontos_por_item=payload.get("descontos_por_item"),
        remover_frete=bool(payload.get("remover_frete", True)),
        frete_atual=float(payload.get("frete", 0)),
    )


@app.post("/api/nfe/auto/processar")
def nfe_auto_processar(user: User = Depends(auth.get_current_user)):
    """Roda o modo automático agora: aplica a regra padrão em todas as pendentes."""
    cfg = _nfe_cfg(user.id)
    if not cfg.auto:
        raise HTTPException(status_code=409, detail="Modo automático desligado. Ligue em /api/nfe/config.")
    try:
        return nfe.processar_automatico(user.id, cfg)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}")
def nfe_obter(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Detalhe normalizado de uma nota (itens + frete) para edição."""
    try:
        return nfe.normalizar_nfe(bling.obter_nfe(user.id, nfe_id))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/{nfe_id}/aplicar")
def nfe_aplicar(nfe_id: str, payload: dict = Body(...),
                user: User = Depends(auth.get_current_user)):
    """Aplica a edição numa nota. enviar=false revisa; enviar=true devolve ao Bling.

    Body: {desconto_tipo, desconto_valor, descontos_por_item?, remover_frete, enviar}
    """
    try:
        return nfe.editar_nota(
            user.id, nfe_id,
            desconto_tipo=payload.get("desconto_tipo", "percentual"),
            desconto_valor=float(payload.get("desconto_valor", 0)),
            descontos_por_item=payload.get("descontos_por_item"),
            remover_frete=bool(payload.get("remover_frete", True)),
            enviar=bool(payload.get("enviar", False)),
        )
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
