from __future__ import annotations

import json
import re
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .calculations import FORMULA_VERSION
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


def add_execution(
    session: Session,
    requested: RequestedAnalysis,
    *,
    numeric_value: Decimal | None,
    text_value: str | None,
    notes: str | None,
    actor_id: int | None,
) -> AnalysisExecution:
    execution = AnalysisExecution(
        requested_analysis=requested,
        execution_number=len(requested.executions) + 1,
        numeric_value=numeric_value,
        text_value=text_value.strip() if text_value else None,
        notes=notes.strip() if notes else None,
        performed_by_id=actor_id,
        performed_at=utc_now(),
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

