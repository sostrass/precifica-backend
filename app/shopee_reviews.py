"""IA de avaliações da Shopee — respostas no padrão da loja.

Dois modos:
- manual: a IA sugere um rascunho, você revisa/edita e envia.
- auto: o agente responde sozinho as notas configuradas (ex.: só 4 e 5 estrelas),
  deixando as notas baixas para você responder com cuidado.
"""
from __future__ import annotations

from . import ai, shopee
from .db import SessionLocal
from .models import ShopeeReviewConfig

TONS = {
    "caloroso": "caloroso, afetuoso e genuíno, como quem gosta de verdade de atender bem",
    "profissional": "profissional, educado e objetivo, transmitindo confiança e seriedade",
    "descontraido": "descontraído e simpático, leve e próximo, mas sem perder o respeito",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config(db, user_id: int) -> ShopeeReviewConfig:
    c = db.query(ShopeeReviewConfig).filter_by(user_id=user_id).first()
    if not c:
        c = ShopeeReviewConfig(user_id=user_id)
        db.add(c)
        db.commit()
        db.refresh(c)
    return c


def _serializar(c: ShopeeReviewConfig) -> dict:
    return {
        "modo": c.modo or "manual",
        "tom": c.tom or "caloroso",
        "limite_chars": c.limite_chars or 450,
        "assinatura": c.assinatura or "",
        "saudacao": c.saudacao or "",
        "instrucoes": c.instrucoes or "",
        "oferecer_chat": bool(c.oferecer_chat),
        "usar_nome": bool(c.usar_nome),
        "usar_emoji": bool(c.usar_emoji),
        "auto_estrelas": list(c.auto_estrelas or [4, 5]),
    }


def obter_config(user_id: int) -> dict:
    db = SessionLocal()
    try:
        return _serializar(_config(db, user_id))
    finally:
        db.close()


def salvar_config(user_id: int, payload: dict) -> dict:
    from datetime import datetime
    db = SessionLocal()
    try:
        c = _config(db, user_id)
        if "modo" in payload and payload["modo"] in ("manual", "auto"):
            c.modo = payload["modo"]
        if "tom" in payload and payload["tom"] in TONS:
            c.tom = payload["tom"]
        if "limite_chars" in payload:
            c.limite_chars = max(120, min(int(payload["limite_chars"]), 1000))
        if "assinatura" in payload:
            c.assinatura = str(payload["assinatura"])[:80]
        if "saudacao" in payload:
            c.saudacao = str(payload["saudacao"])[:80]
        if "instrucoes" in payload:
            c.instrucoes = str(payload["instrucoes"])[:1200]
        if "oferecer_chat" in payload:
            c.oferecer_chat = bool(payload["oferecer_chat"])
        if "usar_nome" in payload:
            c.usar_nome = bool(payload["usar_nome"])
        if "usar_emoji" in payload:
            c.usar_emoji = bool(payload["usar_emoji"])
        if "auto_estrelas" in payload:
            try:
                est = sorted({int(x) for x in payload["auto_estrelas"] if 1 <= int(x) <= 5})
                c.auto_estrelas = est or [4, 5]
            except Exception:  # noqa: BLE001
                pass
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return _serializar(c)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Prompt no padrão da loja
# --------------------------------------------------------------------------- #
def montar_prompt(cfg, nota, comentario, produto=None, nome=None, tom=None) -> str:
    tom_desc = TONS.get(tom or cfg.tom or "caloroso", TONS["caloroso"])
    coment = (comentario or "").strip() or "(o cliente não escreveu texto, deu só a nota)"
    nota = int(nota or 5)

    partes = [
        "Você é o dono de uma loja de armarinho e aviamentos no Brasil (miçangas, pérolas, strass, "
        "caixas organizadoras, materiais para bijuteria e artesanato) respondendo PUBLICAMENTE a uma "
        "avaliação de cliente na Shopee.",
        f"Tom da resposta: {tom_desc}.",
        f"Nota recebida: {nota}/5.",
    ]
    if produto:
        partes.append(f'Produto avaliado: "{produto}".')
    if nome and cfg.usar_nome:
        partes.append(f'Nome do cliente: "{nome}".')
    partes.append(f'O que o cliente escreveu: "{coment}".')

    regras = [
        f"Escreva em português do Brasil, com no máximo {cfg.limite_chars or 450} caracteres.",
        "Soe humano e específico ao que o cliente disse — proibido resposta genérica de robô.",
        "Não invente fatos nem prometa o que não pode cumprir.",
    ]
    if cfg.usar_nome and nome:
        regras.append("Pode cumprimentar pelo primeiro nome do cliente.")
    elif not cfg.usar_nome:
        regras.append("Não use o nome do cliente.")
    regras.append("Pode usar 1 ou 2 emojis leves, se combinar." if cfg.usar_emoji
                  else "Não use nenhum emoji.")

    if nota <= 3:
        regras.append("A nota é baixa: peça desculpas com sinceridade, demonstre que se importa "
                      "de verdade e que vai resolver. Nunca seja defensivo nem culpe o cliente.")
        if cfg.oferecer_chat:
            regras.append("Convide o cliente a chamar no chat da Shopee para resolver rápido.")
    else:
        regras.append("A nota é boa: agradeça com calor e convide a conferir os lançamentos / voltar a comprar.")

    if cfg.saudacao:
        regras.append(f'Comece a resposta com uma saudação no estilo: "{cfg.saudacao.strip()}".')
    if (cfg.instrucoes or "").strip():
        regras.append(f"Regras específicas da loja que você DEVE respeitar: {cfg.instrucoes.strip()}")
    if (cfg.assinatura or "").strip():
        regras.append(f'Finalize assinando exatamente como: "{cfg.assinatura.strip()}".')

    partes.append("Regras: " + " ".join(f"({i + 1}) {r}" for i, r in enumerate(regras)))
    partes.append("Responda SÓ com o texto final da resposta — sem aspas, sem rótulos, sem explicação.")
    return "\n".join(partes)


# --------------------------------------------------------------------------- #
# Sugerir (rascunho, sem enviar) e enviar
# --------------------------------------------------------------------------- #
def sugerir(user_id: int, nota, comentario, produto=None, nome=None, tom=None) -> str:
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
    finally:
        db.close()
    prompt = montar_prompt(cfg, nota, comentario, produto, nome, tom)
    texto = (ai._gerar_texto(user_id, prompt) or "").strip().strip('"')
    return texto


def enviar(user_id: int, comment_id, texto: str) -> dict:
    return shopee.responder_avaliacao(user_id, comment_id, texto)


# --------------------------------------------------------------------------- #
# Modo automático — o agente responde sozinho as notas configuradas
# --------------------------------------------------------------------------- #
def auto_responder(user_id: int, limite: int = 50) -> dict:
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        modo = cfg.modo
        alvos = set(cfg.auto_estrelas or [4, 5])
        cfg_snap = ShopeeReviewConfig(
            modo=cfg.modo, tom=cfg.tom, limite_chars=cfg.limite_chars,
            assinatura=cfg.assinatura, saudacao=cfg.saudacao, instrucoes=cfg.instrucoes,
            oferecer_chat=cfg.oferecer_chat, usar_nome=cfg.usar_nome, usar_emoji=cfg.usar_emoji,
        )
    finally:
        db.close()
    if modo != "auto":
        return {"acao": "manual", "respondidos": 0}

    try:
        r = shopee.listar_avaliacoes(user_id, status="UNANSWERED", limite=limite)
    except shopee.ShopeeError as e:
        return {"acao": "erro", "erro": str(e), "respondidos": 0}

    comentarios = (r.get("response") or {}).get("item_comment_list") or []
    respondidos, ignorados = 0, 0
    for c in comentarios:
        if (c.get("comment_reply") or {}).get("reply"):
            continue
        nota = c.get("rating_star")
        if nota not in alvos:
            ignorados += 1
            continue
        prompt = montar_prompt(cfg_snap, nota, c.get("comment"),
                               None, c.get("buyer_username"), None)
        try:
            texto = (ai._gerar_texto(user_id, prompt) or "").strip().strip('"')
            if not texto:
                continue
            shopee.responder_avaliacao(user_id, c.get("comment_id"), texto)
            respondidos += 1
        except Exception:  # noqa: BLE001 — um erro num comentário não derruba o lote
            continue
    return {"acao": "auto", "respondidos": respondidos,
            "ignorados_para_revisao": ignorados, "vistos": len(comentarios)}
