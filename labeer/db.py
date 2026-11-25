from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

import certifi
from sqlalchemy import Connection, Engine, create_engine, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.schema import CreateSchema

from .config import DeploymentError, settings


class Base(DeclarativeBase):
    pass


# O mesmo identificador é usado por inicialização, migração e gravações da aplicação.
_WRITE_LOCK = 1279345221
POSTGRES_SCHEMA = "labeer"


def postgres_engine(url: URL) -> Engine:
    # O libpq binário pode não localizar as raízes do sistema no ambiente da nuvem.
    # Use o pacote portátil de CAs, preservando verify-full e qualquer CA explícita.
    if url.query.get("sslrootcert") == "system":
        url = url.update_query_dict({"sslrootcert": certifi.where()})
    return create_engine(
        url, connect_args={
            "connect_timeout": 8, "prepare_threshold": None, "gssencmode": "disable",
            "options": f"-csearch_path={POSTGRES_SCHEMA}",
        },
        isolation_level="READ COMMITTED", pool_pre_ping=True, pool_size=3, max_overflow=2,
    )


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = settings.database_target()
    if settings.is_cloud_pilot:
        return postgres_engine(url)
    return create_engine(url, connect_args={"check_same_thread": False}, pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def lock_postgres_write(connection: Connection | Session) -> None:
    # Reserva transacional: commit ou rollback liberam a próxima gravação.
    connection.execute(text("SET LOCAL lock_timeout = '8s'"))
    connection.execute(text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"), {"key": _WRITE_LOCK})


def prepare_postgres_namespace(connection: Connection) -> None:
    if not inspect(connection).has_schema(POSTGRES_SCHEMA):
        connection.execute(CreateSchema(POSTGRES_SCHEMA))
    if connection.execute(text("SELECT current_schema()")).scalar_one() != POSTGRES_SCHEMA:
        raise DeploymentError("A conexão precisa manter o esquema labeer como padrão. Use o endpoint direto.")


@contextmanager
def session_scope(*, lock_for_report: bool = False) -> Iterator[Session]:
    session = get_session_factory()()
    try:
        if lock_for_report:
            if get_engine().dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            else:
                lock_postgres_write(session)
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_database() -> None:
    from . import models  # noqa: F401

    with get_engine().begin() as connection:
        if connection.dialect.name == "postgresql":
            lock_postgres_write(connection)
            prepare_postgres_namespace(connection)
        Base.metadata.create_all(bind=connection)

