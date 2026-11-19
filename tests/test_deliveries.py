import smtplib
import ssl
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from labeer import deliveries, report_mail
from labeer.db import Base
from labeer.deliveries import (
    can_reconcile, confirmed_addresses, delivery_history, delivery_status, draft_email,
    reconcile_delivery, record_external, send_report, targets,
)
from labeer.models import AnalysisParameter, RequestedAnalysis, Sample, User, utc_now
from labeer.report_mail import (
    ACCEPTED, EXTERNAL, FAILED, PARTIAL, PENDING, UNKNOWN,
    SMTPConfig, TransportResult, compose_message, parse_recipients, smtp_from_environment, validate_content,
)
from labeer.reports import ReportError, build_draft, cancel_report, issue_report, record_review
from labeer.services import add_execution, seed_parameters


TO = ("um@example.test", "dois@example.test")
SUBJECT = "Laudo de teste - FQ999"
BODY = "Olá,\n\nSegue o PDF do ensaio fictício."


@pytest.fixture
def smtp():
    return SMTPConfig("smtp.example.test", 587, "starttls", "teste@example.test", "usuario", "credencial-ficticia")


@pytest.fixture
def fixture_db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'deliveries.db'}")
    Base.metadata.create_all(engine)

    @contextmanager
    def scope(*, lock_for_report=False):
        with Session(engine, expire_on_commit=False) as db:
            if lock_for_report:
                db.execute(text("BEGIN IMMEDIATE"))
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    monkeypatch.setattr(deliveries, "session_scope", scope)
    try:
        with scope() as db:
            seed_parameters(db)
            admin = User(email="admin@example.test", name="Administradora de teste", role="ADMIN")
            analyst = User(email="analista@example.test", name="Analista de teste", role="ANALISTA")
            sample = Sample(fq_number=999, brewery="Cervejaria Fictícia", product="Demonstração")
            parameter = db.scalar(select(AnalysisParameter).where(AnalysisParameter.key == "alcohol"))
            analysis = RequestedAnalysis(sample=sample, parameter=parameter, include_in_report=True)
            db.add_all([admin, analyst, analysis])
            db.flush()
            add_execution(db, analysis, numeric_value=Decimal("4.2"), text_value=None, notes=None, actor_id=analyst.id)
            review = record_review(db, build_draft(db, sample.id), analyst.id)
            version = issue_report(db, review.id, analyst.id)
        yield SimpleNamespace(scope=scope, admin=admin.id, analyst=analyst.id, sample=sample.id, version=version)
    finally:
        engine.dispose()


def _mail(fixture_db, smtp, recipients=TO):
    return draft_email(fixture_db.version, ", ".join(recipients), SUBJECT, BODY, smtp)


def _accept(config, message, recipients):
    return TransportResult(ACCEPTED, recipients)


def test_recipient_parser_and_header_validation():
    assert parse_recipients("Um@Example.Test, um@example.test;\ndois@example.test") == ("Um@example.test", TO[1])
    assert parse_recipients("um+laboratorio@example.test") == ("um+laboratorio@example.test",)
    for value in ("", "Bia <um@example.test>", "x@example.test\nBcc: outro@example.test",
                  ".teste@example.test", "x@exemplo..test", "c\x00@example.test"):
        with pytest.raises(ValueError):
            parse_recipients(value)
    with pytest.raises(ValueError):
        validate_content("Laudo\r\nBcc: terceiro@example.test", "teste")


def test_mail_is_off_unless_explicitly_configured(monkeypatch):
    monkeypatch.setenv("LABEER_SMTP_ENABLED", "false")
    monkeypatch.setenv("LABEER_SMTP_HOST", "smtp.example.test")
    assert smtp_from_environment() is None
    monkeypatch.setenv("LABEER_SMTP_ENABLED", "true")
    monkeypatch.setenv("LABEER_SMTP_SECURITY", "plain")
    with pytest.raises(report_mail.MailConfigError, match="TLS"):
        smtp_from_environment()


