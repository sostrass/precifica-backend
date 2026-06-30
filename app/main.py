import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import ai, agentes, auth, bling, catalogo, decisao, kpis, nfe, precificacao, pricing, qualidade, radar, scraper, shopee, shopee_boost, shopee_boost_auto, shopee_impressao, shopee_promo_auto, shopee_reviews, webhooks
from .config import settings
from .db import run_migrations, SessionLocal, Base, engine, garantir_colunas_extras
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
    os próximos da fila (reimpulsiona quando os 4h de um lote acabam). Quando a
    auto-seleção está ligada, reabastece a fila automática periodicamente."""
    loop = asyncio.get_event_loop()
    ciclos = 0
    while True:
        await asyncio.sleep(600)  # 10 minutos
        ciclos += 1
        try:
            from .models import ShopeeBoostConfig, ShopeeBoostItem
            db = SessionLocal()
            try:
                cfgs = db.query(ShopeeBoostConfig).filter_by(ativo=True).all()
                ativos = [c.user_id for c in cfgs]
                cond = [c.user_id for c in cfgs if getattr(c, "cond_ativo", False)]
                # quem precisa reabastecer a fila automática (a cada ~12h ou se esvaziou)
                reabastecer = []
                for c in cfgs:
                    if not getattr(c, "auto_selecao", False):
                        continue
                    n_auto = (db.query(ShopeeBoostItem)
                              .filter_by(user_id=c.user_id, auto=True).count())
                    if n_auto == 0 or ciclos % 72 == 0:
                        reabastecer.append(c.user_id)
            finally:
                db.close()
            for uid in reabastecer:
                await loop.run_in_executor(None, shopee_boost.auto_selecionar, uid, None)
            for uid in cond:  # boost condicional: fixa ameaçados + roda ciclo
                await loop.run_in_executor(None, shopee_boost_auto.aplicar, uid)
            for uid in ativos:
                if uid in cond:
                    continue  # aplicar já rodou o ciclo deste usuário
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


async def _agendador_reviews():
    """Modo automático das avaliações: a cada hora responde sozinho as notas
    configuradas (ex.: 4 e 5 estrelas), deixando notas baixas para revisão manual."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(3600)  # 1 hora
        try:
            from .models import ShopeeReviewConfig
            db = SessionLocal()
            try:
                autos = [c.user_id for c in db.query(ShopeeReviewConfig).filter_by(modo="auto").all()]
            finally:
                db.close()
            for uid in autos:
                await loop.run_in_executor(None, shopee_reviews.auto_responder, uid)
        except Exception:  # noqa: BLE001 — nunca derruba o app
            pass


