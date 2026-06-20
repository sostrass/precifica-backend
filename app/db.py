from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

# Railway entrega "postgres://" em algumas versões; SQLAlchemy quer "postgresql://".
url = settings.database_url or "sqlite:///./dev.db"
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}

engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db():
    from . import models  # noqa: F401  (garante que os modelos sejam registrados)
    Base.metadata.create_all(bind=engine)


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
