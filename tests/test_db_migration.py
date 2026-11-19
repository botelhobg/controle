import hashlib
import os
import sqlite3
from datetime import UTC
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

from labeer.accounts import bootstrap_admin, login
from labeer.config import postgres_url
from labeer.db import Base, POSTGRES_SCHEMA, _WRITE_LOCK, lock_postgres_write, postgres_engine
from labeer.deliveries import record_external
from labeer.migrar_banco import MigrationError, main, migrate, preview
from labeer.models import AnalysisExecution, AnalysisParameter, ReportVersion, RequestedAnalysis, Sample, utc_now
from labeer.reports import build_draft, cancel_report, issue_report, record_review
from labeer.services import add_execution, seed_parameters


EXACT = Decimal("4.2345678901234567890123")


@pytest.fixture
def origin(tmp_path):
    source = tmp_path / "origem ficticia.db"
    engine = create_engine(f"sqlite:///{source}")
    Base.metadata.create_all(engine)
    with Session(engine) as db, db.begin():
        bootstrap_admin(db, "admin@example.test", "senha-somente-de-teste")
        grant = login(db, "admin@example.test", "senha-somente-de-teste")
        seed_parameters(db)
        sample = Sample(id=51, fq_number=999, brewery="Cervejaria Fictícia", product="Demonstração",
                        responsible_id=grant.user_id)
        parameter = db.scalar(select(AnalysisParameter).where(AnalysisParameter.key == "alcohol"))
        request = RequestedAnalysis(sample=sample, parameter=parameter, include_in_report=True)
        db.add(request)
        db.flush()
        add_execution(db, request, numeric_value=EXACT, text_value=None, notes="Antiga", actor_id=grant.user_id)
        review = record_review(db, build_draft(db, sample.id), grant.user_id)
        report = issue_report(db, review.id, grant.user_id)
        record_external(db, report.id, "primeiro@example.test", utc_now().replace(tzinfo=UTC),
                        grant.user_id, message_id="<teste@example.test>")
        add_execution(db, request, numeric_value=Decimal("4.5"), text_value=None, notes=None, actor_id=grant.user_id)
        corrected = record_review(db, build_draft(db, sample.id, reason="Correção fictícia"), grant.user_id)
        replacement = issue_report(db, corrected.id, grant.user_id)
        cancel_report(db, replacement.id, "Teste de invalidação", grant.user_id)
    engine.dispose()
    return source


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_preview_reads_old_data_and_never_connects_to_a_target(origin, monkeypatch, capsys):
    before = _digest(origin)
    monkeypatch.setenv("LABEER_MIGRATION_DATABASE_URL", "invalida-inutilizada")
    counts = preview(origin)
    assert counts["report_versions"] == 2
    assert counts["report_deliveries"] == counts["report_cancellations"] == 1
    assert counts["analysis_executions"] == 2 and counts["user_account_state"] == 1
    assert main([str(origin)]) == 0
    assert "Nenhum dado foi enviado" in capsys.readouterr().out
    assert _digest(origin) == before


def test_preview_refuses_inconsistent_pdf(origin):
    with sqlite3.connect(origin) as connection:
        connection.execute("UPDATE report_versions SET pdf_bytes = ?", (b"PDF de teste corrompido",))
    with pytest.raises(MigrationError, match="hash"):
        preview(origin)


def test_preview_refuses_missing_file_without_creating_it(tmp_path):
    absent = tmp_path / "ausente.db"
    with pytest.raises(MigrationError, match="existente"):
        preview(absent)
    assert not absent.exists()


def test_postgres_copy_and_write_lock(origin):
    uri = os.getenv("LABEER_QA_POSTGRES_URL")
    if not uri:
        pytest.skip("Teste opcional com PostgreSQL local descartável e TLS.")
    url = postgres_url(uri)
    assert url.host in {"localhost", "127.0.0.1", "::1"} and url.database.startswith("labeer_qa_")
    target = postgres_engine(url)
    try:
        before = _digest(origin)
        with pytest.raises(MigrationError, match="nome confirmado"):
            migrate(origin, target, "outro_banco")
        counts = migrate(origin, target, url.database)
        assert counts == preview(origin) and _digest(origin) == before
        with pytest.raises(MigrationError, match="vazio"):
            migrate(origin, target, url.database)

        with Session(target) as db, db.begin():
            assert db.scalar(select(AnalysisExecution.numeric_value).order_by(AnalysisExecution.id)) == EXACT
            reports = db.scalars(select(ReportVersion).order_by(ReportVersion.number)).all()
            assert reports[1].replaces_id == reports[0].id
            assert reports[1].cancellation is not None
            grant = login(db, "admin@example.test", "senha-somente-de-teste")
            new = Sample(fq_number=1000, brewery="Teste", product="Após cópia", responsible_id=grant.user_id)
            db.add(new)
            db.flush()
            assert new.id > 51
        with target.connect() as first, target.connect() as second:
            assert not inspect(first).get_table_names(schema="public")
            assert len(inspect(first).get_table_names(schema=POSTGRES_SCHEMA)) == len(counts)
            lock_postgres_write(first)
            statement = text("SELECT pg_try_advisory_xact_lock(CAST(:key AS bigint))")
            assert second.execute(statement, {"key": _WRITE_LOCK}).scalar_one() is False
            first.rollback()
            assert second.execute(statement, {"key": _WRITE_LOCK}).scalar_one() is True
    finally:
        target.dispose()
