import asyncio
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import ai, agentes, auth, bling, catalogo, decisao, kpis, nfe, precificacao, pricing, qualidade, radar, scraper, shopee, shopee_boost, webhooks
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


async def _agendador_boost():
    """Laço do auto-boost da Shopee: a cada 10min checa quem tem vaga e impulsiona
    os próximos da fila (reimpulsiona quando os 4h de um lote acabam)."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(600)  # 10 minutos
        try:
            from .models import ShopeeBoostConfig
            db = SessionLocal()
            try:
                ativos = [c.user_id for c in db.query(ShopeeBoostConfig).filter_by(ativo=True).all()]
            finally:
                db.close()
            for uid in ativos:
                await loop.run_in_executor(None, shopee_boost.ciclo, uid)
        except Exception:  # noqa: BLE001 — nunca derruba o app
            pass


async def _agendador_catalogo():
    """Rede de segurança: re-sincroniza o catálogo de quem já sincronizou ao menos uma vez,
    a cada N horas (além do webhook, caso algum evento tenha se perdido)."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(max(settings.catalogo_resync_horas, 1) * 3600)
        try:
            from .models import CatalogoSync
            db = SessionLocal()
            try:
                usuarios = [c.user_id for c in db.query(CatalogoSync).all()]
            finally:
                db.close()
            for uid in usuarios:
                await loop.run_in_executor(None, catalogo.sincronizar_tudo, uid)
        except Exception:  # noqa: BLE001
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    # garante tabelas aditivas — não mexe nas existentes
    try:
        from .models import (ProdutoSync, ProdutoCache, CatalogoSync,
                             ShopeeConta, ShopeeBoostItem, ShopeeBoostConfig)
        for M in (WebhookEvento, ProdutoSync, ProdutoCache, CatalogoSync,
                  ShopeeConta, ShopeeBoostItem, ShopeeBoostConfig):
            M.__table__.create(bind=engine, checkfirst=True)
    except Exception:  # noqa: BLE001
        pass
    tarefa = asyncio.create_task(_agendador_radar()) if settings.radar_intervalo_horas > 0 else None
    tarefa_boost = asyncio.create_task(_agendador_boost())
    tarefa_cat = asyncio.create_task(_agendador_catalogo()) if settings.catalogo_resync_horas > 0 else None
    yield
    if tarefa:
        tarefa.cancel()
    tarefa_boost.cancel()
    if tarefa_cat:
        tarefa_cat.cancel()


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
def _processar_evento_async(user_id: int, evento_db_id: int):
    """Processa o evento fora do ciclo da resposta (fila em background)."""
    db = SessionLocal()
    try:
        reg = db.query(WebhookEvento).filter_by(id=evento_db_id).first()
        if not reg or reg.processado:
            return
        data = (reg.payload or {}).get("data") or {}
        recurso = (reg.recurso or "").lower()
        webhooks.processar(user_id, reg.recurso, reg.acao, data)
        # Mantém o cache do catálogo atualizado a partir do push
        if recurso in ("produto", "produtos") and reg.entidade_id:
            if (reg.acao or "").lower() in ("deleted", "deletado", "excluido"):
                catalogo.remover_produto(db, user_id, reg.entidade_id)
            else:
                catalogo.atualizar_do_bling(user_id, reg.entidade_id)
            webhooks.confirmar_sync(db, user_id, reg.entidade_id)
        reg.processado = True
        db.commit()
    except Exception:  # noqa: BLE001 — background nunca quebra o app
        pass
    finally:
        db.close()


@app.post("/webhooks/bling/{token}")
async def receber_webhook(token: str, request: Request, background_tasks: BackgroundTasks):
    """Recebe os eventos do Bling (push). Grava rápido, responde 200 NA HORA e processa
    em background (fila) — se falhar por 3 dias o Bling desabilita o webhook."""
    user_id = webhooks.verificar_token(token)
    if not user_id:
        return {"ok": False}
    try:
        corpo = await request.json()
    except Exception:  # noqa: BLE001
        corpo = {}
    try:
        db = SessionLocal()
        try:
            reg = webhooks.registrar_evento(db, user_id, corpo)  # gravação rápida + dedupe
            if reg:
                background_tasks.add_task(_processar_evento_async, user_id, reg.id)
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


