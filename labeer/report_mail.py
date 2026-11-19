"""Configuração SMTP, validação de endereços e transporte de uma mensagem MIME."""

from __future__ import annotations

import os
import re
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from .config import settings


PENDING = "pending"
ACCEPTED = "accepted"
PARTIAL = "partial"
FAILED = "failed"
UNKNOWN = "unknown"
EXTERNAL = "external"

MAX_RECIPIENTS = 20
SUBJECT_LIMIT = 150
BODY_LIMIT = 4000
_ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_\x60{|}~-]+"
_LOCAL = re.compile(rf"{_ATOM}(?:\.{_ATOM})*")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_DOMAIN = re.compile(rf"{_LABEL}(?:\.{_LABEL})+")


class MailConfigError(ValueError):
    pass


def parse_address(raw: str) -> str:
    address = raw.strip()
    local, sep, domain = address.rpartition("@")
    if (
        not sep or len(address) > 254 or len(local) > 64
        or not _LOCAL.fullmatch(local) or not _DOMAIN.fullmatch(domain)
    ):
        raise ValueError("Informe apenas endereços de e-mail válidos, sem nomes ou marcadores.")
    return f"{local}@{domain.lower()}"


def parse_recipients(raw: str) -> tuple[str, ...]:
    if len(raw) > 6000:
        raise ValueError("A lista de destinatários é muito longa.")
    items = [token.strip() for token in re.split(r"[,;\n]+", raw) if token.strip()]
    if not items or len(items) > MAX_RECIPIENTS:
        raise ValueError(f"Informe de 1 a {MAX_RECIPIENTS} destinatários.")
    recipients = {}
    for text in items:
        address = parse_address(text)
        recipients.setdefault(address.casefold(), address)
    return tuple(recipients.values())


def validate_content(subject: str, body: str) -> tuple[str, str]:
    if any(not char.isprintable() for char in subject):
        raise ValueError("O assunto deve estar em uma linha de texto.")
    subject, body = subject.strip(), body.strip()
    if not subject or len(subject) > SUBJECT_LIMIT:
        raise ValueError(f"Informe um assunto de até {SUBJECT_LIMIT} caracteres.")
    if not body or len(body) > BODY_LIMIT or "\x00" in body:
        raise ValueError(f"Informe uma mensagem de até {BODY_LIMIT} caracteres.")
    return subject, body


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    security: str
    sender: str
    username: str
    password: str = field(repr=False)

    def __post_init__(self):
        if self.security not in {"starttls", "ssl"} or not 1 <= self.port <= 65535:
            raise MailConfigError("Configure a porta e o modo de TLS do servidor SMTP.")
        if not _DOMAIN.fullmatch(self.host) or not self.username or not self.password:
            raise MailConfigError("Preencha servidor, usuário e credencial SMTP nas configurações locais.")
        try:
            sender = parse_address(self.sender)
        except ValueError:
            raise MailConfigError("Configure um endereço de remetente SMTP válido.") from None
        object.__setattr__(self, "sender", sender)


def smtp_from_environment() -> SMTPConfig | None:
    if settings.is_cloud_pilot or os.getenv("LABEER_SMTP_ENABLED", "").strip().lower() not in {"1", "true"}:
        return None
    security = os.getenv("LABEER_SMTP_SECURITY", "starttls").strip().lower()
    default_port = "465" if security == "ssl" else "587"
    try:
        port = int(os.getenv("LABEER_SMTP_PORT", "").strip() or default_port)
    except ValueError:
        raise MailConfigError("A porta SMTP precisa ser um número.") from None
    return SMTPConfig(
        host=os.getenv("LABEER_SMTP_HOST", "").strip(),
        port=port, security=security, sender=os.getenv("LABEER_SMTP_FROM", ""),
        username=os.getenv("LABEER_SMTP_USER", "").strip(), password=os.getenv("LABEER_SMTP_PASSWORD", ""),
    )


@dataclass(frozen=True)
class TransportResult:
    status: str
    accepted: tuple[str, ...] = ()
    detail: str | None = None


def compose_message(
    *, sender: str, recipients: tuple[str, ...], subject: str, body: str,
    message_id: str, date: datetime, pdf: bytes, filename: str,
) -> EmailMessage:
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = format_datetime(date.replace(tzinfo=UTC))
    message.set_content(body, cte="quoted-printable")
    message.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    return message


def submit_smtp(settings: SMTPConfig, message: EmailMessage, recipients: tuple[str, ...]) -> TransportResult:
    client = None
    sending = False
    try:
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        if settings.security == "ssl":
            client = smtplib.SMTP_SSL(settings.host, settings.port, context=context, timeout=12)
        else:
            client = smtplib.SMTP(settings.host, settings.port, timeout=12)
            client.starttls(context=context)
            client.ehlo()
        client.login(settings.username, settings.password)
        sending = True
        refused = client.send_message(message, from_addr=settings.sender, to_addrs=list(recipients))
        rejected = {addr.casefold() for addr in refused}
        if not rejected.issubset({addr.casefold() for addr in recipients}):
            return TransportResult(UNKNOWN, detail="A resposta do servidor não pôde ser associada aos destinatários.")
        accepted = tuple(addr for addr in recipients if addr.casefold() not in rejected)
        if not accepted:
            return TransportResult(FAILED, detail="O servidor recusou todos os destinatários.")
        if rejected:
            return TransportResult(PARTIAL, accepted, "Um ou mais destinatários foram recusados.")
        return TransportResult(ACCEPTED, accepted)
    except smtplib.SMTPAuthenticationError:
        return TransportResult(FAILED, detail="O servidor não aceitou a credencial SMTP.")
    except smtplib.SMTPRecipientsRefused:
        return TransportResult(FAILED, detail="O servidor recusou todos os destinatários.")
    except smtplib.SMTPSenderRefused:
        return TransportResult(FAILED, detail="O servidor recusou o remetente configurado.")
    except smtplib.SMTPDataError as error:
        if 400 <= error.smtp_code < 600:
            return TransportResult(FAILED, detail=f"O servidor recusou a mensagem (SMTP {error.smtp_code}).")
        return TransportResult(UNKNOWN, detail="Resposta inesperada ao fim da transmissão.")
    except (OSError, smtplib.SMTPException):
        if sending:
            return TransportResult(UNKNOWN, detail="A conexão terminou sem confirmação do servidor.")
        return TransportResult(FAILED, detail="Não foi possível estabelecer a sessão SMTP com TLS.")
    finally:
        if client is not None:
            # O resultado do DATA não muda se a sessão terminar sem responder a QUIT.
            with suppress(OSError):
                client.close()
