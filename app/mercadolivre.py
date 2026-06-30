"""Integração direta com a API do Mercado Livre (api.mercadolibre.com).

Espelha o papel do módulo da Shopee: ler os anúncios do vendedor (por SKU),
preço/status e empurrar atualização de preço — sem depender do Bling, que via
v3 não devolve os vínculos com preço.

Credenciais (por enquanto via ambiente; depois movemos pra config por usuário):
  ML_CLIENT_ID, ML_CLIENT_SECRET, ML_REFRESH_TOKEN, ML_SELLER_ID

Fluxo OAuth do ML (resumo, pra próxima fase ligar o botão "Conectar"):
  1. Redireciona o vendedor pra https://auth.mercadolivre.com.br/authorization?response_type=code&client_id=...&redirect_uri=...
  2. O ML chama de volta o redirect_uri com ?code=...
  3. Troca o code por access_token + refresh_token (grant_type=authorization_code).
  4. Guarda o refresh_token; renova o access_token quando expira (grant_type=refresh_token).
"""
from __future__ import annotations

import os
import time

import requests

API = "https://api.mercadolibre.com"
AUTH = "https://auth.mercadolivre.com.br"
TIMEOUT = 20


class MLNaoConfigurado(RuntimeError):
    """Credenciais do Mercado Livre ausentes/incompletas."""


class MLErro(RuntimeError):
    """Falha em chamada à API do Mercado Livre."""


# cache simples do access_token em memória: {chave: (expira_em, token)}
_TOKENS: dict[str, tuple[float, str]] = {}


def _cfg() -> dict:
    return {
        "client_id": os.environ.get("ML_CLIENT_ID"),
        "client_secret": os.environ.get("ML_CLIENT_SECRET"),
        "refresh_token": os.environ.get("ML_REFRESH_TOKEN"),
        "seller_id": os.environ.get("ML_SELLER_ID"),
    }


def configurado() -> bool:
    c = _cfg()
    return bool(c["client_id"] and c["client_secret"] and c["refresh_token"])


def url_autorizacao(redirect_uri: str) -> str:
    """URL pra iniciar o OAuth (botão 'Conectar Mercado Livre')."""
    c = _cfg()
    if not c["client_id"]:
        raise MLNaoConfigurado("ML_CLIENT_ID ausente")
    return (f"{AUTH}/authorization?response_type=code"
            f"&client_id={c['client_id']}&redirect_uri={redirect_uri}")


def trocar_code_por_token(code: str, redirect_uri: str) -> dict:
    """Troca o 'code' do callback por access_token + refresh_token."""
    c = _cfg()
    if not (c["client_id"] and c["client_secret"]):
        raise MLNaoConfigurado("client_id/secret ausentes")
    r = requests.post(f"{API}/oauth/token", timeout=TIMEOUT, data={
        "grant_type": "authorization_code", "client_id": c["client_id"],
        "client_secret": c["client_secret"], "code": code, "redirect_uri": redirect_uri,
    })
    if r.status_code >= 400:
        raise MLErro(f"oauth: {r.status_code} {r.text[:200]}")
    return r.json()


def _access_token() -> str:
    """Renova (ou reusa) o access_token via refresh_token."""
    if not configurado():
        raise MLNaoConfigurado("defina ML_CLIENT_ID, ML_CLIENT_SECRET e ML_REFRESH_TOKEN")
    c = _cfg()
    chave = c["refresh_token"][:12]
    cached = _TOKENS.get(chave)
    if cached and time.time() < cached[0] - 60:
        return cached[1]
    r = requests.post(f"{API}/oauth/token", timeout=TIMEOUT, data={
        "grant_type": "refresh_token", "client_id": c["client_id"],
        "client_secret": c["client_secret"], "refresh_token": c["refresh_token"],
    })
    if r.status_code >= 400:
        raise MLErro(f"refresh: {r.status_code} {r.text[:200]}")
    d = r.json()
    tok = d.get("access_token")
    expira = time.time() + float(d.get("expires_in") or 21600)
    _TOKENS[chave] = (expira, tok)
    return tok


def _get(path: str, params: dict | None = None) -> dict:
    tok = _access_token()
    r = requests.get(f"{API}{path}", params=params, timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {tok}"})
    if r.status_code >= 400:
        raise MLErro(f"GET {path}: {r.status_code} {r.text[:200]}")
    return r.json()


def _seller_id() -> str:
    sid = _cfg().get("seller_id")
    if sid:
        return sid
    me = _get("/users/me")
    return str(me.get("id"))


def buscar_item_por_sku(sku: str) -> dict | None:
    """Acha o anúncio do vendedor que tem este SKU (seller_custom_field/seller_sku).
    Retorna {item_id, preco, status, permalink, titulo} ou None."""
    if not sku:
        return None
    sid = _seller_id()
    # 1) busca direta por seller_sku
    try:
        d = _get(f"/users/{sid}/items/search", params={"seller_sku": sku})
        ids = d.get("results") or []
        if ids:
            return obter_item(ids[0])
    except MLErro:
        pass
    # 2) fallback: busca textual (a próxima fase pode varrer + cachear tudo)
    return None


def obter_item(item_id: str) -> dict:
    it = _get(f"/items/{item_id}")
    sku = None
    for a in (it.get("attributes") or []):
        if a.get("id") in ("SELLER_SKU", "GTIN") and a.get("value_name"):
            sku = a["value_name"]
            break
    if not sku:
        sku = it.get("seller_custom_field")
    return {
        "item_id": it.get("id"), "titulo": it.get("title"),
        "preco": float(it.get("price") or 0),
        "preco_original": float(it.get("original_price") or it.get("price") or 0),
        "status": it.get("status"), "permalink": it.get("permalink"),
        "sku": sku, "estoque": it.get("available_quantity"),
    }


def atualizar_preco(item_id: str, preco: float) -> dict:
    """Empurra o novo preço direto no anúncio do ML (PUT /items/{id})."""
    tok = _access_token()
    r = requests.put(f"{API}/items/{item_id}", timeout=TIMEOUT,
                     headers={"Authorization": f"Bearer {tok}",
                              "Content-Type": "application/json"},
                     json={"price": round(float(preco), 2)})
    if r.status_code >= 400:
        raise MLErro(f"PUT /items/{item_id}: {r.status_code} {r.text[:200]}")
    return {"ok": True, "item_id": item_id, "preco": round(float(preco), 2)}
