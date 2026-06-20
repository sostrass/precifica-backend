"""Integração com a API v3 do Bling — agora multi-tenant (token por usuário).

Fatos oficiais (developer.bling.com.br):
- Base: https://api.bling.com.br/Api/v3
- OAuth 2.0 authorization_code -> POST /oauth/token (o 'code' expira em ~1 min)
- Renovação via refresh_token
- Limites: 3 req/s e 120.000/dia
- Listagem traz dados resumidos; detalhe completo via GET individual
"""

import base64
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import jwt
import requests

from .config import settings
from .db import SessionLocal
from .models import OAuthToken

API_BASE = "https://api.bling.com.br/Api/v3"
AUTHORIZE_URL = "https://www.bling.com.br/Api/v3/oauth/authorize"
TOKEN_URL = f"{API_BASE}/oauth/token"


class BlingAuthError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Rate limiter global (3 req/s do Bling). Em multi-instância, mover p/ Redis.
# --------------------------------------------------------------------------- #
class _RateLimiter:
    def __init__(self, rate_per_sec: int = 3):
        self.min_interval = 1.0 / rate_per_sec
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delta = now - self._last
            if delta < self.min_interval:
                time.sleep(self.min_interval - delta)
            self._last = time.monotonic()


_limiter = _RateLimiter(rate_per_sec=3)


def _basic_auth_header() -> str:
    raw = f"{settings.bling_client_id}:{settings.bling_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# --------------------------------------------------------------------------- #
# OAuth — o 'state' carrega (assinado) o id do usuário, para o callback saber
# a qual tenant associar o token. Sem estado em memória (funciona multi-instância).
# --------------------------------------------------------------------------- #
def get_authorize_url(user_id: int) -> str:
    state = jwt.encode(
        {"uid": user_id, "type": "bling_state",
         "exp": datetime.utcnow() + timedelta(minutes=10)},
        settings.jwt_secret, algorithm="HS256",
    )
    params = {"response_type": "code", "client_id": settings.bling_client_id, "state": state}
    if settings.bling_redirect_uri:
        params["redirect_uri"] = settings.bling_redirect_uri
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def user_id_from_state(state: str) -> int:
    try:
        payload = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise BlingAuthError("State inválido ou expirado.")
    if payload.get("type") != "bling_state":
        raise BlingAuthError("State inválido.")
    return int(payload["uid"])


def _save_token(user_id: int, data: dict):
    expires_in = int(data.get("expires_in", 21600))  # 6h padrão do Bling
    expires_at = datetime.utcnow() + timedelta(seconds=expires_in - 60)
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None:
            tok = OAuthToken(user_id=user_id)
            db.add(tok)
        tok.access_token = data["access_token"]
        novo_refresh = data.get("refresh_token")
        if novo_refresh:
            tok.refresh_token = novo_refresh
        elif not tok.refresh_token:
            tok.refresh_token = ""
        tok.expires_at = expires_at
        db.commit()


def exchange_code(user_id: int, code: str):
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "1.0",
    }
    r = requests.post(TOKEN_URL, headers=headers,
                      data={"grant_type": "authorization_code", "code": code}, timeout=30)
    if r.status_code != 200:
        raise BlingAuthError(f"Falha ao trocar o code ({r.status_code}): {r.text}")
    _save_token(user_id, r.json())


def refresh_token(user_id: int):
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None or not tok.refresh_token:
            raise BlingAuthError("Sem refresh_token. Refaça a autorização do Bling.")
        rt = tok.refresh_token
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "1.0",
    }
    r = requests.post(TOKEN_URL, headers=headers,
                      data={"grant_type": "refresh_token", "refresh_token": rt}, timeout=30)
    if r.status_code != 200:
        raise BlingAuthError(f"Falha no refresh ({r.status_code}): {r.text}")
    _save_token(user_id, r.json())


def _access_token(user_id: int) -> str:
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None:
            raise BlingAuthError("Conta Bling ainda não autorizada para este usuário.")
        expirado = tok.expires_at <= datetime.utcnow()
        at = tok.access_token
    if expirado:
        refresh_token(user_id)
        with SessionLocal() as db:
            at = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first().access_token
    return at


