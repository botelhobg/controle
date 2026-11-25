from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from .calculations import FORMULAS, FORMULA_VERSION, INPUT_LABELS, calculate, parse_input
from .models import (
    AnalysisExecution,
    AnalysisParameter,
    AuditEvent,
    CurrentResult,
    RequestedAnalysis,
    Sample,
    User,
    utc_now,
)


PARAMETER_SEED = [
    ("alcohol", "Álcool", "% v/v", "measured", 1, None, "40.00"),
    ("ph", "pH", None, "measured", 2, None, "12.00"),
    ("apparent_extract", "Extrato aparente", "°P", "measured", 1, None, "28.00"),
    ("real_extract", "Extrato real", "°P", "calculated", 1, "real_extract", "0.00"),
    ("original_extract", "Extrato original", "°P", "calculated", 1, "original_extract", "0.00"),
    ("ibu", "Amargor", "IBU", "measured", 0, None, "28.00"),
    ("calories", "Calorias", "kcal/100 mL", "calculated", 0, "calories", "50.00"),
    ("carbohydrates", "Carboidratos", "g/100 mL", "calculated", 1, "carbohydrates", "45.00"),
    ("color", "Cor", "ASBC", "measured", 1, None, "12.00"),
    ("density", "Densidade", "g/cm³", "measured", 4, None, "12.00"),
    ("gluten", "Glúten", "ppm", "measured", 0, None, "320.00"),
    ("shelf_life", "Shelf life", "dias", "manual", 0, None, "100.00"),
]


def seed_parameters(session: Session) -> None:
    existing = set(session.scalars(select(AnalysisParameter.key)).all())
    for key, name, unit, kind, decimals, formula_key, price in PARAMETER_SEED:
        if key not in existing:
            session.add(
                AnalysisParameter(
                    key=key,
                    name=name,
                    unit=unit,
                    kind=kind,
                    decimals=decimals,
                    formula_key=formula_key,
                    formula_version=FORMULA_VERSION if formula_key else None,
                    price=Decimal(price),
                )
            )
    session.flush()


def seed_defaults(session: Session, admin_email: str, admin_password_hash: str) -> User:
    user = session.scalar(select(User).where(User.email == admin_email))
    if user is None:
        user = User(
            email=admin_email,
            name="Administrador LaBeer",
            role="ADMIN",
            password_hash=admin_password_hash,
        )
        session.add(user)
    seed_parameters(session)
    return user


def next_fq_number(session: Session) -> int:
    highest = session.scalar(select(func.max(Sample.fq_number)))
    return int(highest or 0) + 1


def deadline_for(received_date: date) -> date:
    return received_date + timedelta(days=20)


def create_sample(
    session: Session,
    *,
    brewery: str,
    product: str,
    style: str | None,
    lot: str | None,
    received_date: date,
    notes: str | None,
    responsible_id: int | None,
    parameter_ids: list[int],
    actor_id: int | None,
) -> Sample:
    sample = Sample(
        fq_number=next_fq_number(session),
        brewery=brewery.strip(),
        product=product.strip(),
        style=style.strip() if style else None,
        lot=lot.strip() if lot else None,
        received_date=received_date,
        due_date=deadline_for(received_date),
        notes=notes.strip() if notes else None,
        responsible_id=responsible_id,
    )
    session.add(sample)
    session.flush()

    parameters = session.scalars(
        select(AnalysisParameter).where(AnalysisParameter.id.in_(parameter_ids))
    ).all()
    for parameter in parameters:
        session.add(
            RequestedAnalysis(
                sample_id=sample.id,
                parameter_id=parameter.id,
                include_in_report=True,
                price_snapshot=parameter.price,
            )
        )
    session.add(
        AuditEvent(
            entity_type="sample",
            entity_id=sample.id,
            action="created",
            details_json=json.dumps({"code": sample.code, "analyses": len(parameters)}),
            user_id=actor_id,
        )
    )
    session.flush()
    return sample


def _append_execution(
    session: Session,
    requested: RequestedAnalysis,
    *,
    numeric_value: Decimal | None,
    text_value: str | None,
    notes: str | None,
    actor_id: int | None,
    raw_inputs_json: str | None = None,
) -> AnalysisExecution:
    execution = AnalysisExecution(
        requested_analysis=requested,
        execution_number=len(requested.executions) + 1,
        numeric_value=numeric_value,
        text_value=text_value.strip() if text_value else None,
        notes=notes.strip() if notes else None,
        performed_by_id=actor_id,
        performed_at=utc_now(),
        raw_inputs_json=raw_inputs_json,
    )
    session.add(execution)
    session.flush()

    if requested.current_result:
        requested.current_result.execution = execution
        requested.current_result.selected_by_id = actor_id
        requested.current_result.selected_at = utc_now()
    else:
        requested.current_result = CurrentResult(
            execution=execution,
            selected_by_id=actor_id,
        )
    requested.status = "Concluída"
    session.flush()
    return execution


