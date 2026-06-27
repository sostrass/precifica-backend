"""Config de impressão por conta (Shopee): dados do emitente + campos visíveis na
folha de separação e na etiqueta. É o backend do painel "Personalizar impressão"
(Módulo 2). Multi-tenant — cada usuário tem a sua linha."""
from __future__ import annotations

from datetime import datetime

from .db import SessionLocal
from .models import ShopeeImpressaoConfig

# toggles conhecidos (nome do campo -> default)
_FLAGS = {
    "mostrar_timeline": True,
    "mostrar_nfe": True,
    "mostrar_rastreio": True,
    "mostrar_destinatario": True,
    "mostrar_miniaturas": True,
    "mostrar_complemento": True,
    "mostrar_nota_comprador": True,
    "mostrar_codigo_barras": True,
    "mostrar_qr": True,
}


def _config(db, user_id: int) -> ShopeeImpressaoConfig:
    c = db.query(ShopeeImpressaoConfig).filter_by(user_id=user_id).first()
    if not c:
        c = ShopeeImpressaoConfig(user_id=user_id)
        db.add(c)
        db.commit()
        db.refresh(c)
    return c


def _serializar(c: ShopeeImpressaoConfig) -> dict:
    d = {
        "emitente_nome": c.emitente_nome or "",
        "emitente_cnpj": c.emitente_cnpj or "",
        "emitente_endereco": c.emitente_endereco or "",
        "emitente_cidade": c.emitente_cidade or "",
    }
    for flag in _FLAGS:
        v = getattr(c, flag, None)
        d[flag] = bool(v) if v is not None else _FLAGS[flag]
    return d


def obter_config(user_id: int) -> dict:
    db = SessionLocal()
    try:
        return _serializar(_config(db, user_id))
    finally:
        db.close()


def salvar_config(user_id: int, payload: dict) -> dict:
    db = SessionLocal()
    try:
        c = _config(db, user_id)
        if "emitente_nome" in payload:
            c.emitente_nome = str(payload["emitente_nome"] or "")[:120]
        if "emitente_cnpj" in payload:
            c.emitente_cnpj = str(payload["emitente_cnpj"] or "")[:24]
        if "emitente_endereco" in payload:
            c.emitente_endereco = str(payload["emitente_endereco"] or "")[:160]
        if "emitente_cidade" in payload:
            c.emitente_cidade = str(payload["emitente_cidade"] or "")[:120]
        for flag in _FLAGS:
            if flag in payload:
                setattr(c, flag, bool(payload[flag]))
        c.atualizado_em = datetime.utcnow()
        db.commit()
        db.refresh(c)
        return _serializar(c)
    finally:
        db.close()
