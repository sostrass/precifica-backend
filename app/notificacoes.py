"""Notificações da plataforma — centro unificado para o sino global.

Qualquer módulo chama `criar(...)` quando algo relevante acontece (um agente aplicou
desconto, novas avaliações foram respondidas, o radar achou mudança de concorrente, etc.).
Nunca levanta exceção para o chamador: notificar é secundário ao fluxo principal.
"""
from __future__ import annotations

import logging

log = logging.getLogger("notificacoes")

# rótulos amigáveis por categoria (o front usa a categoria para o ícone)
CATEGORIAS = {
    "nfe": "Nota fiscal",
    "precificacao": "Precificação",
    "avaliacao": "Avaliações",
    "radar": "Radar",
    "concorrencia": "Concorrência",
    "pedido": "Pedidos",
    "estoque": "Estoque",
    "agente": "Agentes",
    "produto": "Produtos",
    "outro": "Geral",
}


def criar(user_id: int, categoria: str, titulo: str, texto: str = "",
          ok: bool = True, modulo: str | None = None, entidade_id=None) -> None:
    """Registra uma notificação. Best-effort: engole qualquer erro."""
    try:
        from .db import SessionLocal
        from .models import Notificacao
        with SessionLocal() as db:
            db.add(Notificacao(
                user_id=user_id, categoria=(categoria or "outro"),
                titulo=titulo[:200], texto=(texto or "")[:500],
                ok=bool(ok), modulo=modulo,
                entidade_id=str(entidade_id) if entidade_id is not None else None,
            ))
            db.commit()
    except Exception:  # noqa: BLE001
        log.debug("falha ao criar notificação", exc_info=True)


def listar(user_id: int, limite: int = 40) -> list:
    """Lista as notificações próprias do usuário (mais novas primeiro)."""
    try:
        from .db import SessionLocal
        from .models import Notificacao
        with SessionLocal() as db:
            regs = (db.query(Notificacao)
                    .filter(Notificacao.user_id == user_id)
                    .order_by(Notificacao.id.desc())
                    .limit(max(1, min(limite, 100))).all())
            return [{
                "id": f"n{r.id}", "categoria": r.categoria, "titulo": r.titulo,
                "texto": r.texto or "", "ok": bool(r.ok), "modulo": r.modulo,
                "entidade_id": r.entidade_id, "lido": bool(r.lido),
                "quando": r.criado_em.isoformat() if r.criado_em else None,
            } for r in regs]
    except Exception:  # noqa: BLE001
        log.debug("falha ao listar notificações", exc_info=True)
        return []


def marcar_lidas(user_id: int) -> int:
    """Marca todas as notificações próprias como lidas. Retorna quantas."""
    try:
        from .db import SessionLocal
        from .models import Notificacao
        with SessionLocal() as db:
            n = (db.query(Notificacao)
                 .filter(Notificacao.user_id == user_id, Notificacao.lido == False)  # noqa: E712
                 .update({Notificacao.lido: True}))
            db.commit()
            return n
    except Exception:  # noqa: BLE001
        return 0
