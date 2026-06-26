from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

# Railway entrega "postgres://" em algumas versões; SQLAlchemy quer "postgresql://".
url = settings.database_url or "sqlite:///./dev.db"
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}

if url.startswith("sqlite"):
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
else:
    # Postgres (Railway): conexões ociosas são derrubadas; recycle evita travas em
    # conexão morta. Pool com folga para o threadpool e timeout curto para falhar
    # rápido em vez de pendurar a requisição quando o pool esgota.
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=15,
        pool_recycle=280,
        connect_args=connect_args,
    )
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db():
    from . import models  # noqa: F401  (garante que os modelos sejam registrados)
    Base.metadata.create_all(bind=engine)


def garantir_colunas_extras():
    """Adiciona colunas novas em tabelas já existentes de forma idempotente (Postgres e SQLite),
    sem depender de uma migration Alembic manual. Seguro rodar a cada boot."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    alvos = {
        "shopee_boost_config": [
            ("auto_selecao", "BOOLEAN DEFAULT FALSE"),
            ("auto_estrategia", "VARCHAR DEFAULT 'estoque_parado'"),
            ("auto_maximo", "INTEGER DEFAULT 30"),
            ("cond_ativo", "BOOLEAN DEFAULT FALSE"),
            ("cond_gatilho_pct", "FLOAT DEFAULT 0"),
            ("cond_max", "INTEGER DEFAULT 3"),
        ],
        "shopee_boost_item": [
            ("auto", "BOOLEAN DEFAULT FALSE"),
            ("condicional", "BOOLEAN DEFAULT FALSE"),
            ("motivo", "VARCHAR"),
        ],
        "shopee_promo_config": [
            ("base_comparacao", "VARCHAR DEFAULT 'dia'"),
            ("dias_analise", "INTEGER DEFAULT 30"),
        ],
        "shopee_review_config": [
            ("auto_pausa_seg", "INTEGER DEFAULT 5"),
            ("auto_max_ciclo", "INTEGER DEFAULT 10"),
        ],
        "shopee_venda_snapshot": [
            ("pedidos_6h", "INTEGER DEFAULT 0"),
            ("bucket", "INTEGER DEFAULT 0"),
        ],
        "webhook_eventos": [
            ("resultado", "JSON"),
        ],
        "nfe_config": [
            ("desconto_plataformas", "JSON"),
        ],
    }
    try:
        with engine.begin() as conn:
            for tabela, cols in alvos.items():
                if not insp.has_table(tabela):
                    continue
                existentes = {c["name"] for c in insp.get_columns(tabela)}
                for nome, tipo in cols:
                    if nome not in existentes:
                        conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))
    except Exception:  # noqa: BLE001 — nunca derruba o boot por causa disso
        pass


def run_migrations():
    """Sobe o schema com Alembic, seguro para banco novo OU já existente.

    - Banco novo (sem tabelas)        -> upgrade head (cria tudo).
    - Banco já existente sem Alembic  -> stamp head (marca como atual, NÃO recria nada).
    - Banco já versionado             -> upgrade head (aplica migrations pendentes).
    """
    import os
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # onde está o alembic.ini
    cfg = Config(os.path.join(raiz, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(raiz, "alembic"))

    tabelas = set(inspect(engine).get_table_names())
    if "alembic_version" not in tabelas and "users" in tabelas:
        # Banco anterior ao Alembic: já tem o esquema inicial. Carimba na revisão
        # INICIAL (não em head) e então sobe as migrações seguintes — assim as
        # tabelas novas (ex.: oauth_state) são criadas sem recriar as antigas.
        command.stamp(cfg, "5bbde79adba9")
        command.upgrade(cfg, "head")
    else:
        command.upgrade(cfg, "head")  # banco novo cria tudo; versionado aplica pendentes
