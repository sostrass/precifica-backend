"""Desmascaramento dos dados do comprador — Shopee.

A Shopee passou a devolver os dados do comprador SEM máscara quando o pedido está em
READY_TO_SHIP, TO_RETURN ou PROCESSED, desde que os campos sejam pedidos explicitamente
em `response_optional_fields` do get_order_detail:

    recipient_address · phone · buyer_cpf_id · buyer_username

O módulo `shopee.py` é FROZEN e pede apenas `recipient_address,buyer_username` — sem
`phone` e `buyer_cpf_id`. Este módulo reaproveita a infraestrutura dele (`_chamar`, que
já resolve token e assinatura) e faz a chamada com a lista completa, sem tocá-lo.

Resultado: nome, endereço completo, telefone e CPF do comprador ficam disponíveis no
painel e na Folha do Pedido — sem depender da waybill oficial da SPX.
"""
from __future__ import annotations

import threading
import time

from . import shopee

# status em que a Shopee entrega os dados SEM máscara (conforme comunicado oficial)
STATUS_DESMASCARADO = {"READY_TO_SHIP", "PROCESSED", "TO_RETURN"}

# todos os campos úteis + os quatro do desmascaramento
_CAMPOS = ",".join([
    "item_list", "recipient_address", "buyer_username", "buyer_cpf_id",
    "pay_time", "note", "payment_method", "cod", "order_status",
    "actual_shipping_fee", "total_amount",
])

_CACHE: dict = {}          # (user_id, order_sn) -> (ts, dados)
_TTL = 900                 # 15 min: dado pessoal, cache curto
_LOCK = threading.Lock()
_SEM = threading.BoundedSemaphore(4)   # não saturar a API da Shopee


def _limpa(v):
    """Descarta valores mascarados. A Shopee mascara com sequências de asteriscos
    ('*****', 'J***', '****@mail.com'): 3+ asteriscos seguidos = dado mascarado."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or "***" in s or set(s) <= {"*", " "}:
        return None
    return s


def _monta(o: dict) -> dict:
    rec = o.get("recipient_address") or {}
    nome = _limpa(rec.get("name"))
    tel = _limpa(rec.get("phone")) or _limpa(o.get("phone"))
    cpf = _limpa(o.get("buyer_cpf_id"))
    completo = _limpa(rec.get("full_address"))
    partes = [_limpa(rec.get("district")), _limpa(rec.get("town"))]
    return {
        "order_sn": o.get("order_sn"),
        "status": o.get("order_status"),
        "desmascarado": bool(nome and completo),
        "cliente": nome,
        "buyer_username": _limpa(o.get("buyer_username")),
        "cpf": cpf,
        "telefone": tel,
        "endereco": {
            "nome": nome,
            "telefone": tel,
            "cpf": cpf,
            "completo": completo,
            "bairro": next((p for p in partes if p), None),
            "cidade": _limpa(rec.get("city")),
            "uf": _limpa(rec.get("state")),
            "cep": _limpa(rec.get("zipcode")),
            "pais": _limpa(rec.get("region")),
        },
    }


def comprador(user_id: int, order_sns: list, forcar: bool = False) -> dict:
    """Mapa {order_sn: dados} com o comprador DESMASCARADO.

    Só chama a API para os pedidos em status que a Shopee libera; os demais voltam
    marcados com `desmascarado=False` (a máscara é do canal, não erro nosso).
    """
    sns = [str(s) for s in (order_sns or []) if s]
    if not sns:
        return {}
    agora = time.time()
    out, faltam = {}, []
    with _LOCK:
        for sn in sns:
            ent = _CACHE.get((user_id, sn))
            if ent and not forcar and agora - ent[0] < _TTL:
                out[sn] = ent[1]
            else:
                faltam.append(sn)
    # a Shopee aceita até 50 order_sn por chamada
    for i in range(0, len(faltam), 50):
        lote = faltam[i:i + 50]
        try:
            with _SEM:
                r = shopee._chamar(user_id, "/api/v2/order/get_order_detail",
                                   {"order_sn_list": ",".join(lote),
                                    "response_optional_fields": _CAMPOS}) or {}
        except Exception as e:  # noqa: BLE001
            print(f"[shopee_comprador] lote falhou ({len(lote)} pedidos): {e}", flush=True)
            continue
        for o in ((r.get("response") or {}).get("order_list") or []):
            dados = _monta(o)
            sn = str(dados.get("order_sn") or "")
            if not sn:
                continue
            out[sn] = dados
            with _LOCK:
                _CACHE[(user_id, sn)] = (agora, dados)
        time.sleep(0.25)   # respiro entre lotes
    for sn in sns:
        out.setdefault(sn, {"order_sn": sn, "desmascarado": False})
    return out


def aquecer(user_id: int, order_sns: list) -> None:
    """Busca em segundo plano — o painel nunca espera por isto."""
    def _run():
        try:
            n = comprador(user_id, order_sns)
            ok = sum(1 for v in n.values() if v.get("desmascarado"))
            print(f"[shopee_comprador] aquecido: {ok}/{len(n)} desmascarados", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[shopee_comprador] aquecimento falhou: {e}", flush=True)
    threading.Thread(target=_run, daemon=True).start()
