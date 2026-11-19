"""Vínculo entre a versão emitida, a confirmação de envio e o registro de e-mail."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import make_msgid
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .accounts import Grant, authorize_work
from .config import settings
from .db import session_scope
from .models import ReportCancellation, ReportDelivery, ReportVersion, Sample, utc_now
from .report_mail import (
    ACCEPTED, EXTERNAL, FAILED, PARTIAL, PENDING, UNKNOWN,
    SMTPConfig, TransportResult, compose_message, parse_recipients, submit_smtp, validate_content,
)
from .report_pdf import report_filename
from .reports import ReportError, digest, report_actor, verified_pdf


UNCERTAIN_GRACE = timedelta(seconds=90)
FINAL_OUTCOMES = {ACCEPTED, PARTIAL, FAILED, UNKNOWN}
STATUS_NAMES = {
    ACCEPTED: "Aceito pelo SMTP", EXTERNAL: "Envio externo informado",
    FAILED: "Não enviado", PARTIAL: "Aceite parcial", PENDING: "Aguardando confirmação",
    UNKNOWN: "Resultado incerto",
}


@dataclass(frozen=True)
class DeliveryDraft:
    report_id: int
    pdf_digest: str
    filename: str
    sender: str
    recipients: tuple[str, ...]
    subject: str
    body: str

    @property
    def fingerprint(self) -> str:
        return digest(asdict(self))


def draft_email(report: ReportVersion, addresses: str, subject: str, body: str, config: SMTPConfig) -> DeliveryDraft:
    to = parse_recipients(addresses)
    subject, body = validate_content(subject, body)
    code = json.loads(report.content_json)["sample"]["code"]
    return DeliveryDraft(report.id, report.pdf_digest, report_filename(code, report.number),
                         config.sender, to, subject, body)


def delivery_history(db: Session, report_id: int) -> list[ReportDelivery]:
    return list(db.scalars(select(ReportDelivery).where(ReportDelivery.report_id == report_id)
                          .order_by(ReportDelivery.id.desc())))


def targets(entry: ReportDelivery) -> tuple[str, ...]:
    return tuple(json.loads(entry.recipients_json))


def confirmed_addresses(entry: ReportDelivery) -> tuple[str, ...]:
    return targets(entry) if entry.resolution == "sent" else tuple(json.loads(entry.accepted_json))


def delivery_status(entry: ReportDelivery) -> str:
    if entry.resolution == "sent":
        return "Envio confirmado manualmente"
    if entry.resolution == "not_sent":
        return "Não envio confirmado manualmente"
    if entry.outcome == PENDING and utc_now() - entry.recorded_at >= UNCERTAIN_GRACE:
        return "Sem confirmação - conferir"
    return STATUS_NAMES[entry.outcome]


def _current_report(db: Session, report_id: int) -> ReportVersion:
    version = db.get(ReportVersion, report_id)
    if version is None:
        raise ReportError("Versão não encontrada.")
    latest_id = db.scalar(select(ReportVersion.id).where(ReportVersion.sample_id == version.sample_id)
                          .order_by(ReportVersion.number.desc()).limit(1))
    if latest_id != report_id or db.get(ReportCancellation, report_id) is not None:
        raise ReportError("Somente a versão vigente pode ser enviada ou registrada como novo envio.")
    return version


def _check_duplicates(db: Session, report_id: int, recipients: tuple[str, ...]) -> None:
    wanted = {address.casefold() for address in recipients}
    blocked = set()
    unresolved = set()
    for entry in delivery_history(db, report_id):
        blocked.update(address.casefold() for address in confirmed_addresses(entry))
        if entry.resolution is None and entry.outcome in {PENDING, UNKNOWN}:
            unresolved.update(address.casefold() for address in targets(entry))
    if wanted & unresolved:
        raise ReportError("Há uma tentativa sem confirmação para um desses destinatários. Confira o histórico antes de repetir.")
    if wanted & blocked:
        raise ReportError("Esta versão já tem envio registrado para um desses destinatários. "
                          "Retire os endereços já registrados antes de enviar aos demais.")


def _maybe_mark_sent(db: Session, report_id: int) -> None:
    try:
        version = _current_report(db, report_id)
    except ReportError:
        # A conferência de uma tentativa histórica não deve mudar o estado de outra versão.
        return
    entries = delivery_history(db, report_id)
    accepted = {addr.casefold() for row in entries for addr in confirmed_addresses(row)}
    intended = {
        addr.casefold()
        for row in entries
        if row.resolution != "not_sent" and row.outcome in {PARTIAL, PENDING, UNKNOWN}
        for addr in targets(row)
    }
    sample = db.get(Sample, version.sample_id)
    if accepted and intended.issubset(accepted):
        sample.status = "Laudo enviado"
    elif sample.status == "Laudo enviado":
        sample.status = "Laudo emitido"


def _finish(db: Session, entry: ReportDelivery, result: TransportResult) -> None:
    to = set(targets(entry))
    accepted = set(result.accepted)
    valid = {
        ACCEPTED: accepted == to,
        PARTIAL: bool(accepted) and accepted < to,
        FAILED: not accepted,
        UNKNOWN: not accepted,
    }
    if result.status not in FINAL_OUTCOMES or not valid[result.status]:
        result = TransportResult(UNKNOWN, detail="A resposta não pôde ser conciliada com os destinatários.")
    entry.outcome = result.status
    entry.accepted_json = json.dumps(result.accepted)
    entry.completed_at = utc_now()
    entry.detail = result.detail
    if result.accepted:
        entry.sent_at = entry.completed_at
    db.flush()
    _maybe_mark_sent(db, entry.report_id)


def send_report(
    draft: DeliveryDraft,
    config: SMTPConfig,
    actor_id: int,
    *,
    grant: Grant | None = None,
    transport: Callable | None = None,
) -> ReportDelivery:
    """Reserva antes de transmitir; uma falha sem resposta nunca dispara nova tentativa automática."""
    if settings.is_cloud_pilot:
        raise ReportError("Envio de e-mails desativado no piloto da nuvem.")

    def authorized_actor(db: Session):
        if grant is not None and authorize_work(db, grant) != actor_id:
            raise ReportError("A sessão não corresponde ao responsável pelo envio.")
        return report_actor(db, actor_id)

    with session_scope(lock_for_report=True) as db:
        actor = authorized_actor(db)
        version = _current_report(db, draft.report_id)
        fresh = draft_email(version, ", ".join(draft.recipients), draft.subject, draft.body, config)
        if fresh != draft:
            raise ReportError("A versão ou o e-mail mudou. Confira novamente a confirmação.")
        verified_pdf(db, version.id)
        _check_duplicates(db, version.id, draft.recipients)
        entry = ReportDelivery(
            report_id=version.id, channel="smtp", outcome=PENDING,
            sender=config.sender, recipients_json=json.dumps(draft.recipients), subject=draft.subject,
            message_id=make_msgid(domain=config.sender.split("@", 1)[1]), pdf_digest=version.pdf_digest,
            recorded_by_id=actor.id, recorded_by_name=actor.name,
        )
        db.add(entry)
        db.flush()
        entry_id = entry.id

    with session_scope(lock_for_report=True) as db:
        entry = db.get(ReportDelivery, entry_id)
        if entry.outcome != PENDING or entry.resolution is not None:
            raise ReportError("Esta tentativa já foi concluída ou conferida.")
        # A mesma reserva de escrita usada pela emissão/invalidação impede troca
        # de versão no intervalo entre a conferência e a submissão do anexo.
        try:
            authorized_actor(db)
            version = _current_report(db, draft.report_id)
            pdf = verified_pdf(db, version.id)
            if version.pdf_digest != entry.pdf_digest:
                raise ReportError("O arquivo não corresponde à versão confirmada.")
            message = compose_message(
                sender=config.sender, recipients=draft.recipients, subject=draft.subject, body=draft.body,
                message_id=entry.message_id, date=utc_now(), pdf=pdf, filename=draft.filename,
            )
        except (ReportError, ValueError):
            result = TransportResult(FAILED, detail="A versão ou a autorização mudou antes da transmissão.")
        else:
            result = (transport or submit_smtp)(config, message, draft.recipients)
        _finish(db, entry, result)
        return entry


def record_external(
    db: Session, report_id: int, addresses: str, sent_at: datetime, actor_id: int, *, message_id: str = "",
) -> ReportDelivery:
    actor = report_actor(db, actor_id)
    version = _current_report(db, report_id)
    verified_pdf(db, version.id)
    to = parse_recipients(addresses)
    _check_duplicates(db, report_id, to)
    if sent_at.tzinfo is None or sent_at.utcoffset() is None:
        raise ReportError("Informe o horário do envio com o fuso correto.")
    actual = sent_at.astimezone(UTC).replace(tzinfo=None, second=0, microsecond=0)
    start = version.issued_at.replace(second=0, microsecond=0)
    now = utc_now()
    if not start <= actual <= now:
        raise ReportError("O horário do envio deve estar entre a emissão desta versão e agora.")
    reference = message_id.strip()
    if reference and (len(reference) > 255 or not re.fullmatch(r"<[^<>@\s]+@[^<>@\s]+>", reference)):
        raise ReportError("Use o formato <identificador@domínio> ou deixe o Message-ID em branco.")
    entry = ReportDelivery(
        report_id=report_id, channel="external", outcome=EXTERNAL, recipients_json=json.dumps(to),
        accepted_json=json.dumps(to), message_id=reference or None, pdf_digest=version.pdf_digest,
        recorded_by_id=actor.id, recorded_by_name=actor.name, completed_at=now, sent_at=actual,
        detail="Registro declarado pelo operador; não passou pelo envio do aplicativo.",
    )
    db.add(entry)
    db.flush()
    _maybe_mark_sent(db, report_id)
    return entry


def can_reconcile(entry: ReportDelivery) -> bool:
    return entry.resolution is None and (
        entry.outcome == UNKNOWN
        or (entry.outcome == PENDING and utc_now() - entry.recorded_at >= UNCERTAIN_GRACE)
    )


def reconcile_delivery(db: Session, delivery_id: int, sent: bool, note: str, actor_id: int) -> ReportDelivery:
    actor = report_actor(db, actor_id, admin_only=True)
    entry = db.get(ReportDelivery, delivery_id)
    if entry is None or not can_reconcile(entry):
        raise ReportError("Só uma tentativa sem confirmação pode ser conferida.")
    if not note.strip() or len(note.strip()) > 500:
        raise ReportError("Registre a evidência usada para conferir a tentativa (até 500 caracteres).")
    entry.resolution = "sent" if sent else "not_sent"
    entry.resolution_note = note.strip()
    entry.resolved_by_id, entry.resolved_by_name = actor.id, actor.name
    entry.resolved_at = utc_now()
    db.flush()
    _maybe_mark_sent(db, entry.report_id)
    return entry
