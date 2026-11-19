"""Regras, revisão e ciclo de vida dos laudos no banco local."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import DecimalException

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from .models import CurrentResult, ReportCancellation, ReportReview, ReportVersion, RequestedAnalysis, Sample, User, utc_now
from .report_pdf import RESULT_SLOTS, TEMPLATE_VERSION, render_report_pdf
from .services import format_report_result


LEGACY_REPORT_STATES = {"Laudo emitido", "Laudo enviado"}
REPORT_ROLES = {"ADMIN", "ANALISTA"}
NOTE_LIMIT = 800
REASON_LIMIT = 500
PARAMETER_ORDER = {key: i for i, key in enumerate(RESULT_SLOTS)}


class ReportError(ValueError):
    pass


def json_snapshot(data: dict) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(data: dict) -> str:
    return sha(json_snapshot(data).encode("utf-8"))


@dataclass(frozen=True)
class ReportDraft:
    sample_id: int
    number: int
    previous: ReportVersion | None
    legacy_status: str | None
    options: dict
    content: dict
    grid: list[dict]
    problems: list[str]
    document_digest: str
    review_digest: str

    @property
    def requires_reason(self) -> bool:
        return self.previous is not None or self.legacy_status is not None

    @property
    def replaces_number(self) -> int | None:
        return self.previous.number if self.previous is not None and self.previous.cancellation is None else None


def report_actor(db: Session, actor_id: int, *, admin_only: bool = False) -> User:
    actor = db.get(User, actor_id)
    allowed = {"ADMIN"} if admin_only else REPORT_ROLES
    if actor is None or not actor.active or actor.role not in allowed:
        raise ReportError("Apenas o Administrador pode realizar esta ação." if admin_only else
                          "Seu perfil não tem permissão para este fluxo.")
    return actor


def report_history(db: Session, sample_id: int) -> list[ReportVersion]:
    return list(db.scalars(
        select(ReportVersion)
        .where(ReportVersion.sample_id == sample_id)
        .options(joinedload(ReportVersion.cancellation), joinedload(ReportVersion.review))
        .order_by(ReportVersion.number.desc())
    ))


def version_status(version: ReportVersion, latest_number: int) -> str:
    if version.cancellation is not None:
        return "Invalidada"
    return "Vigente" if version.number == latest_number else "Substituída"


def _display(parameter, execution) -> str:
    shown = format_report_result(parameter, execution.numeric_value, execution.text_value)
    if parameter.unit and shown.endswith(f" {parameter.unit}"):
        shown = shown[:-(len(parameter.unit) + 1)]
    return shown


def build_draft(
    db: Session,
    sample_id: int,
    *,
    decisions: dict[str, bool] | None = None,
    note: str = "",
    reason: str = "",
) -> ReportDraft:
    sample = db.scalar(select(Sample).where(Sample.id == sample_id).options(
        selectinload(Sample.requested_analyses).joinedload(RequestedAnalysis.parameter),
        selectinload(Sample.requested_analyses)
        .joinedload(RequestedAnalysis.current_result).joinedload(CurrentResult.execution),
    ))
    if sample is None:
        raise ReportError("A amostra não foi encontrada.")
    note, reason = note.strip(), reason.strip()
    choices = dict(decisions or {})
    history = report_history(db, sample.id)
    previous = history[0] if history else None
    legacy = sample.status if previous is None and sample.status in LEGACY_REPORT_STATES else None
    number = previous.number + 1 if previous is not None else 1
    problems = []
    if len(note) > NOTE_LIMIT:
        problems.append(f"Use até {NOTE_LIMIT} caracteres na observação para o PDF.")
    if (previous is not None or legacy is not None) and not reason:
        problems.append("Informe o motivo da reemissão ou correção.")
    if len(reason) > REASON_LIMIT:
        problems.append(f"Use até {REASON_LIMIT} caracteres no motivo.")

    ordered = sorted(sample.requested_analyses, key=lambda req: (
        PARAMETER_ORDER.get(req.parameter.key, len(PARAMETER_ORDER)),
        req.parameter.name, req.id,
    ))
    unknown = {str(req.id) for req in ordered if req.include_in_report is None}
    if any(k not in unknown or type(v) is not bool for k, v in choices.items()):
        problems.append("A seleção de análises mudou. Confira novamente quais resultados incluir.")
    grid, results = [], []
    for request in ordered:
        param = request.parameter
        decision = request.include_in_report if request.include_in_report is not None else choices.get(str(request.id))
        if decision is None:
            problems.append(f"Defina se {param.name} deve entrar no laudo.")
        active = request.current_result.execution if request.current_result else None
        has_value = active is not None and (
            active.numeric_value is not None or bool((active.text_value or "").strip())
        )
        shown = None
        if has_value:
            try:
                shown = _display(param, active)
            except (DecimalException, ValueError):
                shown = None
        if decision is True:
            if shown is None or request.status != "Concluída":
                problems.append(f"{param.name} foi incluído, mas não tem resultado vigente concluído.")
            else:
                results.append({
                    "key": param.key, "name": param.name, "unit": param.unit, "display": shown,
                    "numeric": str(active.numeric_value) if active.numeric_value is not None else None,
                    "textual": active.text_value, "analysis_id": request.id,
                    "execution_id": active.id, "execution_number": active.execution_number,
                    "performed_at": active.performed_at.isoformat() if active.performed_at else None,
                })
        grid.append({
            "request_id": request.id, "name": param.name, "unit": param.unit, "status": request.status,
            "default": request.include_in_report, "included": decision, "display": shown,
        })
    if not results:
        problems.append("Inclua pelo menos um resultado concluído.")
    content = {
        "template_version": TEMPLATE_VERSION,
        "sample": {
            "code": sample.code, "brewery": sample.brewery, "product": sample.product,
            "lot": sample.lot, "style": sample.style,
            "received_on": sample.received_date.isoformat() if sample.received_date else None,
        },
        "results": results, "note": note or None,
    }
    document_digest = digest(content)
    if previous is not None and previous.cancellation is None and previous.content_digest == document_digest:
        problems.append("Já existe um laudo vigente com esse conteúdo. Não é necessária outra versão.")
    options = {"decisions": choices, "note": note, "reason": reason}
    review_digest = digest({
        "sample_id": sample.id, "number": number,
        "base_id": previous.id if previous else None,
        "base_invalidated": previous is not None and previous.cancellation is not None,
        "legacy_status": legacy, "document_digest": document_digest,
        "scopes": [(r["request_id"], r["default"], r["included"]) for r in grid],
        "options": options,
    })
    return ReportDraft(
        sample.id, number, previous, legacy, options, content, grid, problems, document_digest, review_digest,
    )


def _require_ready(draft: ReportDraft) -> None:
    if draft.problems:
        raise ReportError(" ".join(draft.problems))


def unissued_review(db: Session, sample_id: int, review_digest: str | None = None) -> ReportReview | None:
    used = select(ReportVersion.id).where(ReportVersion.review_id == ReportReview.id).exists()
    statement = select(ReportReview).where(ReportReview.sample_id == sample_id, ~used)
    if review_digest is not None:
        statement = statement.where(ReportReview.draft_digest == review_digest)
    return db.scalar(statement.order_by(ReportReview.id.desc()).limit(1))


def pending_review(db: Session, draft: ReportDraft) -> ReportReview | None:
    return None if draft.problems else unissued_review(db, draft.sample_id, draft.review_digest)


def record_review(db: Session, draft: ReportDraft, actor_id: int) -> ReportReview:
    actor = report_actor(db, actor_id)
    fresh = build_draft(db, draft.sample_id, **draft.options)
    if fresh.review_digest != draft.review_digest:
        raise ReportError("A prévia mudou. Confira os dados antes de confirmar a revisão.")
    _require_ready(fresh)
    recorded = pending_review(db, fresh)
    if recorded is not None:
        return recorded
    review = ReportReview(
        sample_id=fresh.sample_id, draft_digest=fresh.review_digest, options_json=json_snapshot(fresh.options),
        reviewed_by_id=actor.id, reviewed_by_name=actor.name,
    )
    db.add(review)
    db.flush()
    return review


def issue_report(db: Session, review_id: int, actor_id: int) -> ReportVersion:
    actor = report_actor(db, actor_id)
    review = db.get(ReportReview, review_id)
    if review is None:
        raise ReportError("Confirme a revisão da prévia antes de emitir.")
    if db.scalar(select(ReportVersion.id).where(ReportVersion.review_id == review_id)) is not None:
        raise ReportError("A revisão já foi usada em uma emissão.")
    draft = build_draft(db, review.sample_id, **json.loads(review.options_json))
    if draft.review_digest != review.draft_digest:
        raise ReportError("Os dados ou o histórico mudaram após a revisão. Faça uma nova conferência.")
    _require_ready(draft)
    timestamp = utc_now()
    pdf = render_report_pdf(
        draft.content, number=draft.number, issued_at=timestamp, replaces_number=draft.replaces_number,
    )
    new = ReportVersion(
        sample_id=review.sample_id, number=draft.number, review_id=review.id,
        replaces_id=draft.previous.id if draft.replaces_number is not None else None,
        content_json=json_snapshot(draft.content), content_digest=draft.document_digest,
        reason=draft.options["reason"] or None, pdf_bytes=pdf, pdf_digest=sha(pdf),
        issued_by_id=actor.id, issued_by_name=actor.name, issued_at=timestamp,
    )
    db.add(new)
    db.get(Sample, draft.sample_id).status = "Laudo emitido"
    db.flush()
    return new


def cancel_report(db: Session, report_id: int, reason: str, actor_id: int) -> ReportCancellation:
    actor = report_actor(db, actor_id, admin_only=True)
    reason = reason.strip()
    if not reason or len(reason) > REASON_LIMIT:
        raise ReportError("Informe um motivo para invalidar o laudo (até 500 caracteres).")
    target = db.get(ReportVersion, report_id)
    if target is None:
        raise ReportError("Versão não encontrada.")
    latest = report_history(db, target.sample_id)[0]
    if target.id != latest.id or latest.cancellation is not None:
        raise ReportError("Só a versão vigente pode ser invalidada sem substituição.")
    cancellation = ReportCancellation(report=target, reason=reason, actor_id=actor.id, actor_name=actor.name)
    db.add(cancellation)
    db.get(Sample, target.sample_id).status = "Laudo invalidado"
    db.flush()
    return cancellation


def verified_pdf(db: Session, report_id: int) -> bytes:
    report = db.get(ReportVersion, report_id)
    if report is None:
        raise ReportError("Versão não encontrada.")
    pdf = report.pdf_bytes
    if sha(pdf) != report.pdf_digest:
        raise ReportError("O PDF armazenado não passou na conferência de integridade. Restaure uma cópia do banco.")
    return pdf


def changes_between(before: dict, after: dict) -> list[dict[str, str]]:
    changes = []

    def compare(name, old, new):
        if old != new:
            changes.append({"Campo": name, "Antes": old or "---", "Depois": new or "---"})

    labels = {
        "code": "Código FQ", "brewery": "Cervejaria", "product": "Nome da cerveja", "lot": "Lote",
        "style": "Estilo", "received_on": "Recebimento",
    }
    for k, label in labels.items():
        compare(label, before["sample"].get(k), after["sample"].get(k))
    compare("Observação", before.get("note"), after.get("note"))
    compare("Modelo de laudo", before.get("template_version"), after.get("template_version"))
    previous = {r["key"]: r for r in before["results"]}
    current = {r["key"]: r for r in after["results"]}

    def detail(row):
        if row is None:
            return None
        display = row["display"] + (f" {row['unit']}" if row["unit"] else "")
        raw = row["numeric"] or row["textual"]
        return f"{display} (registro: {raw}; execução {row['execution_number']})"

    for key in dict.fromkeys([*previous, *current]):
        old, new = previous.get(key), current.get(key)
        if old != new:
            changes.append({"Campo": (new or old)["name"], "Antes": detail(old) or "Não incluído",
                            "Depois": detail(new) or "Não incluído"})
    return changes