def test_acceptance_records_exact_pdf_and_blocks_duplicate(fixture_db, smtp):
    env = fixture_db
    draft = _mail(env, smtp)

    def transport(config, message, recipients):
        assert recipients == TO and config.sender == smtp.sender
        with env.scope() as db:
            row, = delivery_history(db, env.version.id)
            assert row.outcome == PENDING
            assert message["Message-ID"] == row.message_id
        attachment, = tuple(message.iter_attachments())
        assert attachment.get_content_type() == "application/pdf"
        assert attachment.get_filename() == "Laudo_FQ999_v01.pdf"
        assert attachment.get_payload(decode=True) == env.version.pdf_bytes
        assert tuple(value.addr_spec for value in message["To"].addresses) == TO
        assert message["Date"] and message["Subject"] == SUBJECT
        return _accept(config, message, recipients)

    result = send_report(draft, smtp, env.analyst, transport=transport)
    assert result.outcome == ACCEPTED and result.sent_at is not None
    assert result.pdf_digest == env.version.pdf_digest
    assert result.sender == smtp.sender and result.recorded_by_id == env.analyst
    assert confirmed_addresses(result) == TO and targets(result) == TO
    with env.scope() as db:
        assert db.get(Sample, env.sample).status == "Laudo enviado"
    with pytest.raises(ReportError, match="já tem envio registrado"):
        send_report(draft, smtp, env.analyst, transport=_not_called)
    with env.scope() as db:
        assert len(delivery_history(db, env.version.id)) == 1


def _not_called(*args):
    pytest.fail("Nenhum transporte deveria ser chamado")


def test_partial_result_can_be_completed_without_sending_twice(fixture_db, smtp):
    env = fixture_db
    result = send_report(_mail(env, smtp), smtp, env.admin,
                         transport=lambda *_: TransportResult(PARTIAL, (TO[0],)))
    assert confirmed_addresses(result) == (TO[0],) and targets(result) == TO
    with env.scope() as db:
        assert db.get(Sample, env.sample).status == "Laudo emitido"
    with pytest.raises(ReportError, match="já tem envio registrado"):
        send_report(_mail(env, smtp), smtp, env.admin, transport=_not_called)
    send_report(_mail(env, smtp, (TO[1],)), smtp, env.admin, transport=_accept)
    with env.scope() as db:
        assert db.get(Sample, env.sample).status == "Laudo enviado"
        assert len(delivery_history(db, env.version.id)) == 2


def test_unknown_requires_manual_admin_reconciliation_before_retry(fixture_db, smtp):
    env = fixture_db
    draft = _mail(env, smtp)
    result = send_report(draft, smtp, env.analyst, transport=lambda *_: TransportResult(UNKNOWN))
    assert result.sent_at is None and confirmed_addresses(result) == ()
    with pytest.raises(ReportError, match="sem confirmação"):
        send_report(draft, smtp, env.admin, transport=_not_called)
    with env.scope() as db, pytest.raises(ReportError, match="Apenas o Administrador"):
        reconcile_delivery(db, result.id, False, "Conferência de teste", env.analyst)
    with env.scope() as db:
        verified = reconcile_delivery(db, result.id, False, "Servidor confirmou que não houve submissão.", env.admin)
        assert verified.outcome == UNKNOWN and verified.resolution == "not_sent"
        assert verified.resolved_by_id == env.admin
    assert send_report(draft, smtp, env.analyst, transport=_accept).outcome == ACCEPTED


def test_interruption_leaves_reservation_and_does_not_repeat_transport(fixture_db, smtp):
    env = fixture_db
    draft = _mail(env, smtp)

    def interrupt(*args):
        raise RuntimeError("interrupção simulada")

    with pytest.raises(RuntimeError, match="interrupção"):
        send_report(draft, smtp, env.admin, transport=interrupt)
    with env.scope() as db:
        entry, = delivery_history(db, env.version.id)
        assert entry.outcome == PENDING and not can_reconcile(entry)
        entry.recorded_at -= timedelta(minutes=2)
    with pytest.raises(ReportError, match="sem confirmação"):
        send_report(draft, smtp, env.admin, transport=_not_called)
    with env.scope() as db:
        entry, = delivery_history(db, env.version.id)
        assert can_reconcile(entry)
        reconcile_delivery(db, entry.id, True, "Mensagem localizada com os dois destinatários.", env.admin)
        assert delivery_status(entry) == "Envio confirmado manualmente"
        assert db.get(Sample, env.sample).status == "Laudo enviado"


