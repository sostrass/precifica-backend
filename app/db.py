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
