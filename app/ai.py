"""Motor de IA — descrição "blindada contra devolução" via Gemini, com cota por tenant."""

from datetime import date

from fastapi import HTTPException

from .config import settings
from .db import SessionLocal
from .models import AiUsage

# Prompt consolidado (melhor de todas as versões Node: autoridade técnica +
# medidas absurdamente claras p/ reduzir devolução + linguagem p/ artesão).
PROMPT_BASE = """Atue como especialista em e-commerce focado em conversão e em REDUZIR DEVOLUÇÕES.
Crie uma descrição detalhada e comercial para o produto: {nome}.
Características conhecidas: {caracteristicas}.

Regras obrigatórias:
1. Autoridade técnica: explique usabilidade prática e aplicações reais.
2. Medidas absurdamente claras: reforce tamanhos e dimensões exatas para o cliente
   não imaginar um tamanho errado (ex.: se for 6mm, deixe explícito que é exatamente 6mm).
3. Linguagem clara para artesãos, profissional, sem palavras difíceis.
4. Antecipe dúvidas e destaque vantagens reais do material (ex.: não resseca,
   fácil de organizar por cor).
5. Sem asteriscos nem negritos exagerados.
6. Foco total em evitar devolução por falta de informação."""


PROMPT_SAC = """Você é um consultor parceiro da Sóstrass Acessórios e Pedrarias respondendo um cliente.
Regras:
1. Comece com cumprimento amigável (Olá, Oi) e valide o que o cliente disse.
2. Autoridade técnica: fale da utilidade prática do produto para artesanato (miçangas, pérolas).
3. Linguagem natural: nada de asteriscos, negritos, palavras difíceis ou tom robótico.
4. No máximo 480 caracteres. Assine como: Equipe Sóstrass Acessórios e Pedrarias.

Relato/avaliação do cliente:
"{relato}\""""

# Rodapé de "blindagem" — o aviso de direitos autorais do próprio lojista, anexado
# ao final da descrição quando blindar=True. Texto fixo do cliente (pode ser trocado).
RODAPE_BLINDAGEM = (
    "AVISO LEGAL DE DIREITOS AUTORAIS: As imagens e o layout deste anúncio são de "
    "propriedade exclusiva da DAJP / Sóstrass Acessórios e Pedrarias. Temos registro "
    "de autoria e possuímos os arquivos originais. É estritamente proibida a cópia."
)


def _checar_e_incrementar_cota(user_id: int):
    hoje = date.today()
    with SessionLocal() as db:
        uso = (db.query(AiUsage)
               .filter(AiUsage.user_id == user_id, AiUsage.dia == hoje)
               .first())
        atual = uso.contador if uso else 0
        if atual >= settings.ia_limite_diario:
            raise HTTPException(status_code=429, detail="Limite diário de IA atingido.")
        if uso is None:
            db.add(AiUsage(user_id=user_id, dia=hoje, contador=1))
        else:
            uso.contador += 1
        db.commit()


def _gerar_texto(user_id: int, prompt: str, modelo: str | None = None) -> str:
    """Chama o Gemini (texto), conferindo chave e cota. Override de modelo opcional."""
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY não configurada.")
    _checar_e_incrementar_cota(user_id)

    import google.generativeai as genai  # import tardio (dependência pesada)

    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(modelo or settings.gemini_model)
    return (model.generate_content(prompt).text or "").strip()


def gerar_descricao(user_id: int, nome_produto: str, caracteristicas: str = "",
                    blindar: bool = False, modelo: str | None = None) -> str:
    """Descrição comercial anti-devolução. Com blindar=True, anexa o rodapé jurídico."""
    prompt = PROMPT_BASE.format(nome=nome_produto, caracteristicas=caracteristicas or "—")
    texto = _gerar_texto(user_id, prompt, modelo)
    if blindar:
        texto = f"{texto}\n\n{RODAPE_BLINDAGEM}"
    return texto


def gerar_sac(user_id: int, relato: str, modelo: str | None = None) -> str:
    """Resposta de atendimento (SAC) humanizada no tom Sóstrass."""
    if not relato or not relato.strip():
        raise HTTPException(status_code=422, detail="Informe o relato/avaliação do cliente.")
    return _gerar_texto(user_id, PROMPT_SAC.format(relato=relato.strip()), modelo)


# Modelo de imagem do Gemini (o apelidado "Nano Banana"). É configurável porque o
# nome muda entre versões — ajuste para o modelo de imagem ao qual você tem acesso.
IMAGE_MODEL_DEFAULT = "gemini-2.0-flash-preview-image-generation"


def gerar_imagem(user_id: int, prompt: str, negativo: str = "",
                 modelo: str | None = None) -> dict:
    """Gera uma foto de produto via Gemini. Devolve {mime_type, imagem_base64}.

    Vídeo (Veo) NÃO entra aqui de propósito: é assíncrono (long-running), com cota
    baixa e custo alto — precisa de um fluxo próprio de polling, não deste endpoint.
    """
    if not prompt or not prompt.strip():
        raise HTTPException(status_code=422, detail="Descreva a cena (prompt) para gerar a imagem.")
    if not settings.gemini_api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY não configurada.")
    _checar_e_incrementar_cota(user_id)  # imagem consome a mesma cota diária de IA

    import base64

    import google.generativeai as genai  # import tardio

    genai.configure(api_key=settings.gemini_api_key)
    texto = prompt.strip()
    if negativo and negativo.strip():
        texto += f"\n\nEvite (negativo): {negativo.strip()}"

    model = genai.GenerativeModel(modelo or IMAGE_MODEL_DEFAULT)
    try:
        resp = model.generate_content(texto)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Falha na geração de imagem: {e}")

    for cand in getattr(resp, "candidates", []) or []:
        parts = getattr(getattr(cand, "content", None), "parts", []) or []
        for p in parts:
            inline = getattr(p, "inline_data", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                b64 = base64.b64encode(data).decode() if isinstance(data, bytes) else data
                return {"mime_type": getattr(inline, "mime_type", "image/png"),
                        "imagem_base64": b64}
    raise HTTPException(status_code=502,
                        detail="A IA não retornou imagem — confira o modelo de imagem configurado.")
