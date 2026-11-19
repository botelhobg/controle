from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


def utc_now() -> datetime:
    """UTC sem fuso para manter portabilidade entre SQLite e PostgreSQL."""
    return datetime.now(UTC).replace(tzinfo=None)


def fq_code(number: int) -> str:
    return f"FQ{number:02d}"


class ResultDecimal(TypeDecorator[Decimal]):
    """Store exact decimals even in SQLite, which has no native decimal type."""

    impl = Numeric
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Text() if dialect.name == "sqlite" else Numeric())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not value.is_finite() or not -80 <= value.as_tuple().exponent <= 80:
            raise ValueError("Número inválido ou fora da precisão aceita.")
        return format(value, "f") if dialect.name == "sqlite" else value

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, Decimal):
            return value
        return Decimal(str(value))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(30), default="ANALISTA")
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AccountState(Base):
    """Controles de acesso separados para atualizar bancos antigos sem alterar users."""

    __tablename__ = "user_account_state"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(default=1)
    must_change: Mapped[bool] = mapped_column(default=False)
    temporary_until: Mapped[datetime | None] = mapped_column(DateTime)
    failures: Mapped[int] = mapped_column(default=0)
    failure_window: Mapped[datetime | None] = mapped_column(DateTime)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    fq_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    brewery: Mapped[str | None] = mapped_column(String(180))
    product: Mapped[str | None] = mapped_column(String(180))
    style: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    received_date: Mapped[date | None] = mapped_column(Date)
    due_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(50), default="Cadastrada")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)

    responsible: Mapped[User | None] = relationship()
    requested_analyses: Mapped[list[RequestedAnalysis]] = relationship(
        back_populates="sample", cascade="all, delete-orphan"
    )

    @property
    def code(self) -> str:
        return fq_code(self.fq_number)


class AnalysisParameter(Base):
    __tablename__ = "analysis_parameters"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default="measured")
    decimals: Mapped[int | None] = mapped_column(Integer, nullable=True)
    display_rule: Mapped[str | None] = mapped_column(String(80), nullable=True)
    formula_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    formula_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RequestedAnalysis(Base):
    __tablename__ = "requested_analyses"
    __table_args__ = (UniqueConstraint("sample_id", "parameter_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), index=True)
    parameter_id: Mapped[int] = mapped_column(ForeignKey("analysis_parameters.id"))
    status: Mapped[str] = mapped_column(String(40), default="Pendente")
    include_in_report: Mapped[bool | None] = mapped_column(Boolean)
    price_snapshot: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    sample: Mapped[Sample] = relationship(back_populates="requested_analyses")
    parameter: Mapped[AnalysisParameter] = relationship()
    executions: Mapped[list[AnalysisExecution]] = relationship(
        back_populates="requested_analysis", cascade="all, delete-orphan"
    )
    current_result: Mapped[CurrentResult | None] = relationship(
        back_populates="requested_analysis", cascade="all, delete-orphan", uselist=False
    )


class AnalysisExecution(Base):
    __tablename__ = "analysis_executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_analysis_id: Mapped[int] = mapped_column(ForeignKey("requested_analyses.id"), index=True)
    execution_number: Mapped[int] = mapped_column(Integer)
    numeric_value: Mapped[Decimal | None] = mapped_column(ResultDecimal(), nullable=True)
    text_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    raw_inputs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    performed_at: Mapped[datetime | None] = mapped_column(DateTime)
    performed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    requested_analysis: Mapped[RequestedAnalysis] = relationship(back_populates="executions")
    performed_by: Mapped[User | None] = relationship()


class CurrentResult(Base):
    __tablename__ = "current_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    requested_analysis_id: Mapped[int] = mapped_column(
        ForeignKey("requested_analyses.id"), unique=True, index=True
    )
    execution_id: Mapped[int] = mapped_column(ForeignKey("analysis_executions.id"))
    selected_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    selected_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    requested_analysis: Mapped[RequestedAnalysis] = relationship(back_populates="current_result")
    execution: Mapped[AnalysisExecution] = relationship(foreign_keys=[execution_id])
    selected_by: Mapped[User | None] = relationship(foreign_keys=[selected_by_id])


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(80))
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ReportReview(Base):
    """Confirmação explícita sobre uma prévia identificada pelo seu conteúdo."""

    __tablename__ = "report_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), index=True)
    draft_digest: Mapped[str] = mapped_column(String(64), index=True)
    options_json: Mapped[str] = mapped_column(Text)
    reviewed_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    reviewed_by_name: Mapped[str] = mapped_column(String(120))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ReportVersion(Base):
    """Cada emissão é acrescentada ao histórico. Seu PDF nunca é regenerado."""

    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("sample_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    review_id: Mapped[int] = mapped_column(ForeignKey("report_reviews.id"), unique=True)
    replaces_id: Mapped[int | None] = mapped_column(ForeignKey("report_versions.id"), unique=True)
    content_json: Mapped[str] = mapped_column(Text)
    content_digest: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    pdf_bytes: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    pdf_digest: Mapped[str] = mapped_column(String(64))
    issued_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    issued_by_name: Mapped[str] = mapped_column(String(120))
    issued_at: Mapped[datetime] = mapped_column(DateTime)

    review: Mapped[ReportReview] = relationship()
    cancellation: Mapped[ReportCancellation | None] = relationship(back_populates="report")


class ReportCancellation(Base):
    """Invalidar sem substituição é um registro separado; o PDF permanece intacto."""

    __tablename__ = "report_cancellations"

    report_id: Mapped[int] = mapped_column(ForeignKey("report_versions.id"), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    actor_name: Mapped[str] = mapped_column(String(120))
    canceled_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    report: Mapped[ReportVersion] = relationship(back_populates="cancellation")


class ReportDelivery(Base):
    """Uma tentativa ou um registro externo, sempre ligado aos bytes de uma versão."""

    __tablename__ = "report_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("report_versions.id"), index=True)
    channel: Mapped[str] = mapped_column(String(12))
    outcome: Mapped[str] = mapped_column(String(20))
    sender: Mapped[str | None] = mapped_column(String(254))
    recipients_json: Mapped[str] = mapped_column(Text)
    accepted_json: Mapped[str] = mapped_column(Text, default="[]")
    subject: Mapped[str | None] = mapped_column(String(150))
    message_id: Mapped[str | None] = mapped_column(String(255))
    pdf_digest: Mapped[str] = mapped_column(String(64))
    recorded_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    recorded_by_name: Mapped[str] = mapped_column(String(120))
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    detail: Mapped[str | None] = mapped_column(String(250))
    resolution: Mapped[str | None] = mapped_column(String(14))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    resolved_by_name: Mapped[str | None] = mapped_column(String(120))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)

