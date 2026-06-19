from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, DateTime, Date, Float, Boolean, ForeignKey, UniqueConstraint
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