CALCULATION_INPUTS = "calculation_inputs_saved"
MEASURED_INPUTS = ("alcohol", "apparent_extract")


@dataclass(frozen=True)
class CalculationUpdate:
    saved: tuple[str, ...]
    pending: tuple[str, ...]


def saved_brix(session: Session, sample_id: int) -> tuple[Decimal | None, int | None]:
    """Apenas leituras confirmadas; referências importadas nunca são aceitas implicitamente."""
    event = session.scalar(select(AuditEvent).where(
        AuditEvent.entity_type == "sample", AuditEvent.entity_id == sample_id,
        AuditEvent.action == CALCULATION_INPUTS,
    ).order_by(AuditEvent.id.desc()).limit(1))
    if event is None:
        return None, None
    value = json.loads(event.details_json)["brix"]
    return parse_input("brix", value), event.id


def calculation_snapshot(session: Session, sample_id: int) -> dict[str, int | None]:
    """Identifica a tela revisada, inclusive mudanças concorrentes de leitura ou escopo."""
    snapshot = dict(session.execute(
        select(AnalysisParameter.key, CurrentResult.execution_id).select_from(RequestedAnalysis)
        .join(RequestedAnalysis.parameter).outerjoin(RequestedAnalysis.current_result)
        .where(RequestedAnalysis.sample_id == sample_id)
    ).all())
    snapshot["__brix__"] = saved_brix(session, sample_id)[1]
    return snapshot


def _calculation_requests(session: Session, sample_id: int) -> dict[str, RequestedAnalysis]:
    return {r.parameter.key: r for r in session.scalars(
        select(RequestedAnalysis).where(RequestedAnalysis.sample_id == sample_id)
        .options(joinedload(RequestedAnalysis.parameter),
                 joinedload(RequestedAnalysis.current_result).joinedload(CurrentResult.execution),
                 selectinload(RequestedAnalysis.executions))
    )}


def _measured_values(requests: Mapping[str, RequestedAnalysis]) -> dict[str, Decimal | None]:
    values = {}
    for field in MEASURED_INPUTS:
        request = requests.get(field)
        execution = request.current_result.execution if request and request.current_result else None
        try:
            values[field] = parse_input(field, execution.numeric_value if execution else None)
        except ValueError:
            # Um resultado censurado ou fora do domínio não é uma entrada numérica válida.
            values[field] = None
    return values


def refresh_calculated_results(
    session: Session, sample_id: int, *, actor_id: int | None, changed_field: str | None = None,
) -> CalculationUpdate:
    """Acrescenta execuções dependentes na mesma transação; não altera medições nem PDFs."""
    requests = _calculation_requests(session, sample_id)
    brix, brix_event = saved_brix(session, sample_id)
    values = {**_measured_values(requests), "brix": brix}
    calculations = calculate(values)
    saved, pending, executions, invalidated = [], [], {}, {}
    for rule in FORMULAS:
        fields = [field for field, _ in rule.terms]
        request = requests.get(rule.key)
        if (request is None or request.parameter.kind != "calculated"
                or request.status not in {"Pendente", "Concluída"}
                or (changed_field is not None and changed_field not in fields)):
            continue
        result = calculations[rule.key]
        if result.value is None or not -80 <= result.value.as_tuple().exponent <= 80:
            pending.append(rule.key)
            if request.current_result:
                invalidated[rule.key] = request.current_result.execution_id
                # Remove só a seleção vigente. As execuções antigas continuam no histórico.
                request.current_result = None
            request.status = "Pendente"
            continue
        provenance = {
            "type": "calculated", "formula_key": rule.key,
            "formula_version": rule.version, "formula_source": rule.source,
            "inputs": {field: str(values[field]) for field in fields},
            "source_executions": {
                field: requests[field].current_result.execution_id for field in fields if field in MEASURED_INPUTS
            },
        }
        if "brix" in fields:
            provenance["brix_reading_id"] = brix_event
        raw = json.dumps(provenance, sort_keys=True, ensure_ascii=False)
        current = request.current_result.execution if request.current_result else None
        if (current and current.numeric_value == result.value and current.text_value is None
                and current.raw_inputs_json == raw and request.status == "Concluída"):
            continue
        execution = _append_execution(
            session, request, numeric_value=result.value, text_value=None, actor_id=actor_id,
            notes=f"Cálculo automático · fórmula {rule.key} v{rule.version}.", raw_inputs_json=raw,
        )
        saved.append(rule.key)
        executions[rule.key] = execution.id
    if saved or invalidated:
        session.add(AuditEvent(
            entity_type="sample", entity_id=sample_id, action="calculations_updated", user_id=actor_id,
            details_json=json.dumps({"executions": executions, "invalidated": invalidated,
                                     "pending": pending, "formula_version": FORMULA_VERSION}),
        ))
    session.flush()
    return CalculationUpdate(tuple(saved), tuple(pending))