def test_external_record_is_explicit_and_preserves_user_provided_time(fixture_db):
    env = fixture_db
    sent = utc_now().replace(tzinfo=UTC)
    with env.scope() as db, pytest.raises(ReportError, match="horário do envio"):
        record_external(db, env.version.id, TO[0], sent - timedelta(days=1), env.analyst)
    with env.scope() as db:
        entry = record_external(db, env.version.id, TO[0], sent, env.analyst, message_id="<teste@example.test>")
        assert entry.outcome == EXTERNAL and entry.sender is None
        assert entry.sent_at == sent.replace(second=0, microsecond=0, tzinfo=None)
        assert entry.message_id == "<teste@example.test>"
        assert confirmed_addresses(entry) == (TO[0],)
        assert db.get(Sample, env.sample).status == "Laudo enviado"


def test_cannot_email_changed_or_superseded_or_invalidated_version(fixture_db, smtp):
    env = fixture_db
    old = _mail(env, smtp)
    with pytest.raises(ReportError, match="mudou"):
        send_report(replace(old, pdf_digest="incorreto"), smtp, env.admin, transport=_not_called)
    with env.scope() as db:
        review = record_review(db, build_draft(db, env.sample, note="Correção de teste", reason="Ajuste"), env.admin)
        newer = issue_report(db, review.id, env.admin)
        new_draft = draft_email(newer, TO[0], SUBJECT, BODY, smtp)
    with pytest.raises(ReportError, match="versão vigente"):
        send_report(old, smtp, env.admin, transport=_not_called)
    with env.scope() as db:
        cancel_report(db, newer.id, "Invalidação de teste", env.admin)
    with pytest.raises(ReportError, match="versão vigente"):
        send_report(new_draft, smtp, env.admin, transport=_not_called)


@pytest.mark.parametrize("security", ["starttls", "ssl"])
def test_smtp_transport_uses_validated_tls_and_reports_partial(monkeypatch, smtp, security):
    notes = []

    class FakeServer:
        def __init__(self, host, port, *, timeout, context=None):
            assert host == "smtp.example.test" and timeout <= 15
            if security == "ssl":
                assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
                notes.append("tls")

        def starttls(self, *, context):
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            notes.append("tls")

        def ehlo(self):
            assert notes == ["tls"]

        def login(self, user, password):
            assert notes == ["tls"]
            notes.append("login")

        def send_message(self, message, *, from_addr, to_addrs):
            assert notes == ["tls", "login"]
            assert from_addr == smtp.sender and to_addrs == list(TO)
            return {TO[1]: (550, b"diagnostico-ficticio")}

        def close(self):
            notes.append("closed")
            raise OSError("erro fictício ao encerrar")

    monkeypatch.setattr(smtplib, "SMTP", FakeServer)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeServer)
    m = compose_message(sender=smtp.sender, recipients=TO, subject=SUBJECT, body=BODY,
                        message_id="<qa@example.test>", date=utc_now(), pdf=b"%PDF-", filename="Laudo_FQ999_v01.pdf")
    response = report_mail.submit_smtp(replace(smtp, security=security), m, TO)
    assert response.status == PARTIAL and response.accepted == (TO[0],)
    assert "diagnostico-ficticio" not in response.detail and notes[-1] == "closed"


@pytest.mark.parametrize(("error", "expected"), [
    (smtplib.SMTPServerDisconnected("conexão caiu"), UNKNOWN),
    (smtplib.SMTPDataError(554, b"detalhe privado"), FAILED),
    (smtplib.SMTPRecipientsRefused(dict.fromkeys(TO, (550, b"detalhe privado"))), FAILED),
])
def test_smtp_error_is_classified_without_exposing_raw_diagnostics(monkeypatch, smtp, error, expected):
    class BrokenServer:
        def __init__(self, *args, **kwargs):
            pass

        def login(self, *args):
            pass

        def send_message(self, *args, **kwargs):
            raise error

        def close(self):
            pass

    monkeypatch.setattr(smtplib, "SMTP_SSL", BrokenServer)
    response = report_mail.submit_smtp(replace(smtp, security="ssl"), object(), TO)
    assert response.status == expected and response.accepted == ()
    assert "detalhe privado" not in response.detail