def token_status(user_id: int) -> dict:
    with SessionLocal() as db:
        tok = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).first()
        if tok is None:
            return {"autorizado": False}
        return {
            "autorizado": True,
            "expira_em": tok.expires_at.isoformat() + "Z",
            "expirado": tok.expires_at <= datetime.utcnow(),
        }


# --------------------------------------------------------------------------- #
# Requisições à API (sempre escopadas no token do usuário)
# --------------------------------------------------------------------------- #
def _request(user_id: int, method: str, path: str, **kwargs) -> requests.Response:
    _limiter.wait()
    url = f"{API_BASE}{path}"
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {_access_token(user_id)}"
    headers.setdefault("Accept", "application/json")
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    if r.status_code == 401:
        refresh_token(user_id)
        _limiter.wait()
        headers["Authorization"] = f"Bearer {_access_token(user_id)}"
        r = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    return r


def listar_produtos(user_id: int, pagina: int = 1, limite: int = 100,
                    criterio: int | None = None) -> dict:
    params = {"pagina": pagina, "limite": limite}
    if criterio is not None:
        params["criterio"] = criterio
    r = _request(user_id, "GET", "/produtos", params=params)
    r.raise_for_status()
    return r.json()


def obter_produto(user_id: int, produto_id: int) -> dict:
    r = _request(user_id, "GET", f"/produtos/{produto_id}")
    r.raise_for_status()
    return r.json()


def atualizar_preco(user_id: int, produto_id: int, preco: float) -> dict:
    """PATCH parcial só do preço. Se a conta exigir PUT completo, ver README."""
    r = _request(user_id, "PATCH", f"/produtos/{produto_id}",
                 json={"preco": round(float(preco), 2)})
    r.raise_for_status()
    return r.json()


# Campos editáveis aceitos no PATCH parcial de produto (nomes da API v3 do Bling).
_CAMPOS_PRODUTO = {"nome", "preco", "precoCusto", "ncm", "pesoBruto",
                   "pesoLiquido", "descricaoCurta", "gtin"}


def atualizar_produto(user_id: int, produto_id: int, campos: dict) -> dict:
    """PATCH parcial dos campos editáveis do produto (só envia o que mudou)."""
    corpo = {k: v for k, v in campos.items() if k in _CAMPOS_PRODUTO and v is not None}
    for k in ("preco", "precoCusto", "pesoBruto", "pesoLiquido"):
        if k in corpo and corpo[k] != "":
            corpo[k] = round(float(corpo[k]), 3 if "peso" in k else 2)
    if not corpo:
        return {}
    r = _request(user_id, "PATCH", f"/produtos/{produto_id}", json=corpo)
    r.raise_for_status()
    return r.json()


def listar_nfe(user_id: int, pagina: int = 1, limite: int = 100,
               situacao: int | None = None) -> dict:
    """Lista NF-e (resumidas). Filtra por situação quando informado (ex.: pendente)."""
    params = {"pagina": pagina, "limite": limite}
    if situacao is not None:
        params["situacao"] = situacao
    r = _request(user_id, "GET", "/nfe", params=params)
    r.raise_for_status()
    return r.json()


def obter_nfe(user_id: int, nfe_id) -> dict:
    """Detalhe completo de uma NF-e (com itens e transporte)."""
    r = _request(user_id, "GET", f"/nfe/{nfe_id}")
    r.raise_for_status()
    return r.json()


def atualizar_nfe(user_id: int, nfe_id, payload: dict) -> dict:
    """Altera uma NF-e existente (PUT). Só funciona em nota Pendente/Rejeitada.

    O envio ao Sefaz é feito no painel/automação do Bling (certificado A1 lá),
    não aqui. Endpoint assumido: PUT /nfe/{id} — confirme contra o schema da sua conta.
    """
    r = _request(user_id, "PUT", f"/nfe/{nfe_id}", json=payload)
    r.raise_for_status()
    return r.json()