# ------------------------------- Catálogo (cache) ------------------------- #
@app.post("/api/catalogo/sincronizar")
def catalogo_sincronizar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara a sincronização COMPLETA do catálogo (puxa tudo do Bling pro cache).
    Roda em background; acompanhe por /api/catalogo/sync_status."""
    est = catalogo.status(user.id)
    if est["status"] == "rodando":
        return {"ok": True, "ja_rodando": True, **est}
    background_tasks.add_task(catalogo.sincronizar_tudo, user.id)
    return {"ok": True, "iniciado": True}


@app.get("/api/catalogo/sync_status")
def catalogo_sync_status(user: User = Depends(auth.get_current_user)):
    return catalogo.status(user.id)


@app.get("/api/catalogo")
def catalogo_listar(busca: str = "", pagina: int = 1, limite: int = 50, situacao: str = "",
                    user: User = Depends(auth.get_current_user)):
    """Lê o catálogo DO CACHE local (rápido, sem martelar o Bling)."""
    return catalogo.listar(user.id, busca=busca, pagina=pagina, limite=min(limite, 200), situacao=situacao)


@app.get("/api/shopee/diagnostico")
def shopee_diagnostico(user: User = Depends(auth.get_current_user)):
    """Testa cada chamada da Shopee individualmente e reporta ok/erro por funcionalidade.
    Serve para descobrir exatamente o que não funciona (e o porquê), sem adivinhação."""
    import time as _t
    if not shopee.app_configurado():
        return {"app": False, "conectado": False, "testes": [],
                "resumo": "App Shopee não configurado no servidor (PARTNER_ID/PARTNER_KEY)."}
    testes = []

    def probe(nome, path, extra=None, metodo="GET"):
        try:
            d = shopee._chamar(user.id, path, extra=extra, metodo=metodo, timeout=8)
            err = d.get("error") if isinstance(d, dict) else None
            if err:
                testes.append({"nome": nome, "ok": False, "erro": f"{err}: {d.get('message') or ''}".strip(": ")})
            else:
                resp = d.get("response") or {}
                qtd = next((len(resp[k]) for k in ("item", "item_comment_list", "question_list",
                            "order_list", "discount_list", "return_list")
                            if isinstance(resp.get(k), list)), None)
                testes.append({"nome": nome, "ok": True, "qtd": qtd})
        except shopee.ShopeeError as e:
            testes.append({"nome": nome, "ok": False, "erro": str(e)})
        except Exception as e:  # noqa: BLE001
            testes.append({"nome": nome, "ok": False, "erro": f"{type(e).__name__}: {e}"})

    agora = int(_t.time())
    probe("Conexão e dados da loja", "/api/v2/shop/get_shop_info")
    probe("Produtos (catálogo Shopee)", "/api/v2/product/get_item_list",
          {"offset": 0, "page_size": 3, "item_status": "NORMAL"})
    probe("Avaliações (reviews)", "/api/v2/product/get_comment", {"page_size": 5})
    probe("Perguntas (Q&A)", "/api/v2/sip/get_item_qa_list", {"page_no": 1, "page_size": 5})
    probe("Pedidos", "/api/v2/order/get_order_list",
          {"time_range_field": "create_time", "time_from": agora - 7 * 86400,
           "time_to": agora, "page_size": 5})
    probe("Descontos / promoções", "/api/v2/discount/get_discount_list",
          {"discount_status": "ongoing", "page_size": 5})
    probe("Saúde da loja", "/api/v2/account_health/get_shop_performance")
    probe("Devoluções", "/api/v2/returns/get_return_list", {"page_no": 0, "page_size": 5})
    ok = sum(1 for t in testes if t["ok"])
    return {"app": True, "conectado": testes[0]["ok"], "ok": ok, "total": len(testes), "testes": testes}


# -------------------------------- Shopee ---------------------------------- #
@app.get("/api/shopee/status")
def shopee_status(user: User = Depends(auth.get_current_user)):
    """Estado da conexão (app + loja) e validação com os dados da loja."""
    est = shopee.status_conexao(user.id)
    if not est.get("loja"):
        return {"configurada": False, "ok": False, **est}
    try:
        info = shopee.info_loja(user.id)
        return {"configurada": True, "ok": True, "loja": info.get("response") or info, **est}
    except shopee.ShopeeError as e:
        return {"configurada": True, "ok": False, "erro": str(e), **est}


@app.post("/api/shopee/conectar")
def shopee_conectar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Salva as credenciais da loja (shop_id + access_token + refresh_token).
    Use quando você já tem os tokens em mãos (sem passar pelo fluxo de OAuth)."""
    shop_id = payload.get("shop_id"); at = payload.get("access_token")
    if not shop_id or not at:
        raise HTTPException(status_code=422, detail="Informe shop_id e access_token.")
    shopee.salvar_conta(user.id, shop_id, at, payload.get("refresh_token"),
                        int(payload.get("expire_in", 14400)))
    return {"ok": True}


