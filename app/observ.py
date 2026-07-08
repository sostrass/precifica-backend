"""
Observabilidade de A a Z do backend.

Objetivo: qualquer falha em qualquer parte do sistema aparece no painel de logs do
Railway (stdout), com contexto — especialmente as criações de promoção/anúncio em massa
(Shopee e Mercado Livre), onde o problema hoje é "cria mas fica sem produto".

Como funciona:
  - configurar_logs(): manda tudo para stdout num formato limpo e legível.
  - instrumentar(): via monkeypatch, envolve as funções de chamada externa
    (shopee._chamar e mercadolivre._req) para logar cada chamada, o tempo, e —
    o ponto crítico — quando a Shopee aceita a campanha mas NÃO adiciona os itens
    (response.count < itens enviados), o que vira um WARN com a resposta crua.
  - middleware()/log_excecao(): logam cada requisição HTTP e cada exceção com traceback.
  - buffer em memória + logs_recentes(): permite ver os últimos logs dentro do app também.

Nada aqui edita arquivos congelados: a instrumentação é aplicada em tempo de import.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import traceback
import uuid
from collections import deque

log = logging.getLogger("precifica")

# Buffer circular dos últimos registros (para o endpoint /api/admin/logs)
_BUFFER: deque = deque(maxlen=800)
_instrumentado = False


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        try:
            _BUFFER.append({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "nivel": record.levelname,
                "origem": record.name,
                "msg": record.getMessage(),
            })
        except Exception:  # noqa: BLE001
            pass


def configurar_logs() -> None:
    """Configura o logging raiz para stdout (capturado pelo Railway)."""
    nivel = os.environ.get("LOG_LEVEL", "INFO").upper()
    raiz = logging.getLogger()
    raiz.setLevel(nivel)
    # remove handlers antigos para não duplicar linhas
    for h in list(raiz.handlers):
        raiz.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    saida = logging.StreamHandler(sys.stdout)
    saida.setFormatter(fmt)
    raiz.addHandler(saida)
    raiz.addHandler(_BufferHandler())
    # bibliotecas barulhentas em nível mais alto
    for ruido in ("httpx", "urllib3", "uvicorn.access"):
        logging.getLogger(ruido).setLevel(logging.WARNING)
    log.info("observabilidade ativa | nivel=%s", nivel)


def _resumo(obj, limite: int = 700) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return s if len(s) <= limite else s[:limite] + "…(truncado)"


def _contar_itens(extra) -> int | None:
    """Quantos itens estão sendo enviados numa chamada de adicionar item."""
    if not isinstance(extra, dict):
        return None
    for chave in ("item_list", "main_item_list", "sub_item_list", "models", "item_id_list"):
        v = extra.get(chave)
        if isinstance(v, list):
            return len(v)
    return None


def _log_chamada_externa(canal: str, metodo: str, path: str, ms: int, r, enviados) -> None:
    resp = r.get("response") if isinstance(r, dict) else None
    err = r.get("error") if isinstance(r, dict) else None
    msg = r.get("message") if isinstance(r, dict) else None
    count = resp.get("count") if isinstance(resp, dict) else None
    add_item = ("add_" in path and "_item" in path) or ("/add_" in path and "item" in path)

    if err:  # a Shopee sinalizou erro explícito
        log.warning("%s %s %s (%dms) ERRO=%s msg=%s | resp=%s", canal, metodo, path, ms, err, msg, _resumo(r))
    elif add_item and enviados is not None and count is not None and count < enviados:
        # cria a campanha mas NÃO adiciona todos os itens — o bug silencioso
        log.warning("%s %s %s (%dms) SO %s DE %s ITENS ENTRARAM (o resto foi recusado em silencio) | resp=%s",
                    canal, metodo, path, ms, count, enviados, _resumo(r))
    elif add_item:
        log.info("%s %s %s (%dms) itens ok: %s de %s", canal, metodo, path, ms, count, enviados)
    else:
        log.info("%s %s %s (%dms) ok", canal, metodo, path, ms)


def instrumentar() -> None:
    """Envolve as funções de chamada externa da Shopee e do Mercado Livre para logar tudo.
    Aplica monkeypatch em tempo de execução — não altera os arquivos."""
    global _instrumentado
    if _instrumentado:
        return

    # ---- Shopee._chamar (arquivo congelado — só envolvemos, não editamos) ----
    try:
        from . import shopee as _sh
        _orig = _sh._chamar

        def _chamar_logado(user_id, path, extra=None, metodo="GET", timeout=25):
            t0 = time.time()
            enviados = _contar_itens(extra)
            try:
                r = _orig(user_id, path, extra=extra, metodo=metodo, timeout=timeout)
                _log_chamada_externa("SHOPEE", metodo, path, int((time.time() - t0) * 1000), r, enviados)
                return r
            except Exception as e:  # noqa: BLE001
                log.warning("SHOPEE %s %s (%dms) FALHOU: %s: %s", metodo, path,
                            int((time.time() - t0) * 1000), type(e).__name__, e)
                raise

        _sh._chamar = _chamar_logado
        log.info("instrumentado: shopee._chamar")
    except Exception as e:  # noqa: BLE001
        log.warning("nao instrumentou shopee: %s", e)

    # ---- Mercado Livre._req ----
    try:
        from . import mercadolivre as _ml
        _orig_ml = _ml._req

        def _req_logado(metodo, path, user_id=None, params=None, json=None, headers=None, base=None, raw=False):
            t0 = time.time()
            enviados = _contar_itens(json)
            kwargs = dict(user_id=user_id, params=params, json=json, headers=headers, raw=raw)
            if base is not None:
                kwargs["base"] = base
            try:
                r = _orig_ml(metodo, path, **kwargs)
                corpo = None
                if isinstance(r, dict):
                    corpo = r
                elif hasattr(r, "status_code"):
                    log.info("ML %s %s (%dms) http=%s", metodo, path, int((time.time() - t0) * 1000), r.status_code)
                    return r
                _log_chamada_externa("ML", metodo, path, int((time.time() - t0) * 1000), corpo, enviados)
                return r
            except Exception as e:  # noqa: BLE001
                log.warning("ML %s %s (%dms) FALHOU: %s: %s", metodo, path,
                            int((time.time() - t0) * 1000), type(e).__name__, e)
                raise

        _ml._req = _req_logado
        log.info("instrumentado: mercadolivre._req")
    except Exception as e:  # noqa: BLE001
        log.warning("nao instrumentou mercadolivre: %s", e)

    _instrumentado = True


async def middleware(request, call_next):
    """Loga cada requisição HTTP: entrada, saída, status e tempo. Erros viram ERROR com traceback."""
    rid = uuid.uuid4().hex[:8]
    t0 = time.time()
    metodo = request.method
    caminho = request.url.path
    if caminho in ("/", "/health", "/favicon.ico"):
        return await call_next(request)
    try:
        resp = await call_next(request)
        ms = int((time.time() - t0) * 1000)
        nivel = logging.WARNING if resp.status_code >= 400 else logging.INFO
        log.log(nivel, "req[%s] %s %s -> %s (%dms)", rid, metodo, caminho, resp.status_code, ms)
        return resp
    except Exception as exc:  # noqa: BLE001
        ms = int((time.time() - t0) * 1000)
        log.error("req[%s] %s %s -> EXCECAO (%dms): %s: %s\n%s", rid, metodo, caminho, ms,
                  type(exc).__name__, exc, traceback.format_exc())
        raise


def log_excecao(request, exc: Exception) -> None:
    """Registra uma exceção não tratada com contexto (usado pelo handler global)."""
    try:
        caminho = request.url.path
        metodo = request.method
    except Exception:  # noqa: BLE001
        caminho, metodo = "?", "?"
    log.error("ERRO NAO TRATADO em %s %s: %s: %s\n%s", metodo, caminho,
              type(exc).__name__, exc, traceback.format_exc())


def logs_recentes(n: int = 200, nivel: str | None = None) -> list:
    itens = list(_BUFFER)
    if nivel:
        nivel = nivel.upper()
        itens = [x for x in itens if x["nivel"] == nivel]
    return itens[-n:][::-1]  # mais recentes primeiro