async def _agendador_promo():
    """Motor de promoções: roda logo após subir e a cada 30 min. Tira foto das vendas
    a cada ~6h (para detectar queda) e roda o ciclo de quem está em modo automático
    (o auto_ciclo respeita o intervalo configurado internamente)."""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(120)  # primeira passada ~2min após o boot (não espera 6h)
    ticks = 0
    while True:
        try:
            from .models import ShopeePromoConfig, ShopeeConta
            db = SessionLocal()
            try:
                lojas = [c.user_id for c in db.query(ShopeeConta).all()]
                autos = [c.user_id for c in db.query(ShopeePromoConfig)
                         .filter_by(ativo=True, modo="auto").all()]
            finally:
                db.close()
            if ticks % 12 == 0:  # snapshot de vendas ~a cada 6h
                for uid in lojas:
                    await loop.run_in_executor(None, shopee_promo_auto.snapshot_vendas, uid)
            for uid in autos:  # auto_ciclo respeita o intervalo internamente
                await loop.run_in_executor(None, shopee_promo_auto.auto_ciclo, uid)
        except Exception:  # noqa: BLE001 — nunca derruba o app
            pass
        ticks += 1
        await asyncio.sleep(1800)  # 30 min


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    # garante tabelas aditivas — não mexe nas existentes
    # Cria TODAS as tabelas faltantes (checkfirst não toca nas que já existem). Robusto:
    # antes uma lista fixa engolia erros e podia pular tabelas novas (ex.: shopee_sync).
    try:
        from . import models as _modelos  # noqa: F401 — registra todos os modelos no metadata
        Base.metadata.create_all(bind=engine)
    except Exception:  # noqa: BLE001
        pass
    # Rede de segurança: cada tabela nova em seu próprio try — uma falha não trava as outras.
    try:
        from .models import (ProdutoCache, CatalogoSync, ProdutoPrecoSnapshot,
                             ShopeeItemCache, ShopeeSync, VinculosSync, Notificacao)
        for M in (ProdutoCache, CatalogoSync, ProdutoPrecoSnapshot,
                  ShopeeItemCache, ShopeeSync, VinculosSync, Notificacao):
            try:
                M.__table__.create(bind=engine, checkfirst=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    garantir_colunas_extras()  # colunas novas em tabelas já existentes (auto-seleção do boost)
    tarefa = asyncio.create_task(_agendador_radar()) if settings.radar_intervalo_horas > 0 else None
    tarefa_boost = asyncio.create_task(_agendador_boost())
    tarefa_reviews = asyncio.create_task(_agendador_reviews())
    tarefa_promo = asyncio.create_task(_agendador_promo())
    tarefa_cat = asyncio.create_task(_agendador_catalogo()) if settings.catalogo_resync_horas > 0 else None
    yield
    if tarefa:
        tarefa.cancel()
    tarefa_boost.cancel()
    tarefa_reviews.cancel()
    tarefa_promo.cancel()
    if tarefa_cat:
        tarefa_cat.cancel()


app = FastAPI(title="BlingAI Manager — Backend", version="0.2.0", lifespan=lifespan)


def _brl(v) -> str:
    """Formata um número como moeda BR para textos de notificação."""
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.frontend_origin == "*" else [settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def _erro_global(request: Request, exc: Exception):
    """Qualquer erro inesperado volta como JSON legível COM cabeçalho CORS, em vez do
    net::ERR_FAILED opaco que o navegador mostra quando um 500 vem sem CORS."""
    origem = request.headers.get("origin")
    headers = {"Access-Control-Allow-Origin": origem or "*",
               "Access-Control-Allow-Credentials": "true"}
    return JSONResponse(status_code=500,
                        content={"detail": f"Erro interno: {type(exc).__name__}: {exc}"},
                        headers=headers)


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
        # NF-e: aplica o desconto padrão automaticamente quando o modo auto está ligado.
        # Roda aqui (background) porque envolve buscar a nota + PUT de volta no Bling.
        if recurso in ("nfe", "notafiscal", "nota_fiscal", "notafiscaleletronica") and reg.entidade_id:
            cfg_nfe = db.query(NfeConfig).filter_by(user_id=user_id).first()
            try:
                reg.resultado = nfe.processar_evento(user_id, reg.entidade_id, cfg_nfe)
            except Exception:  # noqa: BLE001
                reg.resultado = {"id": str(reg.entidade_id), "ok": False, "motivo": "erro_inesperado"}
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
    """Recebe os eventos do Bling (push). Responde 200 NA HORA e joga TODO o trabalho
    de banco/rede para o threadpool (background) — nada de I/O síncrono no event loop,
    senão o Bling (que dispara muitos webhooks) congela o servidor inteiro."""
    user_id = webhooks.verificar_token(token)
    if not user_id:
        return {"ok": False}
    try:
        corpo = await request.json()
    except Exception:  # noqa: BLE001
        corpo = {}
    background_tasks.add_task(_registrar_e_processar, user_id, corpo)
    return {"ok": True}


def _registrar_e_processar(user_id: int, corpo: dict):
    """Roda no threadpool (fora do event loop): grava o evento e processa em seguida."""
    reg_id = None
    db = SessionLocal()
    try:
        reg = webhooks.registrar_evento(db, user_id, corpo)  # gravação rápida + dedupe
        reg_id = reg.id if reg else None
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    if reg_id:
        _processar_evento_async(user_id, reg_id)


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


@app.post("/api/catalogo/vinculos/enriquecer")
def catalogo_vinculos_enriquecer(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara o mapeamento de canais por produto (lê os vínculos no Bling, ~1 chamada por
    produto). Roda em background; acompanhe por /api/catalogo/vinculos/status."""
    st = catalogo.status_vinculos(user.id)
    if st.get("status") == "rodando":
        return {"ok": True, "ja_rodando": True, **st}
    background_tasks.add_task(catalogo.enriquecer_vinculos, user.id)
    return {"ok": True, "iniciado": True}


@app.get("/api/catalogo/vinculos/status")
def catalogo_vinculos_status(user: User = Depends(auth.get_current_user)):
    return catalogo.status_vinculos(user.id)


@app.post("/api/shopee/catalogo/sincronizar")
def shopee_catalogo_sincronizar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara a sincronização do catálogo da Shopee pro cache (item_id, sku, preço, promoção).
    Roda em background; acompanhe por /api/shopee/catalogo/status."""
    from . import shopee_catalogo
    est = shopee_catalogo.status(user.id)
    if est["status"] == "rodando":
        return {"ok": True, "ja_rodando": True, **est}
    background_tasks.add_task(shopee_catalogo.sincronizar, user.id)
    return {"ok": True, "iniciado": True}


@app.get("/api/shopee/catalogo/status")
def shopee_catalogo_status(user: User = Depends(auth.get_current_user)):
    from . import shopee_catalogo
    return shopee_catalogo.status(user.id)


@app.get("/api/shopee/item")
def shopee_item_lookup(sku: str, user: User = Depends(auth.get_current_user)):
    """Anúncio da Shopee (preço, preço original, promoção) por SKU — lê do cache."""
    from . import shopee_catalogo
    return shopee_catalogo.item_por_sku(user.id, sku) or {"encontrado": False}


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


@app.get("/api/shopee/boost/condicional")
def shopee_boost_cond_get(user: User = Depends(auth.get_current_user)):
    """Config + diagnóstico do boost condicional pelo Radar (quem está ameaçado agora)."""
    cfg = shopee_boost_auto.config(user.id)
    try:
        ev = shopee_boost_auto.avaliar(user.id)
    except shopee.ShopeeError as e:
        ev = {"erro": str(e), "ameacados": []}
    return {"config": cfg, **ev}


@app.put("/api/shopee/boost/condicional")
def shopee_boost_cond_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return shopee_boost_auto.salvar_config(user.id, payload)


@app.post("/api/shopee/boost/condicional/aplicar")
def shopee_boost_cond_aplicar(user: User = Depends(auth.get_current_user)):
    """Avalia e aplica agora: fixa os ameaçados em boost e libera os que saíram da mira."""
    try:
        return shopee_boost_auto.aplicar(user.id, forcar=True)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


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
        if "auto_selecao" in payload: c.auto_selecao = bool(payload["auto_selecao"])
        if "auto_estrategia" in payload: c.auto_estrategia = payload["auto_estrategia"]
        if "auto_maximo" in payload: c.auto_maximo = max(5, min(int(payload["auto_maximo"]), 50))
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/shopee/boost/auto_selecionar")
def shopee_boost_auto_selecionar(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Os agentes escolhem os produtos do boost automaticamente, por estratégia."""
    try:
        return shopee_boost.auto_selecionar(user.id, payload.get("estrategia"))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


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


@app.get("/api/shopee/boost/desempenho")
def shopee_boost_desempenho(user: User = Depends(auth.get_current_user)):
    """Vendas dos últimos 30 dias por produto do boost — pra avaliar se o impulso está
    convertendo. Chama a Shopee (cacheada ~30min), por isso fica fora do status."""
    try:
        vendas = shopee.vendas_por_item(user.id, dias=30)  # {item_id(int): qtd}
    except Exception:  # noqa: BLE001
        vendas = {}
    return {"dias": 30, "vendas": {str(k): int(v) for k, v in (vendas or {}).items()}}


# ---- Avaliações ----
@app.get("/api/shopee/avaliacoes")
def shopee_avaliacoes(status: str = "UNANSWERED", cursor: str = "", item_id: str = "",
                      user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_avaliacoes(user.id, item_id=item_id or None, status=status, cursor=cursor)
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


@app.get("/api/shopee/avaliacoes/config")
def shopee_review_config_get(user: User = Depends(auth.get_current_user)):
    return shopee_reviews.obter_config(user.id)


@app.put("/api/shopee/avaliacoes/config")
def shopee_review_config_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return shopee_reviews.salvar_config(user.id, payload)


@app.get("/api/shopee/impressao/config")
def shopee_impressao_config_get(user: User = Depends(auth.get_current_user)):
    """Config de impressão da conta: dados do emitente + campos visíveis na folha/etiqueta."""
    return shopee_impressao.obter_config(user.id)


@app.put("/api/shopee/impressao/config")
def shopee_impressao_config_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return shopee_impressao.salvar_config(user.id, payload)


@app.post("/api/shopee/avaliacoes/sugerir")
def shopee_review_sugerir(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera um rascunho de resposta no padrão da loja — SEM enviar. Para o modo manual.
    Body: {nota, comentario, produto?, nome?, tom?}."""
    try:
        texto = shopee_reviews.sugerir(
            user.id, payload.get("nota", 5), payload.get("comentario", ""),
            payload.get("produto"), payload.get("nome"), payload.get("tom"))
        return {"texto": texto}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"IA falhou: {e}")


@app.post("/api/shopee/avaliacoes/auto_responder")
def shopee_review_auto(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara o modo automático agora, em SEGUNDO PLANO: responde as avaliações sem
    resposta cujas notas estão em auto_estrelas, com pausa entre cada uma (anti-flood na
    API da Shopee). Roda fora da requisição para não estourar o timeout do gateway."""
    background_tasks.add_task(shopee_reviews.auto_responder, user.id)
    return {"acao": "iniciado",
            "mensagem": "O agente começou a responder em segundo plano, com pausa entre as respostas. "
                        "Atualize a lista em instantes para ver as respostas aparecendo."}


@app.post("/api/shopee/avaliacoes/mutirao")
def shopee_review_mutirao(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Responde a fila INTEIRA de pendentes (notas-alvo), em segundo plano, com pausa
    entre cada resposta e progresso ao vivo (acompanhe em /atividade).
    Body opcional: {completo: true} varre TODOS os produtos para alcançar avaliações antigas."""
    return shopee_reviews.iniciar_mutirao(user.id, completo=bool((payload or {}).get("completo")))


@app.post("/api/shopee/avaliacoes/parar")
def shopee_review_parar(user: User = Depends(auth.get_current_user)):
    """Interrompe o mutirão do agente no próximo item."""
    return shopee_reviews.parar_agente(user.id)


@app.get("/api/shopee/avaliacoes/atividade")
def shopee_review_atividade(user: User = Depends(auth.get_current_user)):
    """Estado vivo do agente: progresso do lote atual, feed das últimas respostas e contagem."""
    return shopee_reviews.atividade(user.id)


@app.post("/api/shopee/avaliacoes/contar")
def shopee_review_contar(background_tasks: BackgroundTasks, forcar: int = 1,
                         user: User = Depends(auth.get_current_user)):
    """Dispara a contagem (respondidas x pendentes) em segundo plano — pagina a Shopee.
    forcar=0 usa o cache (~10min) se houver; forcar=1 recalcula. Resultado em /atividade."""
    background_tasks.add_task(shopee_reviews.contar_avaliacoes, user.id, 60, bool(forcar))
    return {"acao": "contando", "mensagem": "Contando suas avaliações em segundo plano…"}


# ----------------------- Motor de promoções automáticas ------------------- #
@app.get("/api/shopee/promo/config")
def shopee_promo_config_get(user: User = Depends(auth.get_current_user)):
    return shopee_promo_auto.obter_config(user.id)


@app.put("/api/shopee/promo/config")
def shopee_promo_config_put(background_tasks: BackgroundTasks, payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    antes = shopee_promo_auto.obter_config(user.id)
    cfg = shopee_promo_auto.salvar_config(user.id, payload)
    # Acabou de ligar o modo automático? roda um ciclo na hora (em segundo plano),
    # pra "selecionar auto" já produzir ação — depois o agendador mantém a cadência.
    virou_auto = cfg.get("ativo") and cfg.get("modo") == "auto" and (
        not antes.get("ativo") or antes.get("modo") != "auto")
    if virou_auto:
        background_tasks.add_task(shopee_promo_auto.auto_ciclo_forcado, user.id)
    return {**cfg, "disparo_imediato": bool(virou_auto)}


@app.post("/api/shopee/promo/diagnosticar")
def shopee_promo_diagnosticar(user: User = Depends(auth.get_current_user)):
    """Testa anexar 1 produto a um desconto e devolve as respostas CRUAS da Shopee,
    pra revelar o motivo exato do '0 produtos'. Apaga o desconto de teste no fim."""
    try:
        return shopee_promo_auto.diagnosticar_desconto(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/diagnosticar-flash")
def shopee_promo_diagnosticar_flash(user: User = Depends(auth.get_current_user)):
    """Testa criar 1 oferta relâmpago (Flash Sale da loja) e devolve as respostas CRUAS da
    Shopee, revelando se a loja tem slots/elegibilidade. Apaga a oferta de teste no fim."""
    try:
        return shopee_promo_auto.diagnosticar_flash(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/propor")
def shopee_promo_propor(user: User = Depends(auth.get_current_user)):
    """Monta as propostas de promoção (não cria nada) — o agente escolhe os produtos
    e calcula o desconto seguro dentro das regras."""
    try:
        return shopee_promo_auto.propor(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/rodar")
def shopee_promo_rodar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Roda o agente AGORA e APLICA as promoções (sem aprovação), em segundo plano.
    Para o modo automático: o usuário clica e o agente aplica sozinho, dentro das travas."""
    background_tasks.add_task(shopee_promo_auto.aplicar_agora, user.id)
    return {"acao": "iniciado",
            "mensagem": "O agente está montando e aplicando as promoções em segundo plano. "
                        "Confira o histórico em instantes."}


@app.post("/api/shopee/promo/aplicar")
def shopee_promo_aplicar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Cria as promoções a partir das propostas aprovadas. Body: {propostas, tipo?}."""
    propostas = payload.get("propostas") or []
    if not propostas:
        raise HTTPException(status_code=422, detail="Envie as propostas a aplicar.")
    try:
        return shopee_promo_auto.aplicar(user.id, propostas, payload.get("tipo"), "manual")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/promo/queda")
def shopee_promo_queda(user: User = Depends(auth.get_current_user)):
    """Estado da detecção de queda de vendas (linha de base x atual)."""
    return shopee_promo_auto.detectar_queda(user.id)


@app.get("/api/shopee/promo/historico")
def shopee_promo_historico(user: User = Depends(auth.get_current_user)):
    return {"itens": shopee_promo_auto.historico(user.id)}


# ---- Pedidos & financeiro ----
@app.get("/api/shopee/pedidos")
def shopee_pedidos(dias: int = 7, cursor: str = "", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_pedidos(user.id, dias=dias, cursor=cursor)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/{order_sn}/detalhe")
def shopee_pedido_detalhe(order_sn: str, user: User = Depends(auth.get_current_user)):
    """Detalhe completo de um pedido (produtos com margem real + repasse + comprador + logística)."""
    try:
        return shopee.pedido_detalhe(user.id, order_sn)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/painel")
def shopee_pedidos_painel(status: str = "A_ENVIAR", dias: int = 15, page: int = 1, page_size: int = 20,
                          busca: str = "", busca_tipo: str = "tudo", grupo: str = "todos", nf: str = "todos",
                          user: User = Depends(auth.get_current_user)):
    """Pedidos enriquecidos + análise de valor (pago x preço de tabela), PAGINADO.
    Filtros: status (Meus Pedidos), grupo (aberto/concluído), nf (situação da NF), busca+busca_tipo."""
    try:
        return shopee.pedidos_painel(user.id, status=status, dias=dias, page=page, page_size=page_size,
                                     busca=busca, busca_tipo=busca_tipo, grupo=grupo, nf=nf)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/contagens")
def shopee_pedidos_contagens(dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Contadores por status (selos das abas) — chamada barata, só lista de SNs."""
    try:
        return shopee.contagens_status(user.id, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/contagens-nf")
def shopee_pedidos_contagens_nf(status: str = "TODOS", dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Contadores por situação de NF (Bling) + selos por pedido. Chamado de forma assíncrona."""
    try:
        return shopee.contagens_nf(user.id, status=status, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/separacao")
def shopee_pedidos_separacao(status: str = "A_ENVIAR", dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Lista de separação (produtos em ordem alfabética + quantidade total)."""
    try:
        return shopee.lista_separacao(user.id, status=status, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/pedidos/enriquecer-impressao")
def shopee_pedidos_enriquecer_impressao(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Enriquece os pedidos selecionados para impressão (etiqueta/folha): rastreio +
    NF-e (casada por numeroPedidoLoja) + descrição complementar por SKU.
    Body: {order_sns:[...], skus:[...]}. Retorna {patches:{order_sn:{...}}, complementos:{sku:texto}}."""
    try:
        return shopee.enriquecer_impressao(user.id,
                                           payload.get("order_sns") or [],
                                           payload.get("skus") or [])
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/pedidos/etiqueta-oficial")
def shopee_etiqueta_oficial(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera e baixa o waybill OFICIAL da Shopee (PDF) para os pedidos selecionados.
    Fluxo: create_shipping_document -> get_result (poll) -> download. Conta de vendedor
    precisa estar validada. Body: {order_sns:[...], tipo?:'auto'}. Retorna o PDF (application/pdf)."""
    order_sns = payload.get("order_sns") or []
    if not order_sns:
        raise HTTPException(status_code=400, detail="Informe ao menos um pedido.")
    try:
        pdf = shopee.gerar_etiqueta_oficial(user.id, order_sns, payload.get("tipo") or "auto")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    nome = "etiqueta-shopee.pdf" if len(order_sns) == 1 else f"etiquetas-shopee-{len(order_sns)}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nome}"'})


@app.get("/api/shopee/financeiro/margem-real")
def shopee_margem_real(dias: int = 7, limite: int = 40, user: User = Depends(auth.get_current_user)):
    """Margem líquida REAL por pedido: repasse da Shopee × custo do produto. Caro — cacheado ~20min."""
    try:
        return shopee.margem_real(user.id, dias=dias, limite_pedidos=limite)
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
        itens = payload.get("itens", [])
        res = shopee.criar_desconto(user.id, payload["nome"], int(payload["inicio"]),
                                    int(payload["fim"]), itens)
        if itens and not res.get("itens_adicionados"):
            motivo = (res.get("item_erros") or ["nenhum produto elegível entrou"])[0]
            raise HTTPException(status_code=502,
                detail=f"O desconto foi criado, mas SEM produtos: {motivo}. "
                       "Verifique se os anúncios estão ativos e elegíveis a desconto.")
        return res
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe nome, inicio, fim e itens.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanhas/agenda")
def shopee_campanhas_agenda(user: User = Depends(auth.get_current_user)):
    """Todas as campanhas (todos os tipos) normalizadas pra visão geral / timeline."""
    try:
        return shopee.agenda_campanhas(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanhas/dashboard")
def shopee_campanhas_dashboard(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Receita gerada por promoções no período (uma varredura, atribuída por promotion_id). Cacheado."""
    try:
        return shopee.dashboard_promo(user.id, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanha/{tipo}/{cid}/desempenho")
def shopee_campanha_desempenho(tipo: str, cid: str, user: User = Depends(auth.get_current_user)):
    """Vendas dos produtos da campanha no período dela (pedidos × produtos). Caro — cacheado ~10min."""
    try:
        return shopee.desempenho_campanha(user.id, tipo, cid)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/campanha/{tipo}/{cid}/repetir")
def shopee_campanha_repetir(tipo: str, cid: str, user: User = Depends(auth.get_current_user)):
    """Recria a campanha igual, com novo período (mesma duração, começando em ~5 min)."""
    try:
        return shopee.repetir_campanha(user.id, tipo, cid)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanha/{tipo}/{cid}")
def shopee_campanha_detalhe(tipo: str, cid: str, user: User = Depends(auth.get_current_user)):
    """Detalhe de uma campanha COM os produtos (nome, imagem, preço de/por).
    tipo: desconto | bundle | addon | flash."""
    fn = {"desconto": shopee.detalhe_desconto, "bundle": shopee.detalhe_bundle,
          "addon": shopee.detalhe_addon, "flash": shopee.detalhe_flash}.get(tipo)
    if not fn:
        raise HTTPException(status_code=404, detail="Tipo de campanha desconhecido.")
    try:
        return fn(user.id, cid)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
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
    """Margem real de cada anúncio Shopee × custo do Bling (por SKU): prejuízo, margem baixa
    ou saudável, com preço de equilíbrio e preço para a margem alvo."""
    try:
        return shopee.divergencia_bling_shopee(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/item/preco")
def shopee_item_preco(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajusta o preço de um anúncio na Shopee (todas as variações). Body: {item_id, preco}."""
    try:
        return shopee.atualizar_preco_item(user.id, payload["item_id"], payload["preco"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe item_id e preco.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/reprecificar")
def shopee_reprecificar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajuste de preço em lote na Shopee para a margem alvo. dry-run por padrão (mostra o plano).
    Body: { item_ids?: [int], aplicar: bool }. Respeita piso e pula SKUs em competição/sem custo."""
    item_ids = payload.get("item_ids")
    aplicar = bool(payload.get("aplicar", False))
    try:
        return shopee.reprecificar_shopee(user.id, item_ids=item_ids, aplicar=aplicar)
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
        ids = payload.get("item_ids", [])
        res = shopee.criar_bundle(user.id, payload["nome"], int(payload["inicio"]),
                                  int(payload["fim"]), int(payload["rule_type"]),
                                  float(payload["valor"]), int(payload.get("min_itens", 2)), ids)
        if ids and not res.get("itens_adicionados"):
            motivo = (res.get("item_erros") or ["nenhum produto entrou no combo"])
            motivo = motivo[0] if isinstance(motivo[0], str) else str(motivo[0])
            raise HTTPException(status_code=502,
                detail=f"O combo foi criado, mas SEM produtos: {motivo}. "
                       "Bundles exigem produtos elegíveis e regra válida (ex.: mínimo 2 itens).")
        return res
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
        principais = payload.get("principais", [])
        res = shopee.criar_addon(user.id, payload["nome"], int(payload["inicio"]),
                                 int(payload["fim"]), principais,
                                 payload.get("adicionais", []), int(payload.get("promotion_type", 0)))
        if principais and not res.get("principais_ok"):
            motivo = (res.get("item_erros") or ["o produto principal não pôde ser adicionado"])[0]
            raise HTTPException(status_code=502,
                detail=f"O add-on foi criado, mas sem o produto principal: {motivo}. "
                       "Verifique se o anúncio principal está ativo e elegível.")
        return res
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
        if "precoCusto" in campos:  # reflete o novo custo no cache local (sem esperar o webhook)
            from .models import ProdutoCache
            pc = db.query(ProdutoCache).filter_by(user_id=user.id, produto_id=str(produto_id)).first()
            if pc:
                pc.custo = float(campos["precoCusto"] or 0)
                db.commit()
        if "preco" in campos:  # reflete o novo Preço Bling no cache local (sem esperar o webhook)
            from .models import ProdutoCache
            pc = db.query(ProdutoCache).filter_by(user_id=user.id, produto_id=str(produto_id)).first()
            if pc:
                pc.preco = float(campos["preco"] or 0)
                db.commit()
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
    # Shopee: temos API direta — o cache da Shopee é a fonte da verdade do "publicado",
    # independente do vínculo do Bling (que a v3 não devolve com preço).
    shopee_cache = None
    sku_prod = raw.get("codigo")
    if sku_prod:
        try:
            from .models import ShopeeItemCache
            with SessionLocal() as _db:
                shopee_cache = (_db.query(ShopeeItemCache)
                                .filter_by(user_id=user.id, sku=sku_prod)
                                .order_by(ShopeeItemCache.atualizado_em.desc()).first())
        except Exception:  # noqa: BLE001
            shopee_cache = None
    # Mercado Livre: API direta (igual à Shopee). Dormente até as credenciais existirem —
    # configurado() é False sem ML_CLIENT_ID/SECRET/REFRESH_TOKEN, então isto vira no-op.
    ml_item = None
    if sku_prod:
        try:
            from . import mercadolivre as _ml
            if _ml.configurado():
                ml_item = _ml.buscar_item_por_sku(sku_prod)
        except Exception:  # noqa: BLE001
            ml_item = None
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

    # Painel por canal: TODOS os canais ativos do config, com líquido realizado + status,
    # mesclando os vínculos existentes. Alimenta a tabela e os badges do cockpit.
    vinc_por_canal = {}
    for v in vinc:
        cn = v.get("canal")
        if not cn:
            continue
        cur = vinc_por_canal.get(cn)
        # com duas lojas no mesmo canal (ex.: duas Shopee), fica com a que tem anúncio/preço
        if cur is None or (not (cur.get("id_anuncio") or cur.get("preco")) and (v.get("id_anuncio") or v.get("preco"))):
            vinc_por_canal[cn] = v

    def _liq_canal(faixas, preco):
        if not preco or preco <= 0:
            return None
        fx = precificacao._faixa_para_preco(faixas, preco) if faixas else {}
        taxa = preco * (float(fx.get("comissao") or 0) + float(fx.get("fixo_pct") or 0)) / 100.0 + float(fx.get("fixo") or 0)
        return round(preco - taxa - preco * float(cfg.get("imposto") or 0) / 100.0 - float(cfg.get("embalagem") or 0), 2)

    # id_loja por canal a partir das lojas conhecidas da conta. É o que garante que o
    # "Aplicar" SEMPRE grava no Bling (hub/fonte da verdade), mesmo quando a v3 não
    # devolve o vínculo com preço — senão a próxima sincronização propaga o preço antigo.
    lojas_por_canal = {}
    try:
        for lj, meta in bling.lojas_da_conta(user.id).items():
            cn_l = bling.MAPA_INTEGRACAO.get((meta.get("integracao") or "").lower())
            if cn_l and cn_l not in lojas_por_canal:
                lojas_por_canal[cn_l] = lj
    except Exception:  # noqa: BLE001
        lojas_por_canal = {}

    canais_painel = []
    cobertos = set()
    for c in (cfg.get("canais") or []):
        cn = c.get("canal")
        v = vinc_por_canal.get(cn)
        # publicado = o produto está vinculado/anunciado nesse canal (independe de termos o preço)
        publicado = bool(v and (v.get("publicado") or v.get("id_anuncio") or v.get("ativo") or v.get("preco")))
        preco_reg = float(v["preco"]) if (v and v.get("preco") and v["preco"] > 0) else None
        item_id = None
        # Shopee: cache direto manda. Marca publicado e completa preço/item_id.
        if cn == "shopee" and shopee_cache:
            publicado = True
            item_id = shopee_cache.item_id
            if preco_reg is None:
                cp = float(shopee_cache.preco_original or 0) or float(shopee_cache.preco or 0)
                if cp > 0:
                    preco_reg = cp
        # Mercado Livre: API direta. Marca publicado e completa preço/anúncio.
        if cn == "mercadolivre" and ml_item:
            publicado = True
            if not item_id:
                item_id = ml_item.get("item_id")
            if preco_reg is None:
                mp = float(ml_item.get("preco_original") or 0) or float(ml_item.get("preco") or 0)
                if mp > 0:
                    preco_reg = mp
        # mostra o canal se está ativo na config OU se o produto já está anunciado nele
        if not c.get("ativo") and not publicado:
            continue
        liquido = _liq_canal(c.get("faixas") or [], preco_reg) if preco_reg else None
        if not publicado:
            status = "falta_anunciar"
        elif preco_reg is None or liquido is None:
            status = "sem_preco"
        elif liquido < 0:
            status = "prejuizo"
        elif base > 0 and liquido < base - 0.01:
            status = "abaixo"
        else:
            status = "no_padrao"
        canais_painel.append({
            "canal": cn, "nome": c.get("nome") or cn, "publicado": publicado,
            "preco_registrado": preco_reg, "preco_alvo": alvo_por_canal.get(cn),
            "liquido": liquido, "status": status, "ativo_cfg": bool(c.get("ativo")),
            "id_loja": (v.get("id_loja") if v else None) or lojas_por_canal.get(cn),
            "id_anuncio": (v.get("id_anuncio") if v else None) or item_id,
            "item_id": item_id,
        })
        cobertos.add(cn)

    # Canais onde o produto ESTÁ anunciado mas nem constam na config de precificação.
    # Aparecem como publicados (pra não dizer "Anunciar" indevidamente); o líquido fica
    # pendente até cadastrar as taxas do canal na configuração.
    NOMES_CANAL = {"mercadolivre": "Mercado Livre", "shopee": "Shopee", "shein": "Shein",
                   "tiktok": "TikTok", "nuvemshop": "Nuvemshop", "amazon": "Amazon",
                   "magalu": "Magalu", "americanas": "Americanas", "tray": "Tray", "shopify": "Shopify"}
    for v in vinc:
        cn = v.get("canal") or ((v.get("integracao") or "").strip().lower() or None)
        if not cn or cn in cobertos:
            continue
        if not (v.get("publicado") or v.get("id_anuncio") or v.get("ativo") or v.get("preco")):
            continue
        preco_reg = float(v["preco"]) if (v.get("preco") and v["preco"] > 0) else None
        canais_painel.append({
            "canal": cn, "nome": NOMES_CANAL.get(cn) or v.get("nome") or cn,
            "publicado": True, "preco_registrado": preco_reg,
            "preco_alvo": alvo_por_canal.get(cn), "liquido": None,
            "status": "sem_preco" if preco_reg is None else "sem_taxas", "ativo_cfg": False,
            "id_loja": v.get("id_loja") or lojas_por_canal.get(cn),
            "id_anuncio": v.get("id_anuncio"), "item_id": None,
        })
        cobertos.add(cn)

    return {"produto_id": raw.get("id"), "sku": raw.get("codigo"),
            "base_venda": round(base, 2), "canais": canais,
            "canais_painel": canais_painel,
            "vinculos": vinculos, "fonte_lida": bool(vinc)}


@app.get("/api/produtos/{produto_id}/simular")
def produto_simular_liquido(produto_id, canal: str, preco: float,
                            user: User = Depends(auth.get_current_user)):
    """Simula o líquido e a margem de um preço hipotético num canal (sem gravar nada).
    Usa as faixas/taxas do canal na config + imposto + embalagem + custo do produto.
    Alimenta o cálculo ao vivo da Promoção e da edição manual por canal."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    cfg = precificacao.obter_config(user.id)
    base = float(raw.get("preco") or 0)
    custo = float(raw.get("precoCusto") or (raw.get("fornecedor") or {}).get("precoCusto") or 0)
    canal_cfg = next((c for c in (cfg.get("canais") or []) if c.get("canal") == canal), None)
    faixas = (canal_cfg or {}).get("faixas") or []
    if not preco or preco <= 0:
        return {"canal": canal, "preco": preco, "liquido": None, "margem": None,
                "taxa": None, "custo": round(custo, 2), "alvo": round(base, 2), "abaixo_alvo": False}
    fx = precificacao._faixa_para_preco(faixas, preco) if faixas else {}
    fx = fx or {}
    comissao_pct = float(fx.get("comissao") or 0) + float(fx.get("fixo_pct") or 0)
    taxa_fixa = float(fx.get("fixo") or 0)
    taxa = preco * comissao_pct / 100.0 + taxa_fixa
    imp_pct = float(cfg.get("imposto") or 0)
    imp_val = preco * imp_pct / 100.0
    emb = float(cfg.get("embalagem") or 0)
    liquido = round(preco - taxa - imp_val - emb, 2)
    margem = round((liquido - custo) / liquido * 100, 1) if (custo > 0 and liquido) else None
    # quebra do líquido (cascata "anatomia do líquido"): cada dedução do preço de lista
    quebra = []
    if taxa:
        rot = f"Taxa {canal} {comissao_pct:.0f}%".strip() if comissao_pct else "Taxa fixa do canal"
        quebra.append({"rotulo": rot, "valor": -round(taxa, 2)})
    if imp_val:
        quebra.append({"rotulo": f"Imposto {imp_pct:.0f}%", "valor": -round(imp_val, 2)})
    if emb:
        quebra.append({"rotulo": "Embalagem / custo fixo", "valor": -round(emb, 2)})
    return {"canal": canal, "preco": round(preco, 2), "liquido": liquido, "taxa": round(taxa, 2),
            "custo": round(custo, 2), "margem": margem, "alvo": round(base, 2),
            "lucro": round(liquido - custo, 2) if custo > 0 else None,
            "imposto_pct": imp_pct, "comissao_pct": comissao_pct,
            "quebra": quebra, "abaixo_alvo": bool(base > 0 and liquido < base - 0.01),
            "tem_faixas": bool(faixas)}


@app.get("/api/produtos/{produto_id}/qualidade")
def produto_qualidade(produto_id, user: User = Depends(auth.get_current_user)):
    """Diagnóstico de qualidade do anúncio: funde o anúncio da Shopee (fotos, vídeo,
    descrição) com a ficha do Bling (título, EAN, NCM, peso). Devolve nota 0-100,
    componentes com status e um plano priorizado. Tudo dado real — nada inventado."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    def _dig(v):
        return "".join(c for c in str(v or "") if c.isdigit())

    nome = (raw.get("nome") or "").strip()
    sku = raw.get("codigo")
    ean = raw.get("gtin") or raw.get("gtinEmbalagem") or ""
    ncm = raw.get("ncm") or (raw.get("tributacao") or {}).get("ncm") or ""
    peso = float(raw.get("pesoBruto") or raw.get("pesoLiquido") or 0)
    desc_bling = (raw.get("descricaoCurta") or raw.get("descricaoComplementar")
                  or raw.get("observacoes") or "")

    # --- anúncio Shopee (fotos, vídeo, descrição, título) ---
    fotos = None
    tem_video = False
    desc_shopee = ""
    titulo_shopee = ""
    tem_shopee = False
    if sku:
        try:
            from .models import ShopeeItemCache
            with SessionLocal() as _db:
                cache = (_db.query(ShopeeItemCache)
                         .filter_by(user_id=user.id, sku=sku)
                         .order_by(ShopeeItemCache.atualizado_em.desc()).first())
            if cache and cache.item_id:
                info = shopee.info_itens(user.id, [cache.item_id])
                lst = ((info.get("response") or {}).get("item_list") or [])
                if lst:
                    it = lst[0]
                    tem_shopee = True
                    img = (it.get("image") or {})
                    fotos = len(img.get("image_url_list") or img.get("image_id_list") or [])
                    vid = it.get("video_info") or it.get("video_upload_id") or []
                    tem_video = bool(vid)
                    desc_shopee = it.get("description") or ""
                    titulo_shopee = it.get("item_name") or ""
        except Exception:  # noqa: BLE001
            tem_shopee = False

    titulo = titulo_shopee or nome
    desc = desc_shopee if len(desc_shopee) >= len(desc_bling) else desc_bling
    len_tit = len(titulo)
    len_desc = len(desc)

    componentes = []
    score = 0.0

    # Título (peso 20)
    if len_tit >= 40:
        s, st = 20, "ok"
    elif len_tit >= 30:
        s, st = 15, "ok"
    elif len_tit >= 15:
        s, st = 8, "atencao"
    else:
        s, st = 0, "falta"
    score += s
    componentes.append({"chave": "titulo", "label": "Título", "valor": s, "max": 20, "status": st,
                        "detalhe": f"{len_tit} caracteres" + (" · com palavras-chave" if len_tit >= 40 else ""),
                        "acao": None if st == "ok" else "Use 40+ caracteres com material e medida."})

    # Fotos (peso 25) — só Shopee tem essa info
    if tem_shopee and fotos is not None:
        s = round(25 * min(fotos, 9) / 9)
        st = "ok" if fotos >= 9 else ("atencao" if fotos >= 4 else "falta")
        det = f"{fotos}/9 fotos"
        ac = None if fotos >= 9 else f"Adicione mais {9 - fotos} — anúncios com 9 fotos vendem mais."
    else:
        s, st, det, ac = 0, "sem_dados", "sem anúncio Shopee", "Vincule/sincronize a Shopee pra ler as fotos."
    score += s
    componentes.append({"chave": "fotos", "label": "Fotos", "valor": s, "max": 25, "status": st,
                        "detalhe": det, "acao": ac, "fotos": fotos})

    # Atributos da ficha (peso 20): EAN + NCM + peso
    faltam = []
    if len(_dig(ean)) not in (8, 12, 13, 14):
        faltam.append("EAN/GTIN")
    if len(_dig(ncm)) != 8:
        faltam.append("NCM")
    if peso <= 0:
        faltam.append("peso")
    ok_attr = 3 - len(faltam)
    s = round(20 * ok_attr / 3)
    st = "ok" if not faltam else ("atencao" if ok_attr >= 1 else "falta")
    score += s
    componentes.append({"chave": "atributos", "label": "Atributos", "valor": s, "max": 20, "status": st,
                        "detalhe": "completos" if not faltam else f"faltam {len(faltam)}: {', '.join(faltam)}",
                        "acao": None if not faltam else f"Preencha: {', '.join(faltam)}."})

    # Descrição (peso 20)
    if len_desc >= 200:
        s, st = 20, "ok"
    elif len_desc >= 80:
        s, st = 12, "atencao"
    else:
        s, st = 0, "falta"
    score += s
    componentes.append({"chave": "descricao", "label": "Descrição", "valor": s, "max": 20, "status": st,
                        "detalhe": f"{len_desc} caracteres" + (" · completa" if len_desc >= 200 else ""),
                        "acao": None if st == "ok" else "Descreva medidas e benefícios (200+ caracteres)."})

    # Vídeo (peso 15)
    if tem_video:
        s, st = 15, "ok"
    elif tem_shopee:
        s, st = 0, "falta"
    else:
        s, st = 0, "sem_dados"
    score += s
    componentes.append({"chave": "video", "label": "Vídeo", "valor": s, "max": 15, "status": st,
                        "detalhe": "tem vídeo" if tem_video else ("sem vídeo" if tem_shopee else "sem anúncio Shopee"),
                        "acao": None if tem_video else ("Anúncio com vídeo converte mais e aparece no feed." if tem_shopee else None)})

    score = int(round(score))
    if score >= 85:
        label = "Excelente"
    elif score >= 70:
        label = "Bom — dá pra subir pra Excelente"
    elif score >= 50:
        label = "Regular — vale completar"
    else:
        label = "Precisa de atenção"

    # plano priorizado (maior ganho primeiro)
    plano = sorted([c for c in componentes if c["status"] in ("falta", "atencao", "sem_dados") and c.get("acao")],
                   key=lambda c: (c["max"] - c["valor"]), reverse=True)
    plano = [{"label": c["label"], "acao": c["acao"], "ganho": c["max"] - c["valor"]} for c in plano]
    potencial = min(100, score + sum(p["ganho"] for p in plano))

    return {"score": score, "label": label, "potencial": potencial,
            "tem_shopee": tem_shopee, "componentes": componentes, "plano": plano}


@app.post("/api/catalogo/kpi-snapshot")
def catalogo_kpi_snapshot(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Upsert da foto de hoje dos KPIs do catálogo (1 linha por dia). O front manda os
    números já calculados; aqui só guardamos pra alimentar tendência/sparkline."""
    from datetime import date as _date
    from .models import KpiSnapshot

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if _i(payload.get("total")) <= 0:
        return {"ok": False, "motivo": "catálogo ainda não carregado"}
    hoje = _date.today()
    with SessionLocal() as db:
        row = db.query(KpiSnapshot).filter_by(user_id=user.id, dia=hoje).first()
        if not row:
            row = KpiSnapshot(user_id=user.id, dia=hoje)
            db.add(row)
        row.total = _i(payload.get("total"))
        row.saudavel = _i(payload.get("saud"))
        row.atencao = _i(payload.get("aten"))
        row.prejuizo = _i(payload.get("prej"))
        row.sem_custo = _i(payload.get("semCusto"))
        row.val_estoque = float(payload.get("valEstoque") or 0)
        row.marg_media = _f(payload.get("margMedia"))
        row.cobertura = payload.get("cobertura") or []
        row.criado_em = datetime.utcnow()
        db.commit()
    return {"ok": True, "dia": hoje.isoformat()}


@app.get("/api/catalogo/kpi-historico")
def catalogo_kpi_historico(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Série diária dos KPIs pra desenhar tendência/sparkline no topo do Catálogo."""
    from datetime import date as _date, timedelta as _td
    from .models import KpiSnapshot
    desde = _date.today() - _td(days=int(dias))
    with SessionLocal() as db:
        rows = (db.query(KpiSnapshot)
                .filter(KpiSnapshot.user_id == user.id, KpiSnapshot.dia >= desde)
                .order_by(KpiSnapshot.dia.asc()).all())
        pontos = [{"dia": r.dia.isoformat(), "total": r.total, "saud": r.saudavel,
                   "aten": r.atencao, "prej": r.prejuizo, "sem_custo": r.sem_custo,
                   "val_estoque": r.val_estoque, "marg_media": r.marg_media,
                   "cobertura": r.cobertura} for r in rows]
    return {"pontos": pontos}


@app.get("/api/mercadolivre/status")
def mercadolivre_status(user: User = Depends(auth.get_current_user)):
    """Status da integração direta com o Mercado Livre (mesma ideia da Shopee)."""
    from . import mercadolivre as ml
    return {"configurado": ml.configurado()}


@app.get("/api/mercadolivre/item")
def mercadolivre_item(sku: str, user: User = Depends(auth.get_current_user)):
    """Lê o anúncio do Mercado Livre por SKU (preço/status), via API direta do ML."""
    from . import mercadolivre as ml
    if not ml.configurado():
        return {"configurado": False, "item": None}
    try:
        return {"configurado": True, "item": ml.buscar_item_por_sku(sku)}
    except ml.MLNaoConfigurado:
        return {"configurado": False, "item": None}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")


@app.get("/api/produtos/{produto_id}/preco_historico")
def produto_preco_historico(produto_id, dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Histórico do Preço Bling por dia (gravado a cada sync). Alimenta o gráfico do cockpit."""
    from .models import ProdutoPrecoSnapshot
    from datetime import date, timedelta
    limite = date.today() - timedelta(days=max(1, min(int(dias or 30), 180)))
    db = SessionLocal()
    try:
        rows = (db.query(ProdutoPrecoSnapshot)
                .filter(ProdutoPrecoSnapshot.user_id == user.id,
                        ProdutoPrecoSnapshot.produto_id == str(produto_id),
                        ProdutoPrecoSnapshot.dia >= limite)
                .order_by(ProdutoPrecoSnapshot.dia.asc()).all())
        pontos = [{"dia": r.dia.isoformat() if r.dia else None, "preco": float(r.preco or 0)} for r in rows]
    finally:
        db.close()
    return {"produto_id": str(produto_id), "pontos": pontos}


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
    from . import notificacoes as notif
    notif.criar(user.id, "precificacao", f"Preço atualizado: {_brl(preco)}",
                f"Produto {produto_id} no canal {id_loja}.", ok=True,
                modulo="precificacao", entidade_id=produto_id)
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


@app.get("/api/marketplaces/capacidades")
def marketplaces_capacidades(user: User = Depends(auth.get_current_user)):
    """O que dá pra monitorar em cada marketplace (ML, Shopee, TikTok, Shein) e como ativar."""
    return scraper.capacidades()


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
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
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
    if aplicar:
        aplicados = sum(1 for r in resultados if r.get("aplicado"))
        if aplicados:
            from . import notificacoes as notif
            notif.criar(user.id, "precificacao", f"{aplicados} preço(s) reprecificado(s) em lote",
                        f"Canal {canal}. Confira os novos preços.", ok=True, modulo="precificacao")
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


def _img_do_payload(dados):
    """Primeira imagem do payload bruto do Bling (lista ou GET)."""
    if not isinstance(dados, dict):
        return None
    ext = (((dados.get("midia") or {}).get("imagens") or {}).get("externas") or [])
    for i in ext:
        if isinstance(i, dict) and i.get("link"):
            return i["link"]
    return dados.get("imagemURL") or None


def _marketplaces_do_payload(dados):
    """Canais onde o produto está anunciado (vinculosLojas, se vier no payload)."""
    if not isinstance(dados, dict):
        return []
    for chave in ("vinculosLojas", "lojas", "produtosLojas"):
        arr = dados.get(chave)
        if isinstance(arr, list) and arr:
            vins = bling.parse_vinculos_multiloja(arr)
            return [{"canal": v.get("canal"), "nome": v.get("nome"), "publicado": bool(v.get("publicado"))}
                    for v in vins if v.get("canal")]
    return []


@app.post("/api/monitoramento")
def monitoramento(payload: dict = Body(default={}),
                  user: User = Depends(auth.get_current_user)):
    """Catálogo no modelo LÍQUIDO. O preço do Bling é o líquido que o lojista quer
    receber. Por canal de marketplace, calcula o preço de LISTA que neta o Preço Bling
    (gross-up pelas taxas daquele canal). Canal 'bling' (padrão) = só o Preço Bling +
    margem real vs custo. Lê do cache local (todos os produtos); se vazio, cai pro
    Bling ao vivo (limitado). Enriquece com imagem, marketplaces e vendas 30d."""
    from .models import ProdutoCache
    canal = payload.get("canal", "bling")
    cfg = precificacao.obter_config(user.id)
    canal_cfg = precificacao._canal_cfg(cfg, canal) if (canal and canal != "bling") else None

    # Preço praticado por canal: líquido que aquele preço de lista neta, e status vs Preço Bling (alvo).
    canais_cfg = {c.get("canal"): c for c in (cfg.get("canais") or []) if c.get("canal")}
    _imp = float(cfg.get("imposto") or 0)
    _emb = float(cfg.get("embalagem") or 0)

    def _status_canal(cn, preco_lista, preco_bling, custo):
        if not preco_lista or preco_lista <= 0:
            return None, None
        ccfg = canais_cfg.get(cn)
        fx = precificacao._faixa_para_preco(ccfg.get("faixas") or [], preco_lista) if (ccfg and ccfg.get("faixas")) else {}
        taxa = preco_lista * (float(fx.get("comissao") or 0) + float(fx.get("fixo_pct") or 0)) / 100.0 + float(fx.get("fixo") or 0)
        liq = round(preco_lista - taxa - preco_lista * _imp / 100.0 - _emb, 2)
        if custo and custo > 0 and liq <= custo:
            flag = "prejuizo"
        elif preco_bling and liq < preco_bling - 0.01:
            flag = "abaixo"
        elif preco_bling and liq > preco_bling + 0.01:
            flag = "acima"
        else:
            flag = "ok"
        return liq, flag

    def _monta(produto_id, sku, nome, preco_bling, custo, estoque, atualizado, imagem=None, marketplaces=None, vendas_un=0):
        margem = round((preco_bling - custo) / preco_bling * 100, 1) if (preco_bling > 0 and custo > 0) else None
        if preco_bling <= 0:
            status = "sem_base"
        elif custo <= 0:
            status = "sem_custo"
        elif preco_bling <= custo:
            status = "critico"            # netando abaixo do custo
        elif margem is not None and margem < 10:
            status = "atencao"
        else:
            status = "lucro_ideal"
        pra_netar = None
        if canal_cfg and preco_bling > 0:
            sug = precificacao.precificar_venda_canal(
                preco_bling, canal_cfg["faixas"], cfg["imposto"], cfg["cartao"], cfg["embalagem"])
            pra_netar = sug["preco"] if sug else None
        return {
            "id": produto_id, "sku": sku, "nome": nome,
            "custo": custo, "estoque": estoque,
            "imagem": imagem, "marketplaces": marketplaces or [], "vendas": vendas_un,
            "preco_bling": preco_bling, "pra_netar": pra_netar,
            "margem_real": margem, "status": status,
            "atualizado": atualizado.isoformat() if atualizado else None,
            # compat com as chaves antigas que o front ainda lê
            "preco_atual": preco_bling, "preco_sugerido": pra_netar, "margem_liquida": margem or 0,
        }

    from sqlalchemy.orm import load_only
    from .models import ShopeeItemCache
    db = SessionLocal()
    try:
        linhas = (db.query(ProdutoCache)
                  .options(load_only(ProdutoCache.produto_id, ProdutoCache.sku, ProdutoCache.nome,
                                     ProdutoCache.imagem, ProdutoCache.preco, ProdutoCache.custo,
                                     ProdutoCache.saldo, ProdutoCache.marketplaces, ProdutoCache.atualizado_em))
                  .filter_by(user_id=user.id).all())
        try:
            shopee_px = {}
            for s in db.query(ShopeeItemCache.sku, ShopeeItemCache.imagem, ShopeeItemCache.preco,
                              ShopeeItemCache.preco_original, ShopeeItemCache.em_promocao,
                              ShopeeItemCache.promo_nome).filter(
                    ShopeeItemCache.user_id == user.id, ShopeeItemCache.sku.isnot(None)).all():
                if s[0]:
                    prat = float(s[3] or 0) or float(s[2] or 0)   # preço cheio (original) ou atual
                    promo = float(s[2] or 0)                      # preço corrente (promo, se houver)
                    shopee_px[s[0]] = {"imagem": s[1], "preco": prat,
                                       "preco_promo": promo if (s[4] and promo and promo < prat) else None,
                                       "em_promocao": bool(s[4]), "promo_nome": s[5]}
            shopee_img = {k: v["imagem"] for k, v in shopee_px.items()}
            shopee_skus = set(shopee_px.keys())
        except Exception:  # noqa: BLE001 — cache da Shopee ainda sem tabela: segue sem badge/imagem Shopee
            db.rollback()
            shopee_skus = set()
            shopee_img = {}
            shopee_px = {}
    finally:
        db.close()

    def _mk(reg_marketplaces, sku, preco_bling=0.0, custo=0.0):
        out = []
        for m in (reg_marketplaces or []):
            if not (isinstance(m, dict) and m.get("canal")):
                continue
            e = dict(m)
            pl = float(e.get("preco") or 0)
            if pl > 0:
                e["liquido"], e["flag"] = _status_canal(e["canal"], pl, preco_bling, custo)
            out.append(e)
        canais = {m.get("canal") for m in out}
        # Shopee: cache direto é a fonte da verdade do preço praticado
        sc = shopee_px.get(sku)
        if sc:
            sh = next((m for m in out if m.get("canal") == "shopee"), None)
            if sh is None:
                sh = {"canal": "shopee", "publicado": True}
                out.append(sh)
            sh["publicado"] = True
            pl = float(sc.get("preco") or 0)
            if pl > 0:
                sh["preco"] = pl
                sh["liquido"], sh["flag"] = _status_canal("shopee", pl, preco_bling, custo)
            if sc.get("em_promocao"):
                sh["promo"] = True
                if sc.get("preco_promo"):
                    sh["preco_promo"] = sc["preco_promo"]
        elif sku in shopee_skus and "shopee" not in canais:
            out.append({"canal": "shopee", "publicado": True})
        return out

    if linhas:
        itens = [_monta(p.produto_id, p.sku, p.nome, float(p.preco or 0), float(p.custo or 0),
                        float(p.saldo or 0), p.atualizado_em, p.imagem or shopee_img.get(p.sku),
                        _mk(p.marketplaces, p.sku, float(p.preco or 0), float(p.custo or 0)), 0) for p in linhas]
        return {"canal": canal, "fonte": "cache", "total": len(itens), "itens": itens}

    # Fallback: cache ainda não sincronizado — lê o Bling ao vivo (limitado).
    try:
        bruto = bling.listar_produtos(user.id, pagina=payload.get("pagina", 1),
                                      limite=payload.get("limite", 100))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    itens = []
    for p in bruto.get("data", []):
        custo = float(p.get("precoCusto") or (p.get("fornecedor") or {}).get("precoCusto") or 0)
        itens.append(_monta(p.get("id"), p.get("codigo"), p.get("nome"),
                            float(p.get("preco") or 0), custo, 0.0, None,
                            _img_do_payload(p), _mk(None, p.get("codigo"), float(p.get("preco") or 0), custo), 0))
    return {"canal": canal, "fonte": "bling", "total": len(itens), "itens": itens}


@app.get("/api/catalogo/vendas")
def catalogo_vendas(user: User = Depends(auth.get_current_user)):
    """Vendas 30d por SKU (Shopee) — chamada à parte pra não travar a lista do Catálogo."""
    try:
        return {"vendas": shopee.vendas_por_sku(user.id)}
    except Exception:  # noqa: BLE001
        return {"vendas": {}}


@app.post("/api/catalogo/ajustar_precos")
def catalogo_ajustar_precos(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajuste em massa do Preço Bling dos produtos selecionados.
    Body: {ids:[produto_id], modo:'pct'|'fixo', direcao:'mais'|'menos', valor:float}.
    Recalcula cada preço-base, grava no Bling e atualiza o cache local."""
    from .models import ProdutoCache
    ids = [str(i) for i in (payload.get("ids") or [])]
    modo = payload.get("modo", "pct")
    direcao = payload.get("direcao", "mais")
    valor = float(payload.get("valor") or 0)
    if not ids or valor <= 0:
        raise HTTPException(status_code=422, detail="Informe os itens e um valor positivo.")
    sinal = 1 if direcao == "mais" else -1
    db = SessionLocal()
    resultados, aplicados = [], 0
    try:
        regs = {r.produto_id: r for r in db.query(ProdutoCache).filter(
            ProdutoCache.user_id == user.id, ProdutoCache.produto_id.in_(ids)).all()}
        for pid in ids:
            reg = regs.get(pid)
            base = float(reg.preco or 0) if reg else 0.0
            if base <= 0:
                resultados.append({"id": pid, "erro": "sem preço-base"})
                continue
            novo = base * (1 + sinal * valor / 100.0) if modo == "pct" else base + sinal * valor
            novo = round(max(novo, 0.01), 2)
            try:
                bling.atualizar_preco(user.id, int(pid), novo)
                if reg:
                    reg.preco = novo
                aplicados += 1
                resultados.append({"id": pid, "de": round(base, 2), "para": novo, "ok": True})
            except Exception as e:  # noqa: BLE001
                resultados.append({"id": pid, "erro": str(e)[:120]})
        db.commit()
    finally:
        db.close()
    if aplicados:
        from . import notificacoes as notif
        notif.criar(user.id, "precificacao", f"{aplicados} preço(s) ajustado(s) em massa",
                    f"Ajuste de Preço Bling aplicado a {aplicados} produto(s).", ok=True, modulo="catalogo")
    return {"aplicados": aplicados, "total": len(ids), "itens": resultados}


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


@app.post("/api/radar/manual")
def radar_add_manual(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Cadastra um concorrente digitado na mão e já grava o preço. Body: {sku, nome, preco, marketplace?}"""
    sku = payload.get("sku")
    nome = (payload.get("nome") or "").strip()
    preco = payload.get("preco")
    if not sku or not nome or preco in (None, ""):
        raise HTTPException(status_code=422, detail="Informe 'sku', 'nome' e 'preco'.")
    try:
        p = float(str(preco).replace(",", "."))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Preço inválido.")
    if p <= 0:
        raise HTTPException(status_code=422, detail="Preço deve ser maior que zero.")
    return radar.adicionar_manual(user.id, sku, nome, p, payload.get("marketplace") or "shopee")


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
            "situacao_pendente": cfg.situacao_pendente,
            "desconto_plataformas": getattr(cfg, "desconto_plataformas", None) or {}}


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
        if "desconto_plataformas" in payload and isinstance(payload["desconto_plataformas"], dict):
            # sanitiza: só plataformas conhecidas, tipo válido e valor numérico
            limpo = {}
            for plat, regra in payload["desconto_plataformas"].items():
                if not isinstance(regra, dict):
                    continue
                tipo = regra.get("tipo")
                if tipo not in ("percentual", "valor"):
                    continue
                try:
                    valor = float(regra.get("valor") or 0)
                except (TypeError, ValueError):
                    continue
                if valor > 0:
                    limpo[str(plat)] = {"tipo": tipo, "valor": valor}
            cfg.desconto_plataformas = limpo
        db.commit()
        db.refresh(cfg)
        return _cfg_dict(cfg)


@app.get("/api/nfe/pendentes")
def nfe_pendentes(pagina: int = 1, limite: int = 100,
                  situacao: int | None = None,
                  user: User = Depends(auth.get_current_user)):
    """Lista as NF-e pendentes (editáveis), já normalizadas para a UI (número, cliente,
    valor, situação, editável). Usa o código de situação da config por padrão."""
    cfg = _nfe_cfg(user.id)
    sit = situacao if situacao is not None else cfg.situacao_pendente
    try:
        raw = bling.listar_nfe(user.id, pagina=pagina, limite=limite, situacao=sit)
        # lista rápida: sem buscar nota a nota (os valores vêm em background via /api/nfe/valores)
        return {"notas": nfe.resumir_lista(raw), "situacao": sit}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/pendentes/todas")
def nfe_pendentes_todas(situacao: int | None = None, max_paginas: int = 80,
                        user: User = Depends(auth.get_current_user)):
    """Lista TODAS as NF-e da situação, paginando o Bling até esgotar (sem o limite de 100).
    A lista é leve (sem valor/UF — esses vêm do /valores em lote). Teto de páginas por segurança.

    Categorias: situacao=6 => Autorizadas (situação 5 E 6, pois a conta pode usar qualquer uma);
    situacao=0 => Todas (mescla as situações usuais, já que o Bling desta conta não lista sem filtro);
    None => padrão (pendentes); demais => filtra por aquela situação."""
    import time as _t
    cfg = _nfe_cfg(user.id)
    if situacao == 0:
        sits = [cfg.situacao_pendente, 5, 6, 4, 2, 7]   # Todas (mescla)
    elif situacao == 6:
        sits = [5, 6]                                    # Autorizadas (5 ou 6)
    elif situacao is not None:
        sits = [situacao]
    else:
        sits = [cfg.situacao_pendente]
    # remove duplicatas preservando ordem
    sits = list(dict.fromkeys([s for s in sits if s is not None]))

    todas, vistos = [], set()
    try:
        for s in sits:
            pagina = 1
            while pagina <= max_paginas:
                raw = bling.listar_nfe(user.id, pagina=pagina, limite=100, situacao=s)
                lote = nfe.resumir_lista(raw)
                if not lote:
                    break
                for n in lote:
                    nid = n.get("id")
                    if nid not in vistos:
                        vistos.add(nid)
                        todas.append(n)
                if len(lote) < 100:
                    break
                pagina += 1
                _t.sleep(0.2)  # respeita o rate limit do Bling
        return {"notas": todas, "situacao": situacao, "situacoes": sits, "total": len(todas)}
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


@app.post("/api/nfe/valores")
def nfe_valores(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Busca em lote os dados que a lista do Bling não traz: valor, plataforma, UF, tributos
    (IBPT), pedido, chave, links e motivo de rejeição. Chamado em background pela tela.

    Faz as chamadas em PARALELO (cada bling.obter_nfe abre sua própria sessão de DB e respeita
    o rate-limit) — sem isso, dezenas de notas demoravam segundos e a tela ficava em 0,00.
    """
    from concurrent.futures import ThreadPoolExecutor
    ids = [str(i) for i in (payload.get("ids") or [])][:100]
    if not ids:
        return {}
    lojas_map = nfe._mapear_lojas_plataforma(user.id)  # cache 1h — resolve NuvemShop/site próprio

    def _um(nid):
        try:
            return nid, nfe.resumo_extra_raw(bling.obter_nfe(user.id, nid), lojas_map)
        except Exception:  # noqa: BLE001
            return nid, {"valor": 0, "plataforma": None}

    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for nid, dados in ex.map(_um, ids):
            out[nid] = dados
    return out


@app.post("/api/nfe/aplicar-selecionadas")
def nfe_aplicar_selecionadas(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Aplica o desconto padrão em uma lista ESPECÍFICA de notas (seleção em massa) e salva no Bling."""
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="Nenhuma nota selecionada.")
    cfg = _nfe_cfg(user.id)
    try:
        r = nfe.processar_ids(user.id, ids, cfg)
        from . import notificacoes as notif
        notif.criar(user.id, "nfe", f"Desconto aplicado em {r.get('aplicadas', 0)} nota(s)",
                    "Seleção em massa concluída. Venha conferir e transmitir no Bling.",
                    ok=(r.get("aplicadas", 0) > 0), modulo="nfe")
        return r
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/faturamento")
def nfe_faturamento(user: User = Depends(auth.get_current_user)):
    """Monitor do teto do Simples: RBT12, total do ano, projeção e % do teto/sublimite
    (estimativa por amostragem — a lista do Bling não traz valor). Lê o último snapshot."""
    return nfe.resumo_faturamento(user.id)


@app.post("/api/nfe/faturamento/recalcular")
def nfe_faturamento_recalcular(background: BackgroundTasks,
                               user: User = Depends(auth.get_current_user)):
    """Dispara o recálculo do faturamento em background (job pesado). A tela busca o
    resultado depois via GET /api/nfe/faturamento."""
    background.add_task(nfe.recalcular_faturamento, user.id)
    return {"iniciado": True, "mensagem": "Recálculo iniciado. Pode levar alguns minutos; atualize em instantes."}


@app.get("/api/nfe/contagens")
def nfe_contagens(user: User = Depends(auth.get_current_user)):
    """Contagem de notas por situação (para as abas). Paginação leve da lista resumida."""
    try:
        return nfe.contagens_situacao(user.id)
    except Exception:
        return {"pendentes": 0, "rejeitadas": 0, "autorizadas": 0, "canceladas": 0, "todas": 0, "aproximado": False}


@app.get("/api/nfe/pedidos-sem-nfe")
def nfe_pedidos_sem_nfe(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Pedidos de venda do Bling (todos os canais) sem NF-e emitida no período."""
    try:
        return nfe.pedidos_sem_nfe(user.id, dias=dias)
    except Exception as e:
        return {"dias": dias, "total": 0, "valor_total": 0, "pedidos": [], "erro": str(e)}


@app.post("/api/nfe/aplicar-todas")
def nfe_aplicar_todas(user: User = Depends(auth.get_current_user)):
    """Ação MANUAL: aplica o desconto padrão em TODAS as notas pendentes de uma vez e
    devolve cada uma ao Bling (sem precisar editar nota por nota no painel do Bling).
    Não depende do modo automático estar ligado. Retorna um relatório por nota."""
    cfg = _nfe_cfg(user.id)
    try:
        r = nfe.processar_automatico(user.id, cfg)
        from . import notificacoes as notif
        if r.get("aplicadas", 0) > 0:
            notif.criar(user.id, "nfe", f"Desconto aplicado em {r.get('aplicadas', 0)} nota(s) pendentes",
                        "Aplicação em lote concluída. Venha conferir e transmitir no Bling.",
                        ok=True, modulo="nfe")
        return r
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/notificacoes")
def notificacoes(limite: int = 40, user: User = Depends(auth.get_current_user)):
    """Centro de notificações da plataforma: eventos do Bling (NF-e, produtos, pedidos…) +
    o que os módulos fizeram (agentes aplicaram desconto, avaliações respondidas, radar de
    concorrência…), em ordem cronológica."""
    from . import notificacoes as notif
    itens = list(notif.listar(user.id, limite))  # notificações próprias (app)
    try:
        db = SessionLocal()
        try:
            regs = (db.query(WebhookEvento)
                    .filter(WebhookEvento.user_id == user.id)
                    .order_by(WebhookEvento.id.desc())
                    .limit(max(1, min(limite, 100))).all())
            for e in regs:
                cat, titulo, texto, ok = webhooks.descrever_evento(e.recurso, e.acao, e.resultado)
                itens.append({
                    "id": f"w{e.id}", "categoria": cat, "titulo": titulo, "texto": texto, "ok": ok,
                    "recurso": e.recurso, "acao": e.acao, "entidade_id": e.entidade_id,
                    "quando": e.recebido_em.isoformat() if e.recebido_em else None,
                    "resultado": e.resultado,
                })
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — schema de webhook desatualizado não pode derrubar o sino
        pass
    # ordena por horário desc (quando ausente vai pro fim) e corta no limite
    itens.sort(key=lambda x: (x.get("quando") or ""), reverse=True)
    return itens[:max(1, min(limite, 100))]


@app.post("/api/notificacoes/marcar-lidas")
def notificacoes_marcar_lidas(user: User = Depends(auth.get_current_user)):
    from . import notificacoes as notif
    return {"lidas": notif.marcar_lidas(user.id)}


@app.post("/api/notificacoes/arquivar")
def notificacoes_arquivar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Arquiva (marca como lidas) notificações específicas. Body: {ids:[...]} ou {id:'n123'}."""
    from . import notificacoes as notif
    ids = payload.get("ids")
    if ids is None and payload.get("id") is not None:
        ids = [payload["id"]]
    return {"arquivadas": notif.arquivar(user.id, ids or [])}


@app.get("/api/nfe/eventos")
def nfe_eventos(limite: int = 25, user: User = Depends(auth.get_current_user)):
    """Últimos eventos de NF-e recebidos do Bling via webhook, com o resultado do
    auto-apply do desconto (o que o sistema fez com cada nota)."""
    recursos = ["nfe", "notafiscal", "nota_fiscal", "notafiscaleletronica"]
    from sqlalchemy import func
    db = SessionLocal()
    try:
        regs = (db.query(WebhookEvento)
                .filter(WebhookEvento.user_id == user.id,
                        func.lower(WebhookEvento.recurso).in_(recursos))
                .order_by(WebhookEvento.id.desc())
                .limit(max(1, min(limite, 100))).all())
        return [{
            "id": e.id, "evento": e.event, "acao": e.acao, "entidade_id": e.entidade_id,
            "quando": e.recebido_em.isoformat() if e.recebido_em else None,
            "resultado": e.resultado,
        } for e in regs]
    finally:
        db.close()


@app.get("/api/nfe/{nfe_id}")
def nfe_obter(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Detalhe normalizado de uma nota (itens + frete) para o editor de desconto."""
    try:
        return nfe.normalizar_nfe(bling.obter_nfe(user.id, nfe_id),
                                  nfe._mapear_lojas_plataforma(user.id))
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/completa")
def nfe_completa(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Visão COMPLETA da nota: emitente, destinatário, totais, impostos, transporte, itens e links."""
    try:
        det = nfe.detalhar_nfe(bling.obter_nfe(user.id, nfe_id),
                               nfe._mapear_lojas_plataforma(user.id))
        # Emitente (sua empresa) — reaproveita os dados do cadastro de impressão (etiqueta/folha).
        try:
            imp = shopee_impressao.obter_config(user.id) or {}
            nome = (imp.get("emitente_nome") or "").strip()
            det["emitente"] = {
                "nome": nome,
                "cnpj": (imp.get("emitente_cnpj") or "").strip(),
                "endereco": (imp.get("emitente_endereco") or "").strip(),
                "cidade": (imp.get("emitente_cidade") or "").strip(),
            } if nome or imp.get("emitente_cnpj") else None
        except Exception:
            det["emitente"] = None
        return det
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/diagnostico-edicao")
def nfe_diagnostico_edicao(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Dry-run: mostra a estrutura real da nota e o payload que seria enviado (sem enviar),
    para ver as parcelas e checar se a soma bate com o total."""
    cfg = _nfe_cfg(user.id)
    try:
        return nfe.diagnosticar_edicao(
            user.id, nfe_id,
            desconto_tipo=cfg.desconto_tipo, desconto_valor=float(cfg.desconto_valor or 0),
            remover_frete=bool(cfg.remover_frete),
        )
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/conciliacao-shopee")
def nfe_conciliacao_shopee(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Compara o valor fiscal da NF-e com o repasse real da Shopee (escrow) do pedido."""
    try:
        return nfe.conciliar_shopee(user.id, nfe_id)
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/conciliacao-shopee-lote")
def nfe_conciliacao_shopee_lote(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Concilia várias notas Shopee de uma vez (resumo + divergências)."""
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="Nenhuma nota informada.")
    try:
        return nfe.conciliar_shopee_lote(user.id, ids)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/{nfe_id}/enviar")
def nfe_enviar(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Retransmite uma NF-e ao Sefaz (para reprocessar notas rejeitadas já corrigidas)."""
    try:
        resultado = bling.enviar_nfe(user.id, nfe_id)
        from . import notificacoes as notif
        notif.criar(user.id, "nfe", f"Nota {nfe_id} retransmitida ao Sefaz",
                    "Acompanhe a autorização na lista/no Bling.", ok=True,
                    modulo="nfe", entidade_id=nfe_id)
        return {"ok": True, "resultado": resultado}
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/{nfe_id}/aplicar")
def nfe_aplicar(nfe_id: str, payload: dict = Body(...),
                user: User = Depends(auth.get_current_user)):
    """Aplica a edição numa nota. enviar=false revisa; enviar=true devolve ao Bling.

    Body: {desconto_tipo, desconto_valor, descontos_por_item?, remover_frete, enviar}
    """
    try:
        res = nfe.editar_nota(
            user.id, nfe_id,
            desconto_tipo=payload.get("desconto_tipo", "percentual"),
            desconto_valor=float(payload.get("desconto_valor", 0)),
            descontos_por_item=payload.get("descontos_por_item"),
            remover_frete=bool(payload.get("remover_frete", True)),
            enviar=bool(payload.get("enviar", False)),
        )
        if res.get("enviado"):
            from . import notificacoes as notif
            total = (res.get("resumo") or {}).get("total_nota")
            notif.criar(user.id, "nfe", f"Desconto aplicado na nota {res.get('numero') or nfe_id}",
                        f"Novo total {_brl(total)}. Confira e transmita no Bling." if total is not None else "Confira a nota.",
                        ok=True, modulo="nfe", entidade_id=nfe_id)
        return res
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