@app.post("/api/shopee/renovar")
def shopee_renovar(user: User = Depends(auth.get_current_user)):
    try:
        return {"ok": shopee.renovar_token(user.id)}
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


def _shopee_redirect_base(request: Request) -> str:
    """URL pública do backend para o Shopee redirecionar de volta após o login."""
    if settings.shopee_redirect_base:
        return settings.shopee_redirect_base.rstrip("/")
    base = str(request.base_url).rstrip("/")
    return base.replace("http://", "https://") if "localhost" not in base and "127.0.0.1" not in base else base


@app.get("/api/shopee/auth/login")
def shopee_auth_login(request: Request, user: User = Depends(auth.get_current_user)):
    """Devolve a URL de autorização da Shopee. O front abre num popup; ao autorizar,
    a Shopee redireciona para /api/shopee/auth/callback com o code, e a gente troca por token."""
    if not shopee.app_configurado():
        raise HTTPException(status_code=400, detail="App Shopee não configurado (PARTNER_ID/PARTNER_KEY).")
    state = shopee.state_token(user.id)
    redirect = f"{_shopee_redirect_base(request)}/api/shopee/auth/callback/{state}"
    return {"url": shopee.url_autorizacao(redirect)}


_HTML_OK = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Shopee conectada</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:3rem;background:#0b0b0f;color:#eee">
<div style="font-size:48px">✅</div>
<h2 style="color:#34C759">Loja Shopee conectada!</h2>
<p style="color:#aaa">Pode fechar esta janela e voltar ao Precifica AI.</p>
<script>try{if(window.opener){window.opener.postMessage('shopee_auth_ok','*');}}catch(e){}setTimeout(function(){window.close();},1500);</script>
</body></html>"""

_HTML_ERR = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Erro</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:3rem;background:#0b0b0f;color:#eee">
<div style="font-size:48px">⚠️</div><h2 style="color:#ff5a52">Não foi possível conectar</h2>
<p style="color:#aaa">{msg}</p>
<script>try{{if(window.opener){{window.opener.postMessage('shopee_auth_err','*');}}}}catch(e){{}}</script>
</body></html>"""


@app.get("/api/shopee/auth/callback/{state}")
def shopee_auth_callback(state: str, code: str = "", shop_id: str = ""):
    """Callback do OAuth da Shopee (aberto pelo redirect). Troca o code por token e salva."""
    uid = shopee.ler_state(state)
    if not uid:
        return HTMLResponse(_HTML_ERR.format(msg="Sessão de conexão expirada. Tente novamente."), status_code=400)
    if not code:
        return HTMLResponse(_HTML_ERR.format(msg="Autorização não recebida da Shopee."), status_code=400)
    try:
        shopee.trocar_code_por_token(uid, code, shop_id)
        return HTMLResponse(_HTML_OK)
    except shopee.ShopeeError as e:
        return HTMLResponse(_HTML_ERR.format(msg=str(e)), status_code=400)


