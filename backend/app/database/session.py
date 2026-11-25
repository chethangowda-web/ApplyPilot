from collections.abc import Generator
from functools import lru_cache
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from app.core.config import get_settings


def build_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


@lru_cache
def get_engine() -> Engine:
    return build_engine()


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    """Yield a transactional SQLAlchemy session."""
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()
