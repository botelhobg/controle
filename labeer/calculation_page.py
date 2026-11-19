"""Prévia de cálculos no Streamlit, sem atualizar resultados vigentes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from .calculations import FORMULAS, FORMULA_VERSION, INPUT_LABELS, calculate, decimal_text, parse_input
from .db import session_scope
from .importer import LEGACY_REFERENCE
from .models import AnalysisExecution, AnalysisParameter, AuditEvent, CurrentResult, RequestedAnalysis, Sample, fq_code
from .services import format_report_result, format_result


REFERENCE_FIELDS = {
    "Calc_Alcool": "alcohol",
    "Calc_Extrato_Aparente": "apparent_extract",
    "Calc_Carb": "brix",
}
RELEVANT_PARAMETERS = {"alcohol", "apparent_extract", *(formula.key for formula in FORMULAS)}


@dataclass(frozen=True)
class RegisteredResult:
    numeric: Decimal | None
    textual: str | None
    execution: int


@dataclass(frozen=True)
class Reading:
    field: str
    value: Decimal
    source: str


def load_preview_data(db: Session, sample_id: int) -> tuple[dict[str, RegisteredResult], list[Reading]]:
    current = db.execute(
        select(AnalysisParameter.key, AnalysisExecution.numeric_value, AnalysisExecution.text_value,
               AnalysisExecution.execution_number)
        .select_from(RequestedAnalysis)
        .join(RequestedAnalysis.parameter)
        .join(RequestedAnalysis.current_result)
        .join(CurrentResult.execution)
        .where(RequestedAnalysis.sample_id == sample_id, AnalysisParameter.key.in_(RELEVANT_PARAMETERS))
    )
    registered = {key: RegisteredResult(number, text, n) for key, number, text, n in current}
    rows = db.scalars(
        select(AuditEvent.details_json)
        .where(AuditEvent.entity_type == "sample", AuditEvent.entity_id == sample_id,
               AuditEvent.action == LEGACY_REFERENCE)
        .order_by(AuditEvent.id)
    )
    readings = []
    for raw in rows:
        if not raw:
            continue
        reference = json.loads(raw, parse_float=Decimal)
        sheet, cells = reference.get("aba"), reference.get("celulas", [])
        field = REFERENCE_FIELDS.get(sheet)
        if field is None or len(cells) < 2:
            continue
        try:
            value = parse_input(field, cells[1])
        except ValueError:
            continue
        if value is not None:
            readings.append(Reading(field, value, f"{sheet}!B{reference['linha']}"))
    return registered, readings


def suggested_values(
    registered: dict[str, RegisteredResult], readings: list[Reading],
) -> dict[str, Reading]:
    """Nunca substitui álcool ou extrato vigente por uma leitura de apoio."""
    known = {}
    for field in ("alcohol", "apparent_extract"):
        result = registered.get(field)
        if result is not None and result.numeric is not None:
            known[field] = Reading(field, result.numeric, f"Resultado vigente · execução {result.execution}")
    brix = [reading for reading in readings if reading.field == "brix"]
    if brix and len({reading.value for reading in brix}) == 1:
        known["brix"] = Reading("brix", brix[0].value, ", ".join(reading.source for reading in brix))
    return known


def render_calculation_page() -> None:
    st.header("Cálculos")
    st.caption(f"Prévia das fórmulas da planilha LaBeer v{FORMULA_VERSION}. Nada é gravado no banco.")
    with session_scope() as db:
        samples = dict(db.execute(select(Sample.id, Sample.fq_number).order_by(Sample.fq_number)).all())
    sample_id = st.selectbox(
        "Amostra para conferir",
        [None, *samples],
        format_func=lambda option: "Simulação sem amostra" if option is None else fq_code(samples[option]),
    )
    with session_scope() as db:
        parameters = {p.key: p for p in db.scalars(
            select(AnalysisParameter).where(AnalysisParameter.key.in_(RELEVANT_PARAMETERS))
        )}
        registered, readings = load_preview_data(db, sample_id) if sample_id is not None else ({}, [])

    suggestions = suggested_values(registered, readings)
    if len({r.value for r in readings if r.field == "brix"}) > 1:
        st.warning("Há leituras diferentes de °Brix nesta amostra. Confira as origens e digite qual usar.")
    if "brix" in suggestions:
        st.caption("Confirme que a leitura de °Brix corresponde ao mesmo ensaio do extrato aparente.")

    columns = st.columns(3)
    inputs = {}
    for column, (field, label) in zip(columns, INPUT_LABELS.items()):
        suggestion = suggestions.get(field)
        default = decimal_text(suggestion.value if suggestion else None)
        typed = column.text_input(label, value=default, key=f"calc:{sample_id}:{field}:{default}")
        try:
            inputs[field] = parse_input(field, typed)
        except ValueError as error:
            inputs[field] = None
            column.error(str(error))
        if inputs[field] is not None:
            source = suggestion.source if suggestion and inputs[field] == suggestion.value else "Digitado na prévia"
            column.caption(source)

    if readings:
        with st.expander("Leituras de apoio e origens"):
            st.dataframe(
                [{"Entrada": INPUT_LABELS[r.field], "Valor": decimal_text(r.value), "Origem": r.source} for r in readings],
                width="stretch", hide_index=True,
            )

    calculations = calculate(inputs)
    summary = []
    for rule in FORMULAS:
        parameter = parameters[rule.key]
        prospective = calculations[rule.key]
        active = registered.get(rule.key)
        registered_display = "—"
        if active:
            registered_display = decimal_text(active.numeric) if active.numeric is not None else format_result(
                parameter, None, active.textual,
            )
        summary.append({
            "Parâmetro": f"{parameter.name} ({parameter.unit})",
            "Valor vigente": registered_display,
            "Prévia sem arredondar": decimal_text(prospective.value) or "—",
            "Como seria exibido": format_report_result(parameter, prospective.value, None),
            "Entradas que faltam": ", ".join(INPUT_LABELS[k] for k in prospective.missing),
        })
    st.dataframe(summary, width="stretch", hide_index=True)
    carbs = calculations["carbohydrates"].value
    if carbs is not None and carbs < 0:
        st.warning("O modelo estimou carboidratos negativos. Revise as leituras; <0,1 é só a regra de exibição.")