@app.get("/api/shopee/produtos")
def shopee_produtos(offset: int = 0, limite: int = 50, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_itens(user.id, offset=offset, limite=limite)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/desempenho")
def shopee_desempenho(user: User = Depends(auth.get_current_user)):
    try:
        return shopee.desempenho_loja(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Boost (auto-impulsionamento rotativo) ----
@app.get("/api/shopee/boost/status")
def shopee_boost_status(user: User = Depends(auth.get_current_user)):
    return shopee_boost.status(user.id)


@app.put("/api/shopee/boost/config")
def shopee_boost_config(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    db = SessionLocal()
    try:
        from .models import ShopeeBoostConfig
        c = db.query(ShopeeBoostConfig).filter_by(user_id=user.id).first()
        if not c:
            c = ShopeeBoostConfig(user_id=user.id); db.add(c)
        if "ativo" in payload: c.ativo = bool(payload["ativo"])
        if "criterio" in payload: c.criterio = payload["criterio"]
        if "janela_inicio" in payload: c.janela_inicio = int(payload["janela_inicio"])
        if "janela_fim" in payload: c.janela_fim = int(payload["janela_fim"])
        if "max_simultaneos" in payload: c.max_simultaneos = min(int(payload["max_simultaneos"]), 5)
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/shopee/boost/itens")
def shopee_boost_add(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Adiciona itens à lista de boost. Body: {itens:[{item_id, nome, fixo?, prioridade?}]}."""
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem
        add = payload.get("itens", [])
        fixos = db.query(ShopeeBoostItem).filter_by(user_id=user.id, fixo=True).count()
        for it in add[:30]:
            iid = str(it.get("item_id"))
            if not iid:
                continue
            reg = db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=iid).first()
            if not reg:
                reg = ShopeeBoostItem(user_id=user.id, item_id=iid); db.add(reg)
            reg.nome = it.get("nome") or reg.nome
            if it.get("fixo") and fixos < 5:
                reg.fixo = True; fixos += 1
            if "prioridade" in it:
                reg.prioridade = int(it["prioridade"])
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.delete("/api/shopee/boost/itens/{item_id}")
def shopee_boost_remove(item_id: str, user: User = Depends(auth.get_current_user)):
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem
        db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=item_id).delete()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/shopee/boost/itens/{item_id}/fixar")
def shopee_boost_fixar(item_id: str, payload: dict = Body(default={}),
                       user: User = Depends(auth.get_current_user)):
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem
        reg = db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=item_id).first()
        if not reg:
            raise HTTPException(status_code=404, detail="Item não está na lista.")
        fixar = bool(payload.get("fixo", not reg.fixo))
        if fixar and db.query(ShopeeBoostItem).filter_by(user_id=user.id, fixo=True).count() >= 5:
            raise HTTPException(status_code=422, detail="Máximo de 5 produtos fixos.")
        reg.fixo = fixar
        db.commit()
        return {"ok": True, "fixo": reg.fixo}
    finally:
        db.close()


@app.post("/api/shopee/boost/rodar")
def shopee_boost_rodar(user: User = Depends(auth.get_current_user)):
    """Roda um ciclo de boost agora (manual). O agendador faz isso sozinho."""
    try:
        return shopee_boost.ciclo(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/boost/sincronizar_nomes")
def shopee_boost_sincronizar_nomes(user: User = Depends(auth.get_current_user)):
    """Resolve os nomes reais dos produtos do boost (1 chamada Shopee, sob demanda).
    Separado do status para nunca travar o painel."""
    try:
        return shopee_boost.sincronizar_nomes(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


# ---- Avaliações ----
@app.get("/api/shopee/avaliacoes")
def shopee_avaliacoes(status: str = "UNANSWERED", cursor: str = "",
                      user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_avaliacoes(user.id, status=status, cursor=cursor)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/avaliacoes/responder")
def shopee_avaliacoes_responder(payload: dict = Body(...),
                                user: User = Depends(auth.get_current_user)):
    """Responde uma avaliação. Body: {comment_id, texto, ia?, nota?, comentario?}.
    Se ia=true, gera a resposta com IA a partir da nota e do comentário do cliente."""
    cid = payload.get("comment_id")
    texto = payload.get("texto")
    if payload.get("ia") and not texto:
        nota = payload.get("nota", 5)
        coment = payload.get("comentario", "")
        prompt = (
            "Você é o dono de uma loja de armarinho na Shopee respondendo a uma avaliação. "
            "Seja cordial, breve, humano e em português do Brasil. "
            f"Nota: {nota}/5. Comentário do cliente: \"{coment}\". "
            "Se a nota for alta, agradeça com calor; se for baixa, peça desculpas, mostre que se "
            "importa e ofereça resolver pelo chat da Shopee. Responda SÓ com o texto da resposta."
        )
        try:
            texto = ai._gerar_texto(user.id, prompt)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"IA falhou: {e}")
    if not cid or not texto:
        raise HTTPException(status_code=422, detail="Informe comment_id e texto (ou ia=true).")
    try:
        shopee.responder_avaliacao(user.id, cid, texto)
        return {"ok": True, "texto": texto}
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Pedidos & financeiro ----
@app.get("/api/shopee/pedidos")
def shopee_pedidos(dias: int = 7, cursor: str = "", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_pedidos(user.id, dias=dias, cursor=cursor)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/{order_sn}/repasse")
def shopee_repasse(order_sn: str, user: User = Depends(auth.get_current_user)):
    """Escrow: valor líquido real recebido (preço − comissão − taxas)."""
    try:
        return shopee.repasse_pedido(user.id, order_sn)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Promoções: descontos ----
@app.get("/api/shopee/descontos")
def shopee_descontos(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_descontos(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/descontos")
def shopee_criar_desconto(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, inicio (epoch), fim (epoch), itens:[{item_id, promotion_price, ...}]}."""
    try:
        return shopee.criar_desconto(user.id, payload["nome"], int(payload["inicio"]),
                                     int(payload["fim"]), payload.get("itens", []))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe nome, inicio, fim e itens.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/descontos/{discount_id}")
def shopee_encerrar_desconto(discount_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_desconto(user.id, discount_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Promoções: cupons ----
@app.get("/api/shopee/cupons")
def shopee_cupons(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_cupons(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/cupons")
def shopee_criar_cupom(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, codigo, inicio, fim, tipo_desconto(1=valor,2=%), valor, compra_minima, quantidade, escopo?}."""
    try:
        return shopee.criar_cupom(user.id, payload["nome"], payload["codigo"],
                                  int(payload["inicio"]), int(payload["fim"]),
                                  int(payload["tipo_desconto"]), float(payload["valor"]),
                                  float(payload.get("compra_minima", 0)),
                                  int(payload["quantidade"]), int(payload.get("escopo", 1)))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Faltam campos do cupom.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/cupons/{voucher_id}")
def shopee_encerrar_cupom(voucher_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_cupom(user.id, voucher_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Shopee Ads ----
@app.get("/api/shopee/ads")
def shopee_ads(dias: int = 7, user: User = Depends(auth.get_current_user)):
    out = {}
    try:
        out["saldo"] = shopee.ads_saldo(user.id)
    except shopee.ShopeeError as e:
        out["saldo_erro"] = str(e)
    try:
        out["desempenho"] = shopee.ads_desempenho(user.id, dias=dias)
    except shopee.ShopeeError as e:
        out["desempenho_erro"] = str(e)
    return out


# ---- Q&A do anúncio ----
@app.get("/api/shopee/perguntas")
def shopee_perguntas(status: str = "UNANSWERED", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_perguntas(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/perguntas/responder")
def shopee_responder_pergunta(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Responde uma pergunta do anúncio. {qa_id, texto, ia?, pergunta?}."""
    qid = payload.get("qa_id"); texto = payload.get("texto")
    if payload.get("ia") and not texto:
        prompt = ("Você é o vendedor respondendo a uma pergunta no anúncio (armarinho, Shopee Brasil). "
                  "Seja claro, cordial e direto, em português do Brasil. "
                  f"Pergunta do cliente: \"{payload.get('pergunta', '')}\". Responda SÓ com a resposta.")
        try:
            texto = ai._gerar_texto(user.id, prompt)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"IA falhou: {e}")
    if not qid or not texto:
        raise HTTPException(status_code=422, detail="Informe qa_id e texto (ou ia=true).")
    try:
        shopee.responder_pergunta(user.id, qid, texto)
        return {"ok": True, "texto": texto}
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Devoluções ----
@app.get("/api/shopee/devolucoes")
def shopee_devolucoes(dias: int = 30, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_devolucoes(user.id, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Divergência Bling × Shopee ----
@app.get("/api/shopee/divergencia")
def shopee_divergencia(user: User = Depends(auth.get_current_user)):
    """Cruza preço do anúncio Shopee × preço registrado no Bling (cache), por SKU."""
    try:
        return shopee.divergencia_bling_shopee(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Bundle Deal ----
@app.get("/api/shopee/bundles")
def shopee_bundles(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_bundles(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/bundles")
def shopee_criar_bundle(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, inicio, fim, rule_type(1=preço fixo,2=%,3=valor), valor, min_itens, item_ids:[]}."""
    try:
        return shopee.criar_bundle(user.id, payload["nome"], int(payload["inicio"]),
                                   int(payload["fim"]), int(payload["rule_type"]),
                                   float(payload["valor"]), int(payload.get("min_itens", 2)),
                                   payload.get("item_ids", []))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Faltam campos do bundle.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/bundles/{bundle_id}")
def shopee_encerrar_bundle(bundle_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_bundle(user.id, bundle_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Add-on Deal ----
@app.get("/api/shopee/addons")
def shopee_addons(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_addons(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/addons")
def shopee_criar_addon(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, inicio, fim, principais:[item_id], adicionais:[{item_id, add_on_deal_price}]}."""
    try:
        return shopee.criar_addon(user.id, payload["nome"], int(payload["inicio"]),
                                  int(payload["fim"]), payload.get("principais", []),
                                  payload.get("adicionais", []), int(payload.get("promotion_type", 0)))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Faltam campos do add-on.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/addons/{addon_id}")
def shopee_encerrar_addon(addon_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_addon(user.id, addon_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Flash Sale ----
@app.get("/api/shopee/flash/slots")
def shopee_flash_slots(dias: int = 7, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.flash_slots(user.id, dias=min(max(dias, 1), 30))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/flash")
def shopee_flash(tipo: int = 1, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_flash(user.id, tipo=tipo)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/flash")
def shopee_criar_flash(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {timeslot_id, itens:[{item_id, purchase_limit, models:[...]}]}."""
    try:
        return shopee.criar_flash(user.id, int(payload["timeslot_id"]), payload.get("itens", []))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe timeslot_id e itens.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/flash/{flash_id}")
def shopee_encerrar_flash(flash_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_flash(user.id, flash_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


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
    """Edita campos do produto no Bling (envia só o que veio no corpo) e registra o envio
    para o painel de sincronização (enviado -> confirmado quando o webhook voltar)."""
    campos = {_MAPA_PRODUTO[k]: v for k, v in payload.items()
              if k in _MAPA_PRODUTO and v is not None}
    if not campos:
        raise HTTPException(status_code=422, detail="Nenhum campo editável informado.")
    db = SessionLocal()
    try:
        try:
            bling.atualizar_produto(user.id, produto_id, campos)
        except bling.BlingAuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
        sku = payload.get("sku") or payload.get("codigo")
        webhooks.registrar_envio(db, user.id, produto_id, sku, sorted(payload.keys()))
    finally:
        db.close()
    return {"ok": True, "atualizados": sorted(campos.keys())}


@app.get("/api/produtos/{produto_id}/status")
def produto_status(produto_id, user: User = Depends(auth.get_current_user)):
    """Status de sincronização (enviado/confirmado/pendente) + plataformas vinculadas do produto."""
    db = SessionLocal()
    try:
        sync = webhooks.status_sync(db, user.id, produto_id)
    finally:
        db.close()
    try:
        vinc = bling.vinculos_multiloja(user.id, produto_id)
    except Exception:  # noqa: BLE001
        vinc = []
    plataformas = [{"nome": v["nome"], "integracao": v.get("integracao"),
                    "publicado": v.get("publicado"), "ativo": v.get("ativo"),
                    "id_anuncio": v.get("id_anuncio"), "preco": v.get("preco")} for v in vinc]
    return {"produto_id": produto_id, "sync": sync, "plataformas": plataformas,
            "fonte_canais": bool(vinc)}


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
    alvo_por_canal = {c["canal"]: c.get("preco_alvo") for c in canais}
    vinculos = [{
        "nome": v["nome"], "integracao": v["integracao"], "canal": v.get("canal"),
        "id_loja": v.get("id_loja"), "id_anuncio": v.get("id_anuncio"),
        "preco_registrado": v["preco"], "link": v.get("link"),
        "publicado": v.get("publicado"), "ativo": v.get("ativo"),
        "preco_alvo": alvo_por_canal.get(v.get("canal")),
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


@app.post("/api/produtos/{produto_id}/preco_canal")
def aplicar_preco_canal(produto_id, payload: dict = Body(...),
                        user: User = Depends(auth.get_current_user)):
    """Grava o preço no canal (marketplace) específico, sem tocar no preço-base.
    Body: {id_loja, preco}. Registra o envio para o painel de sincronização."""
    id_loja = payload.get("id_loja")
    preco = payload.get("preco")
    if not id_loja or preco in (None, ""):
        raise HTTPException(status_code=422, detail="Informe id_loja e preco.")
    try:
        res = bling.atualizar_preco_canal(user.id, produto_id, id_loja, float(preco))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bling recusou a gravação por canal: {e}")
    db = SessionLocal()
    try:
        webhooks.registrar_envio(db, user.id, produto_id, None, [f"preço canal {id_loja}"])
    finally:
        db.close()
    return res


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


def _status(margem) -> str:
    """Classifica a margem líquida em faixa de saúde. Usado pelo dashboard e pelo monitoramento.
    Critério alinhado ao pricing.py (saudável a partir de 15%)."""
    m = float(margem or 0)
    if m >= 15:
        return "lucro_ideal"
    if m >= 5:
        return "atencao"
    return "critico"


@app.get("/api/kpis")
def kpis_dashboard(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """KPIs do período: GMV, ticket, venda por canal, mais vendidos, tendência, risco de ruptura."""
    try:
        pedidos = bling.listar_pedidos_periodo(user.id, dias=dias)
        produtos = (bling.listar_produtos(user.id, limite=100).get("data") or [])
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    resultado = {"dias": dias, **kpis.calcular(pedidos, produtos)}
    # traduz o ID da loja para o nome da loja (ex.: 205946980 -> "Shopee NOVO")
    try:
        mapa = bling.lojas_da_conta(user.id)  # {id_loja: {nome, integracao}}
        for c in resultado.get("por_canal", []):
            meta = mapa.get(str(c.get("loja")))
            if meta and meta.get("nome"):
                c["loja"] = meta["nome"]
            elif str(c.get("loja")) == "sem_loja":
                c["loja"] = "Venda direta"
    except Exception:
        pass
    return resultado


@app.get("/api/dashboard/carteira")
def dashboard_carteira(canal: str = "mercadolivre", user: User = Depends(auth.get_current_user)):
    """Margem líquida por produto sobre o catálogo COMPLETO (cache), não a API live.
    Devolve resumo agregado (todos os produtos) + uma amostra de itens para o heatmap/watchlist."""
    produtos = catalogo.todos(user.id)
    cfg = precificacao.obter_config(user.id)
    cont = {"lucro_ideal": 0, "atencao": 0, "critico": 0}
    soma = 0.0
    n = 0
    itens = []
    for p in produtos:
        custo = float(p.get("custo") or 0)
        preco = float(p.get("preco") or 0)
        if preco <= 0:
            continue
        av = precificacao.avaliar_com_cfg(cfg, custo, preco, canal)
        margem = av["margem_atual"] if av["margem_atual"] is not None else (av["margem_sugerida"] or 0)
        st = _status(margem)
        cont[st] = cont.get(st, 0) + 1
        soma += margem
        n += 1
        itens.append({"sku": p.get("sku"), "nome": p.get("nome"), "custo": custo,
                      "preco_atual": preco, "preco_sugerido": av["preco_sugerido"],
                      "margem_liquida": margem, "status": st})
    for it in itens:
        it["pot"] = (round((it["preco_sugerido"] - it["preco_atual"]) / it["preco_atual"] * 100, 1)
                     if it["preco_atual"] and it["preco_sugerido"] else None)
    piores = sorted([i for i in itens if i["status"] != "lucro_ideal"],
                    key=lambda x: x["margem_liquida"])[:6]
    melhores = sorted([i for i in itens if i["pot"] is not None and i["pot"] > 1],
                      key=lambda x: x["pot"], reverse=True)[:6]
    resumo = {"total": n, "ideal": cont["lucro_ideal"], "atencao": cont["atencao"],
              "critico": cont["critico"], "margem_media": round(soma / n, 1) if n else 0,
              "piores": piores, "melhores": melhores, "canal": canal}
    return {"resumo": resumo, "itens": itens[:200]}


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


_LOTE_IA_CAMPOS = {
    "titulo": ("nome", "título do anúncio (curto, com material e medida, para busca)"),
    "nome": ("nome", "título do anúncio (curto, com material e medida, para busca)"),
    "descricao": ("descricaoComplementar", "descrição complementar (rica, para busca e conversão)"),
    "descricao_complementar": ("descricaoComplementar", "descrição complementar (rica, para busca e conversão)"),
}


@app.post("/api/produtos/lote/ia")
def lote_ia(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera título OU descrição com IA para vários produtos e (opcional) grava no Bling.
    Body: {produto_ids: [...], campo: 'titulo'|'descricao', aplicar: bool, instrucao?}."""
    ids = payload.get("produto_ids") or []
    campo = (payload.get("campo") or "titulo").strip().lower()
    aplicar = bool(payload.get("aplicar", True))
    instrucao = (payload.get("instrucao") or "").strip()
    if campo not in _LOTE_IA_CAMPOS:
        raise HTTPException(status_code=422, detail="campo deve ser 'titulo' ou 'descricao'.")
    if not ids:
        raise HTTPException(status_code=422, detail="Informe produto_ids.")
    campo_bling, campo_label = _LOTE_IA_CAMPOS[campo]
    ids = ids[:30]  # teto de segurança por lote
    db = SessionLocal()
    resultados = []
    try:
        for pid in ids:
            linha = {"produto_id": pid, "aplicado": False}
            try:
                raw = (bling.obter_produto(user.id, pid) or {}).get("data", {}) or {}
                nome = raw.get("nome") or ""
                atual = raw.get(campo_bling) or ""
                base = atual if atual else f"(vazio — crie do zero a partir do produto: {nome})"
                prompt = (
                    "Você é copywriter de e-commerce para marketplaces brasileiros. "
                    f"Reescreva o campo '{campo_label}' do anúncio, otimizando para busca e conversão, "
                    "mantendo a informação correta. Responda SOMENTE com o texto final, sem comentários.\n"
                    + (f"Instrução: {instrucao}\n" if instrucao else "")
                    + f"Produto: {nome}\nConteúdo atual:\n{base}"
                )
                texto = ai._gerar_texto(user.id, prompt)
                if campo_bling == "nome":
                    texto = " ".join(texto.split())[:150]
                linha["texto"] = texto
                if aplicar:
                    bling.atualizar_produto(user.id, int(pid), {campo_bling: texto})
                    webhooks.registrar_envio(db, user.id, pid, raw.get("codigo"), [campo])
                    linha["aplicado"] = True
            except Exception as e:  # noqa: BLE001 — falha por item não derruba o lote
                linha["erro"] = str(e)[:160]
            resultados.append(linha)
    finally:
        db.close()
    ok = sum(1 for r in resultados if r.get("aplicado"))
    return {"campo": campo, "aplicado": aplicar, "ok": ok, "total": len(resultados), "itens": resultados}
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
