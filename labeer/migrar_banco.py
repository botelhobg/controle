"""Conferir um snapshot SQLite ou copiar para um PostgreSQL novo e dedicado."""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
from contextlib import closing, contextmanager
from itertools import zip_longest
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, func, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from .config import DeploymentError, postgres_url
from .db import Base, POSTGRES_SCHEMA, lock_postgres_write, postgres_engine, prepare_postgres_namespace
from .models import ReportDelivery, ReportVersion


class MigrationError(ValueError):
    """Entrada ou comparação recusada; não inclui credenciais nem resultados."""


@contextmanager
def _snapshot(path: Path):
    if not path.is_file():
        raise MigrationError("Informe um arquivo SQLite existente. Nenhum arquivo novo foi criado.")
    uri = path.resolve().as_uri() + "?mode=ro"
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    try:
        with engine.connect() as connection:
            # Todas as leituras observam o mesmo snapshot; a conexão nunca grava a origem.
            connection.exec_driver_sql("BEGIN")
            yield connection
    finally:
        engine.dispose()


def _plan(source: Connection) -> dict[str, int]:
    tables = Base.metadata.sorted_tables
    inspection = inspect(source)
    if set(inspection.get_table_names()) != {t.name for t in tables}:
        raise MigrationError("A origem precisa ser um banco completo do LaBeer. Abra uma cópia na v0.6 ou v0.7 primeiro.")
    for table in tables:
        if {c["name"] for c in inspection.get_columns(table.name)} != set(table.c.keys()):
            raise MigrationError(f"A tabela {table.name} não corresponde ao formato esperado do LaBeer.")
    if (source.exec_driver_sql("PRAGMA quick_check(1)").scalar_one() != "ok"
            or source.exec_driver_sql("PRAGMA foreign_key_check").first() is not None):
        raise MigrationError("O SQLite tem inconsistências. Confira a cópia de segurança.")

    for pdf, digest in source.execute(select(ReportVersion.pdf_bytes, ReportVersion.pdf_digest)):
        if not pdf or hashlib.sha256(pdf).hexdigest() != digest:
            raise MigrationError("Há um PDF cujo conteúdo não corresponde ao hash registrado.")
    mismatch = source.execute(
        select(ReportDelivery.id).join(ReportVersion, ReportVersion.id == ReportDelivery.report_id)
        .where(ReportDelivery.pdf_digest != ReportVersion.pdf_digest).limit(1)
    ).first()
    if mismatch is not None:
        raise MigrationError("Há um registro de envio que não corresponde ao PDF da versão.")
    return {table.name: source.execute(select(func.count()).select_from(table)).scalar_one() for table in tables}


def preview(path: Path) -> dict[str, int]:
    """Somente leitura local. Nunca abre a conexão com PostgreSQL."""
    with _snapshot(path) as source:
        return _plan(source)


def _rows(connection: Connection, table):
    # Versões substitutas precisam ser inseridas depois de suas predecessoras.
    order = [table.c.sample_id, table.c.number] if table is ReportVersion.__table__ else list(table.primary_key)
    return connection.execute(select(table).order_by(*order)).mappings()


def _sequences(target: Connection) -> None:
    for table in Base.metadata.sorted_tables:
        pk = next(iter(table.primary_key))
        sequence = target.execute(
            text("SELECT pg_get_serial_sequence(:tablename, :columnname)"),
            {"tablename": f"{POSTGRES_SCHEMA}.{table.name}", "columnname": pk.name},
        ).scalar_one()
        if sequence is None:
            continue
        highest = target.execute(select(func.max(pk))).scalar_one()
        if highest is not None and highest > 0:
            target.execute(text("SELECT setval(CAST(:sequence AS regclass), :value, true)"),
                           {"sequence": sequence, "value": highest})


def migrate(path: Path, destination: Engine, confirmed_database: str) -> dict[str, int]:
    if destination.dialect.name != "postgresql" or confirmed_database != destination.url.database:
        raise MigrationError("O nome confirmado deve corresponder ao banco PostgreSQL de destino.")
    with _snapshot(path) as source:
        counts = _plan(source)
        with destination.begin() as target:
            lock_postgres_write(target)
            if target.execute(text("SELECT current_database()")).scalar_one() != confirmed_database:
                raise MigrationError("O PostgreSQL retornou um banco diferente do nome confirmado.")
            prepare_postgres_namespace(target)
            db = inspect(target)
            if (db.get_table_names(schema=POSTGRES_SCHEMA) or db.get_view_names(schema=POSTGRES_SCHEMA)
                    or db.get_materialized_view_names(schema=POSTGRES_SCHEMA)):
                raise MigrationError("O esquema labeer precisa estar vazio. Nenhuma tabela será sobrescrita.")
            Base.metadata.create_all(target)
            for table in Base.metadata.sorted_tables:
                with closing(_rows(source, table)) as records:
                    for batch in records.partitions(32):
                        target.execute(table.insert(), [dict(row) for row in batch])
                # Compara todos os campos (incluindo os BLOBs PDF), antes de confirmar.
                with closing(_rows(source, table)) as left, closing(_rows(target, table)) as right:
                    if any(a != b for a, b in zip_longest(left, right, fillvalue=None)):
                        raise MigrationError(f"A conferência divergiu em {table.name}. A migração foi cancelada.")
            _sequences(target)
        return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Migração pontual de um banco LaBeer.")
    parser.add_argument("sqlite", type=Path, help="cópia do arquivo SQLite da v0.6 ou v0.7")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--conferir", action="store_true", help="somente leitura local (padrão)")
    group.add_argument("--aplicar", metavar="NOME_DO_BANCO", help="gravar em um PostgreSQL vazio com este nome")
    args = parser.parse_args(argv)
    try:
        if args.aplicar is None:
            counts = preview(args.sqlite)
            print("Conferência local concluída. Nenhum dado foi enviado.")
        else:
            url = postgres_url(os.getenv("LABEER_MIGRATION_DATABASE_URL", ""))
            engine = postgres_engine(url)
            try:
                counts = migrate(args.sqlite, engine, args.aplicar)
            finally:
                engine.dispose()
            print("Cópia conferida e concluída. A origem foi mantida intacta.")
    except (MigrationError, DeploymentError) as error:
        parser.exit(2, f"Operação interrompida: {error}\n")
    except (SQLAlchemyError, OSError, ValueError, TypeError):
        parser.exit(2, "Não foi possível concluir. Confira a origem, o destino e a conexão, sem expor credenciais.\n")
    for table, count in counts.items():
        print(f"{table}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
