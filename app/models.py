from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, DateTime, Date, Float, Boolean, ForeignKey, UniqueConstraint, JSON
)

from .db import Base


class User(Base):
    """Cada usuário é um tenant isolado (sua própria conta Bling e seus dados)."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    nome = Column(String, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)


class OAuthToken(Base):
    """Token do Bling POR usuário (1 linha por tenant)."""

    __tablename__ = "bling_oauth_token"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AiUsage(Base):
    """Contador de uso da IA por usuário/dia (controle de custo comercial)."""

    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    dia = Column(Date, default=date.today, nullable=False)
    contador = Column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "dia", name="uq_ai_usage_user_dia"),)


class NfeConfig(Base):
    """Config do módulo de NF-e por tenant: modo automático + regra padrão de edição."""

    __tablename__ = "nfe_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    auto = Column(Boolean, default=False, nullable=False)               # toggle do modo automático
    desconto_tipo = Column(String, default="percentual", nullable=False)  # 'percentual' | 'valor'
    desconto_valor = Column(Float, default=0.0, nullable=False)
    remover_frete = Column(Boolean, default=True, nullable=False)
    # Código da situação "Pendente" na API do Bling. Na v3 costuma ser 1, mas deixamos
    # configurável para não arriscar erro fiscal caso a sua conta use outro código.
    situacao_pendente = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RadarAlvo(Base):
    """Um anúncio de concorrente monitorado, por tenant e por SKU."""

    __tablename__ = "radar_alvo"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)        # SKU do nosso produto
    nome = Column(String, nullable=True)                    # nome da loja/concorrente
    marketplace = Column(String, nullable=True)             # ex.: mercadolivre, shopee
    url = Column(String, nullable=False)                    # link do anúncio do concorrente
    ativo = Column(Boolean, default=True, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class RadarSnapshot(Base):
    """Foto do preço de um alvo num instante. O histórico nasce do acúmulo destas."""

    __tablename__ = "radar_snapshot"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    alvo_id = Column(Integer, ForeignKey("radar_alvo.id"), nullable=False, index=True)
    preco_normal = Column(Float, nullable=True)
    preco_oferta = Column(Float, nullable=True)
    coletado_em = Column(DateTime, default=datetime.utcnow, index=True)


class PrecificacaoConfig(Base):
    """Configuração de precificação por tenant: custos globais + taxas por canal.

    A coluna `canais` guarda (JSON) a lista de canais, cada um com suas FAIXAS de preço:
    [{canal, nome, ativo, faixas:[{ate, comissao, fixo, fixo_pct}]}].
    `ate` = teto da faixa (None = sem teto / catch-all).
    """

    __tablename__ = "precificacao_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)

    # custos globais em % (incidem sobre o preço de venda)
    imposto = Column(Float, default=12.0, nullable=False)
    cartao = Column(Float, default=2.5, nullable=False)
    # custos por unidade em R$ (somados ao custo do produto)
    embalagem = Column(Float, default=0.0, nullable=False)
    frete = Column(Float, default=0.0, nullable=False)
    # margem líquida desejada padrão (%)
    margem_padrao = Column(Float, default=20.0, nullable=False)

    canais = Column(JSON, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OAuthState(Base):
    """State do OAuth do Bling, guardado no banco (uso único, TTL curto).

    Padrão correto de CSRF para OAuth: imune a redeploy e a troca de JWT_SECRET,
    e sem risco de truncamento (token curto em vez de um JWT longo no state).
    """

    __tablename__ = "oauth_state"

    state = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)


class WebhookEvento(Base):
    """Log dos eventos recebidos do Bling via webhook (push em tempo real)."""

    __tablename__ = "webhook_eventos"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event = Column(String, nullable=True)        # ex.: "produto.updated"
    recurso = Column(String, nullable=True, index=True)  # ex.: "produto"
    acao = Column(String, nullable=True)         # ex.: "updated"
    event_id = Column(String, nullable=True, index=True)  # dedupe
    company_id = Column(String, nullable=True)
    entidade_id = Column(String, nullable=True)  # data.id
    payload = Column(JSON, nullable=True)
    processado = Column(Boolean, default=False)
    recebido_em = Column(DateTime, default=datetime.utcnow, index=True)


class ProdutoSync(Base):
    """Status de sincronização de um produto entre o app e o Bling.
    'enviado' quando empurramos uma alteração; 'confirmado' quando o webhook
    de produto.updated chega de volta. Pendente = enviado mas ainda não confirmado."""

    __tablename__ = "produto_sync"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    produto_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True)
    status = Column(String, default="enviado")     # enviado | confirmado | erro
    campos = Column(JSON, nullable=True)            # o que foi enviado por último
    enviado_em = Column(DateTime, nullable=True)
    confirmado_em = Column(DateTime, nullable=True)
    erro = Column(String, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "produto_id", name="uq_sync_user_produto"),)


class ProdutoCache(Base):
    """Cópia local (cache) do catálogo do Bling. Carregado uma vez por completo e
    mantido atualizado via webhook — assim as telas leem daqui e o Bling fica com folga."""

    __tablename__ = "produto_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    produto_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True, index=True)
    nome = Column(String, nullable=True)
    preco = Column(Float, default=0.0)
    custo = Column(Float, default=0.0)
    saldo = Column(Float, default=0.0)
    situacao = Column(String, nullable=True)   # Ativo / Inativo
    tipo = Column(String, nullable=True)
    dados = Column(JSON, nullable=True)        # payload bruto do produto
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "produto_id", name="uq_cache_user_produto"),)


class CatalogoSync(Base):
    """Estado da sincronização completa do catálogo (uma linha por usuário)."""

    __tablename__ = "catalogo_sync"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    status = Column(String, default="ocioso")   # ocioso | rodando | concluido | erro
    total = Column(Integer, default=0)           # total no cache
    paginas = Column(Integer, default=0)
    erro = Column(String, nullable=True)
    iniciado_em = Column(DateTime, nullable=True)
    concluido_em = Column(DateTime, nullable=True)


class ShopeeConta(Base):
    """Credenciais e tokens da Shopee por usuário (multi-tenant).
    O access_token expira em ~4h e é renovado pelo refresh_token automaticamente."""

    __tablename__ = "shopee_conta"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    shop_id = Column(String, nullable=True)
    access_token = Column(String, nullable=True)
    refresh_token = Column(String, nullable=True)
    expira_em = Column(DateTime, nullable=True)       # quando o access_token expira
    conectado_em = Column(DateTime, nullable=True)
    nome_loja = Column(String, nullable=True)
    ativo = Column(Boolean, default=True)


class ShopeeBoostItem(Base):
    """Produto na lista de auto-boost rotativo da Shopee.
    fixo=True => sempre impulsionado (pin, máx 5). Senão entra no rodízio por prioridade."""

    __tablename__ = "shopee_boost_item"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    item_id = Column(String, nullable=False)          # id do anúncio na Shopee
    nome = Column(String, nullable=True)
    fixo = Column(Boolean, default=False)             # pin
    prioridade = Column(Integer, default=0)           # maior = impulsiona antes
    ultimo_boost = Column(DateTime, nullable=True)    # quando foi impulsionado por último
    boost_ate = Column(DateTime, nullable=True)       # fim das 4h do boost atual
    impulsos = Column(Integer, default=0)             # contador de quantas vezes
    criado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_boost_user_item"),)


class ShopeeBoostConfig(Base):
    """Configuração do motor de auto-boost por usuário."""

    __tablename__ = "shopee_boost_config"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    ativo = Column(Boolean, default=False)            # liga/desliga o rodízio
    janela_inicio = Column(Integer, default=0)        # hora 0-23 (0 = sempre)
    janela_fim = Column(Integer, default=0)           # hora 0-23 (0 = sempre)
    criterio = Column(String, default="prioridade")   # prioridade | margem | giro | abc
    max_simultaneos = Column(Integer, default=5)      # teto da Shopee
    atualizado_em = Column(DateTime, default=datetime.utcnow)