def save_calculations(
    session: Session, sample_id: int, inputs: Mapping[str, Decimal | str | None], *, actor_id: int | None,
    expected_snapshot: Mapping[str, int | None],
) -> CalculationUpdate:
    """Confirma °Brix e grava só cálculos solicitados, sem transformar uma simulação em medição."""
    if session.get(Sample, sample_id) is None:
        raise ValueError("Amostra não encontrada. Selecione novamente.")
    if dict(expected_snapshot) != calculation_snapshot(session, sample_id):
        raise ValueError("Os resultados ou leituras mudaram. Reabra a amostra e confira os cálculos.")
    requests = _calculation_requests(session, sample_id)
    if not any(r.parameter.kind == "calculated" and r.status in {"Pendente", "Concluída"}
               and key in {rule.key for rule in FORMULAS} for key, r in requests.items()):
        raise ValueError("Esta amostra não tem parâmetros calculados solicitados em aberto.")
    values = {field: parse_input(field, inputs.get(field)) for field in INPUT_LABELS}
    measured = _measured_values(requests)
    if any(values[field] != measured[field] for field in MEASURED_INPUTS):
        raise ValueError("Para gravar, use álcool e extrato aparente vigentes. Registre a medição em Resultados.")
    previous, event_id = saved_brix(session, sample_id)
    if previous != values["brix"] or event_id is None:
        session.add(AuditEvent(
            entity_type="sample", entity_id=sample_id, action=CALCULATION_INPUTS, user_id=actor_id,
            details_json=json.dumps({"brix": str(values["brix"]) if values["brix"] is not None else None,
                                     "formula_version": FORMULA_VERSION}),
        ))
        session.flush()
    return refresh_calculated_results(session, sample_id, actor_id=actor_id)


def add_execution(
    session: Session, requested: RequestedAnalysis, *, numeric_value: Decimal | None,
    text_value: str | None, notes: str | None, actor_id: int | None,
) -> AnalysisExecution:
    execution = _append_execution(
        session, requested, numeric_value=numeric_value, text_value=text_value, notes=notes, actor_id=actor_id,
    )
    if requested.parameter.key in MEASURED_INPUTS:
        refresh_calculated_results(session, requested.sample_id, actor_id=actor_id,
                                   changed_field=requested.parameter.key)
    return execution


def format_result(parameter: AnalysisParameter, numeric: Decimal | None, text: str | None) -> str:
    if text:
        if re.sub(r"[.\s]", "", text.casefold()) == "nd":
            return "n.d."
        qualifier = re.fullmatch(r"([<>≤≥])\s*([+-]?\d+(?:[,.]\d+)?)", text)
        if qualifier and parameter.unit:
            prefix, limit = qualifier.groups()
            return f"{prefix}{limit.replace('.', ',')} {parameter.unit}"
        return text
    if numeric is None:
        return "—"
    if parameter.key == "carbohydrates" and numeric < Decimal("0.1"):
        return "<0,1 g/100 mL"
    if parameter.key == "calories":
        return f"{numeric.quantize(Decimal('1'), rounding=ROUND_HALF_UP)} kcal/100 mL"
    decimals = parameter.decimals if parameter.decimals is not None else 2
    quantum = Decimal("1").scaleb(-decimals)
    value = numeric.quantize(quantum, rounding=ROUND_HALF_UP)
    rendered = f"{value:.{decimals}f}".replace(".", ",")
    return f"{rendered} {parameter.unit}".strip() if parameter.unit else rendered


def format_report_result(parameter: AnalysisParameter, numeric: Decimal | None, text: str | None) -> str:
    """O layout dos laudos usa duas casas nos carboidratos quantificados acima do limite."""
    if parameter.key == "carbohydrates" and not text and numeric is not None and numeric >= Decimal("0.1"):
        number = numeric.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"{number:.2f}".replace(".", ",") + " g/100 mL"
    return format_result(parameter, numeric, text)

