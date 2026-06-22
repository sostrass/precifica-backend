import asyncio
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from . import ai, agentes, auth, bling, decisao, kpis, nfe, precificacao, pricing, qualidade, radar, scraper, webhooks
from .config import settings
from .db import run_migrations, SessionLocal, Base, engine
from .models import NfeConfig, User, WebhookEvento


async def _agendador_radar():
    """Laço em segundo plano: varre os alvos de todos os tenants a cada N horas."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(max(settings.radar_intervalo_horas, 1) * 3600)
        try:
            await loop.run_in_executor(None, radar.varrer_todos)
        except Exception:  # noqa: BLE001 — o agendador nunca derruba o app
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    # garante a tabela de eventos de webhook (aditivo, não mexe nas existentes)
    try:
        WebhookEvento.__table__.create(bind=engine, checkfirst=True)
    except Exception:  # noqa: BLE001
        pass
    tarefa = asyncio.create_task(_agendador_radar()) if settings.radar_intervalo_horas > 0 else None
    yield
    if tarefa:
        tarefa.cancel()


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
def bling_callback(code: str = Query(...), state: str = Query(None)):
    try:
        uid, st = bling.resolver_state(state)   # casa o exato ou a única pendente
        bling.exchange_code(uid, code)          # se falhar, o state segue p/ retry
        bling.consume_state(st)                 # consome só no sucesso
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mensagem": "Conta Bling autorizada com sucesso."}


@app.get("/auth/bling/status")
def bling_status(user: User = Depends(auth.get_current_user)):
    return bling.token_status(user.id)


# ------------------------------- Webhooks --------------------------------- #
@app.post("/webhooks/bling/{token}")
async def receber_webhook(token: str, request: Request):
    """Recebe os eventos do Bling (push). Responde 200 SEMPRE e rápido — se falhar
    por 3 dias o Bling desabilita o webhook. Erros internos são engolidos (logados)."""
    user_id = webhooks.verificar_token(token)
    if not user_id:
        # token inválido: ainda assim 200 para o Bling não desabilitar por engano de rota
        return {"ok": False}
    try:
        corpo = await request.json()
    except Exception:  # noqa: BLE001
        corpo = {}
    try:
        db = SessionLocal()
        try:
            reg = webhooks.registrar_evento(db, user_id, corpo)
            if reg:
                webhooks.processar(user_id, reg.recurso, reg.acao, corpo.get("data") or {})
                reg.processado = True
                db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — nunca devolve erro ao Bling
        pass
    return {"ok": True}


@app.get("/api/webhooks/url")
def webhook_url(request: Request, user: User = Depends(auth.get_current_user)):
    """URL única e assinada deste tenant para colar no Bling (Configuração de servidores)."""
    base = str(request.base_url).rstrip("/")
    token = webhooks.gerar_token(user.id)
    return {"url": f"{base}/webhooks/bling/{token}",
            "recursos": ["produtos", "pedidos de vendas", "notas fiscais eletrônicas", "estoque"]}


@app.get("/api/webhooks/eventos")
def webhook_eventos(limite: int = 30, user: User = Depends(auth.get_current_user)):
    """Últimos eventos recebidos do Bling (pra conferir que está chegando)."""
    db = SessionLocal()
    try:
        q = (db.query(WebhookEvento).filter_by(user_id=user.id)
             .order_by(WebhookEvento.recebido_em.desc()).limit(min(limite, 100)).all())
        return {"eventos": [{
            "event": e.event, "recurso": e.recurso, "acao": e.acao,
            "entidade_id": e.entidade_id, "processado": e.processado,
            "recebido_em": e.recebido_em.isoformat() if e.recebido_em else None,
        } for e in q]}
    finally:
        db.close()


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
    """Detalhe limpo do produto + avaliação de preço por canal (motor de faixas)."""
    try:
        raw = bling.obter_produto(user.id, produto_id).get("data", {})
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    custo = float(raw.get("precoCusto") or (raw.get("fornecedor") or {}).get("precoCusto") or 0)
    preco = float(raw.get("preco") or 0)
    ncm = (raw.get("tributacao") or {}).get("ncm") or raw.get("ncm") or ""
    fotos = [i.get("link") for i in (((raw.get("midia") or {}).get("imagens") or {}).get("externas") or []) if i.get("link")]
    estoque = (raw.get("estoque") or {}).get("saldoVirtualTotal")
    dims = raw.get("dimensoes") or {}
    cfg = precificacao.obter_config(user.id)
    canais = []
    for c in cfg.get("canais", []):
        if not c.get("ativo"):
            continue
        av = precificacao.avaliar_com_cfg(cfg, custo, preco, c["canal"])
        canais.append({"canal": c["canal"], "nome": c["nome"],
                       "preco_sugerido": av["preco_sugerido"],
                       "margem_sugerida": av["margem_sugerida"],
                       "margem_atual": av["margem_atual"]})

    return {
        "id": raw.get("id"),
        "sku": raw.get("codigo"),
        "nome": raw.get("nome"),
        "preco": preco,
        "custo": custo,
        "ncm": ncm,
        "gtin": raw.get("gtin"),
        "peso_bruto": raw.get("pesoBruto"),
        "peso_liquido": raw.get("pesoLiquido"),
        "descricao_curta": raw.get("descricaoCurta"),
        "descricao_complementar": raw.get("descricaoComplementar"),
        "campos_customizados": [
            {"id": c.get("idCampoCustomizado"), "vinculo": c.get("idVinculo"),
             "rotulo": c.get("item") or "", "valor": c.get("valor")}
            for c in (raw.get("camposCustomizados") or [])
        ],
        "situacao": raw.get("situacao"),
        "tipo": raw.get("tipo"),
        "marca": raw.get("marca"),
        "unidade": raw.get("unidade"),
        "estoque": estoque,
        "dimensoes": {"largura": dims.get("largura"), "altura": dims.get("altura"),
                      "profundidade": dims.get("profundidade")} if dims else None,
        "fotos": fotos,
        "precificacao": canais,
        "qualidade": qualidade.score_cadastro({
            "nome": raw.get("nome"),
            "ean": raw.get("gtin"),
            "ncm": ncm,
            "peso": raw.get("pesoBruto") or raw.get("pesoLiquido"),
            "descricao": raw.get("descricaoComplementar") or raw.get("descricaoCurta"),
        }),
    }


_MAPA_PRODUTO = {  # nome amigável (front) -> campo da API do Bling
    "nome": "nome", "preco": "preco", "custo": "precoCusto", "ncm": "ncm",
    "gtin": "gtin", "peso_bruto": "pesoBruto", "peso_liquido": "pesoLiquido",
    "descricao_curta": "descricaoCurta", "descricao_complementar": "descricaoComplementar",
}


@app.put("/api/produtos/{produto_id}")
def atualizar_produto(produto_id: int, payload: dict = Body(...),
                      user: User = Depends(auth.get_current_user)):
    """Edita campos do produto no Bling (envia só o que veio no corpo)."""
    campos = {_MAPA_PRODUTO[k]: v for k, v in payload.items()
              if k in _MAPA_PRODUTO and v is not None}
    if not campos:
        raise HTTPException(status_code=422, detail="Nenhum campo editável informado.")
    try:
        bling.atualizar_produto(user.id, produto_id, campos)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"ok": True, "atualizados": sorted(campos.keys())}


# ---------- Diagnóstico: JSON cru do Bling (p/ construir telas sobre dado real) ----------
@app.get("/api/diagnostico/precos/{produto_id}")
def diag_precos(produto_id, user: User = Depends(auth.get_current_user)):
    """Payload CRU do produto + tabelas de preço — revela onde o Bling guarda o preço por canal."""
    try:
        prod = (bling.obter_produto(user.id, produto_id) or {}).get("data", {})
        try:
            tabelas = bling.listar_tabelas_precos(user.id)
        except Exception as e:  # noqa: BLE001
            tabelas = {"erro": str(e)}
        return {"produto_id": produto_id,
                "preco_base": prod.get("preco"),
                "tem_variacoes": bool(prod.get("variacoes")),
                "chaves_produto": sorted(prod.keys()),
                "tabelas_precos": tabelas}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/produtos/{produto_id}/sincronizacao")
def produto_sincronizacao(produto_id, user: User = Depends(auth.get_current_user)):
    """Preço-alvo por canal (preserva o líquido) vs. preço registrado no Bling por marketplace.
    Lê os vínculos multiloja. Canais não configurados aparecem só com o registrado + flag de
    prejuízo (preço abaixo do líquido)."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
        vinc = bling.vinculos_multiloja(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    base = float(raw.get("preco") or 0)
    cfg = precificacao.obter_config(user.id)
    precos = {v["canal"]: v["preco"] for v in vinc if v.get("canal") and v["preco"] > 0}
    canais = precificacao.divergencias(cfg, base, precos)
    vinculos = [{
        "nome": v["nome"], "integracao": v["integracao"], "canal": v.get("canal"),
        "id_anuncio": v.get("id_anuncio"), "preco_registrado": v["preco"], "link": v.get("link"),
        "publicado": v.get("publicado"), "ativo": v.get("ativo"),
        "prejuizo": bool(v["preco"] > 0 and base > 0 and v["preco"] < base),
    } for v in vinc]
    return {"produto_id": raw.get("id"), "sku": raw.get("codigo"),
            "base_venda": round(base, 2), "canais": canais,
            "vinculos": vinculos, "fonte_lida": bool(vinc)}


@app.get("/api/diagnostico/multiloja/{produto_id}")
def diagnostico_multiloja(produto_id, lojas: str = "", user: User = Depends(auth.get_current_user)):
    """Testa se a API pública do Bling expõe preço por canal via ?idLoja=.
    'lojas' = IDs separados por vírgula. Sem isso, usa as lojas conhecidas desta conta."""
    padrao = ["203414926", "204884434", "203923623", "205946980", "205916963", "205693668"]
    ids = [s.strip() for s in lojas.split(",") if s.strip()] or padrao
    try:
        return bling.probe_multiloja(user.id, produto_id, ids)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/produtos/{produto_id}/posicionamento")
def produto_posicionamento(produto_id, canal: str = "mercado_livre",
                           user: User = Depends(auth.get_current_user)):
    """Varredura de posicionamento: acha o produto no canal e classifica o preço vs mercado."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return scraper.posicionamento(raw.get("nome") or "", raw.get("preco") or 0, canal=canal)


@app.get("/api/produtos/{produto_id}/conselho")
def produto_conselho(produto_id, user: User = Depends(auth.get_current_user)):
    """Convoca o Conselho de IA para o produto: falas dos agentes + plano com ações."""
    try:
        return agentes.conselho(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/kpis")
def kpis_dashboard(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """KPIs do período: GMV, ticket, venda por canal, mais vendidos, tendência, risco de ruptura."""
    try:
        pedidos = bling.listar_pedidos_periodo(user.id, dias=dias)
        produtos = (bling.listar_produtos(user.id, limite=100).get("data") or [])
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return {"dias": dias, **kpis.calcular(pedidos, produtos)}


@app.get("/api/diagnostico/pedidos")
def diag_pedidos(user: User = Depends(auth.get_current_user)):
    """Payload CRU dos pedidos de venda — revela se a listagem traz itens (mais vendidos)."""
    try:
        return bling.listar_pedidos(user.id, limite=3)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/ia/campo")
def ia_campo(payload: dict, user: User = Depends(auth.get_current_user)):
    """Reescreve ou cria o conteúdo de um campo com IA. Body: {campo, texto, instrucao?, nome?}."""
    campo = (payload.get("campo") or "descrição").strip()
    texto = (payload.get("texto") or "").strip()
    instrucao = (payload.get("instrucao") or "").strip()
    nome = (payload.get("nome") or "").strip()
    base = texto if texto else f"(vazio — crie do zero a partir do produto: {nome or 'produto de armarinho'})"
    prompt = (
        "Você é copywriter de e-commerce para marketplaces brasileiros (Mercado Livre, Shopee, Amazon). "
        f"Reescreva o campo '{campo}' do anúncio, otimizando para busca e conversão, mantendo a informação "
        "correta, em HTML simples quando fizer sentido. Responda SOMENTE com o texto final, sem comentários.\n"
        + (f"Instrução do usuário: {instrucao}\n" if instrucao else "")
        + (f"Produto: {nome}\n" if nome else "")
        + f"Conteúdo atual:\n{base}"
    )
    try:
        return {"texto": ai._gerar_texto(user.id, prompt)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"IA indisponível: {e}")


@app.get("/api/diagnostico/sistema")
def diag_sistema(user: User = Depends(auth.get_current_user)):
    """Revela qual banco está em uso (SQLite efêmero x Postgres persistente) e o estado das migrações."""
    from sqlalchemy import text
    from .db import engine
    from .models import OAuthState
    banco = engine.url.get_backend_name()  # 'sqlite' ou 'postgresql'
    ver, n_states = None, -1
    with SessionLocal() as db:
        try:
            ver = db.execute(text("select version_num from alembic_version")).scalar()
        except Exception:  # noqa: BLE001
            pass
        try:
            n_states = db.query(OAuthState).count()
        except Exception:  # noqa: BLE001
            pass
    return {
        "banco": banco,
        "persistente": banco != "sqlite",
        "alerta": None if banco != "sqlite" else
                  "Backend em SQLite efêmero: dados e conexão somem a cada restart. Defina DATABASE_URL para o Postgres.",
        "alembic_versao": ver,
        "oauth_states_no_banco": n_states,
    }


@app.get("/api/diagnostico/produto/{produto_id}")
def diag_produto(produto_id: int, user: User = Depends(auth.get_current_user)):
    """Payload CRU de um produto no Bling — revela os campos reais (fotos, etc.)."""
    try:
        return bling.obter_produto(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/diagnostico/nfe/{nfe_id}")
def diag_nfe(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Payload CRU de uma NF-e no Bling — revela a estrutura real da nota inteira."""
    try:
        return bling.obter_nfe(user.id, nfe_id)
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
        custo = float(p.get("precoCusto") or (p.get("fornecedor") or {}).get("precoCusto") or 0)
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


@app.post("/api/radar/varrer-tudo")
def radar_varrer_tudo(user: User = Depends(auth.get_current_user)):
    """Varre de uma vez todos os SKUs com alvos ativos do usuário."""
    return radar.varrer_usuario(user.id)


@app.get("/api/radar/alertas")
def radar_alertas(dias: int = 7, sku: str | None = None, limiar_pct: float = 5.0,
                  user: User = Depends(auth.get_current_user)):
    """Feed de mudanças que pedem ação (quedas, altas, novos mínimos) a partir dos snapshots."""
    return radar.alertas(user.id, dias=dias, limiar_pct=limiar_pct, sku=sku)


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
    """Detalhe normalizado de uma nota (itens + frete) para o editor de desconto."""
    try:
        return nfe.normalizar_nfe(bling.obter_nfe(user.id, nfe_id))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/completa")
def nfe_completa(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Visão COMPLETA da nota: destinatário, totais, impostos, transporte, itens e links."""
    try:
        return nfe.detalhar_nfe(bling.obter_nfe(user.id, nfe_id))
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
