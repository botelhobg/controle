import json
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from labeer.calculations import FORMULA_VERSION, calculate
from labeer.db import Base
from labeer.models import AnalysisExecution, AnalysisParameter, AuditEvent, RequestedAnalysis, Sample, User
from labeer.reports import ReportError, build_draft, issue_report, record_review, verified_pdf
from labeer.services import (
    CALCULATION_INPUTS, add_execution, calculation_snapshot, format_result, save_calculations, saved_brix,
    seed_parameters,
)


DERIVED = ("real_extract", "original_extract", "calories", "carbohydrates")
INPUTS = {"alcohol": D("5.0"), "apparent_extract": D("0.5"), "brix": D("6.5")}


@pytest.fixture
def records():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            seed_parameters(db)
            user = User(email="admin@example.test", name="Admin fictício", role="ADMIN")
            sample = Sample(fq_number=1, brewery="Fictícia", product="Teste")
            db.add_all([sample, user])
            db.flush()
            parameters = {p.key: p for p in db.scalars(select(AnalysisParameter))}
            requests = {key: RequestedAnalysis(sample=sample, parameter=parameters[key], include_in_report=True)
                        for key in ("alcohol", "apparent_extract", *DERIVED, "gluten", "shelf_life")}
            db.add_all(requests.values())
            db.flush()
            for key, value in (("alcohol", D("5.0")), ("apparent_extract", D("0.5")),
                               ("gluten", D("18")), ("shelf_life", D("90"))):
                add_execution(db, requests[key], numeric_value=value, text_value=None, notes=None, actor_id=user.id)
            yield db, sample, requests, user
    finally:
        engine.dispose()


def save(records, inputs=None, snapshot=None):
    db, sample, _, user = records
    return save_calculations(db, sample.id, INPUTS if inputs is None else inputs, actor_id=user.id,
                             expected_snapshot=calculation_snapshot(db, sample.id) if snapshot is None else snapshot)


def current(request):
    return request.current_result.execution if request.current_result else None


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def test_measured_results_automatically_save_available_calculations(records):
    db, sample, req, user = records
    assert {key: current(req[key]).numeric_value for key in DERIVED[:3]} == {
        "real_extract": D("2.3144505"), "original_extract": D("9.9991139"), "calories": D("37.15"),
    }
    assert req["carbohydrates"].status == "Pendente" and current(req["carbohydrates"]) is None
    assert saved_brix(db, sample.id) == (None, None)
    update = save(records)
    assert update.saved == ("carbohydrates",) and not update.pending
    assert current(req["carbohydrates"]).numeric_value == D("0.0865")
    assert format_result(req["carbohydrates"].parameter, D("0.0865"), None) == "<0,1 g/100 mL"
    assert format_result(req["calories"].parameter, D("37.15"), None) == "37 kcal/100 mL"
    for key in DERIVED:
        execution = current(req[key])
        provenance = json.loads(execution.raw_inputs_json)
        assert provenance["formula_key"] == key and provenance["formula_version"] == FORMULA_VERSION
        assert execution.performed_by_id == user.id
        assert provenance["source_executions"]["apparent_extract"] == current(req["apparent_extract"]).id
    assert json.loads(current(req["carbohydrates"]).raw_inputs_json)["brix_reading_id"] == saved_brix(db, sample.id)[1]


def test_saving_unchanged_calculations_does_not_duplicate_history(records):
    db, _, _, _ = records
    save(records)
    before = (count(db, AnalysisExecution), count(db, AuditEvent))
    assert not save(records).saved
    assert (count(db, AnalysisExecution), count(db, AuditEvent)) == before


def test_alcohol_repetition_updates_only_dependent_results(records):
    db, sample, req, user = records
    save(records)
    before = {key: current(request).id for key, request in req.items()}
    add_execution(db, req["alcohol"], numeric_value=D("3.5"), text_value=None, notes="Repetição", actor_id=user.id)
    for key in ("apparent_extract", "carbohydrates", "gluten", "shelf_life"):
        assert current(req[key]).id == before[key]
    expected = calculate({**INPUTS, "alcohol": D("3.5")})
    for key in DERIVED[:3]:
        assert current(req[key]).numeric_value == expected[key].value
        assert current(req[key]).execution_number == 2
        assert any(e.id == before[key] for e in req[key].executions)
    assert saved_brix(db, sample.id)[0] == D("6.5")


def test_repeated_same_measurement_records_new_calculation_provenance(records):
    db, _, req, user = records
    before = current(req["calories"])
    source = add_execution(db, req["alcohol"], numeric_value=D("5"), text_value=None, notes=None, actor_id=user.id)
    after = current(req["calories"])
    assert after.id != before.id and after.numeric_value == before.numeric_value
    assert json.loads(after.raw_inputs_json)["source_executions"]["alcohol"] == source.id


def test_apparent_extract_repetition_updates_all_four_with_confirmed_brix(records):
    db, _, req, user = records
    save(records)
    before = {key: current(request).id for key, request in req.items()}
    add_execution(db, req["apparent_extract"], numeric_value=D("0.7"), text_value=None, notes=None, actor_id=user.id)
    expected = calculate({**INPUTS, "apparent_extract": D("0.7")})
    for key in DERIVED:
        assert current(req[key]).id != before[key] and current(req[key]).numeric_value == expected[key].value
    for key in ("alcohol", "gluten", "shelf_life"):
        assert current(req[key]).id == before[key]


def test_unrelated_analysis_does_not_recalculate(records):
    db, _, req, user = records
    save(records)
    before = {key: current(req[key]).id for key in DERIVED}
    add_execution(db, req["gluten"], numeric_value=D("20"), text_value=None, notes=None, actor_id=user.id)
    assert {key: current(req[key]).id for key in DERIVED} == before


def test_brix_change_updates_only_carbohydrates_and_zero_is_valid(records):
    _, _, req, _ = records
    save(records)
    before = {key: current(req[key]).id for key in DERIVED}
    update = save(records, {**INPUTS, "brix": D("0")})
    assert update.saved == ("carbohydrates",)
    assert current(req["carbohydrates"]).numeric_value == D("1.3995")
    assert {key: current(req[key]).id for key in DERIVED[:3]} == {key: before[key] for key in DERIVED[:3]}


def test_missing_brix_invalidates_only_selection_and_preserves_old_execution(records):
    _, _, req, _ = records
    save(records)
    old = current(req["carbohydrates"])
    update = save(records, {**INPUTS, "brix": None})
    assert update.pending == ("carbohydrates",)
    assert req["carbohydrates"].status == "Pendente" and current(req["carbohydrates"]) is None
    assert req["carbohydrates"].executions == [old] and old.numeric_value == D("0.0865")
    save(records)
    assert current(req["carbohydrates"]).execution_number == 2


@pytest.mark.parametrize("field,invalidated", [("alcohol", DERIVED[:3]), ("apparent_extract", DERIVED)])
def test_censored_input_cannot_leave_stale_completed_calculations(records, field, invalidated):
    db, _, req, user = records
    save(records)
    old = {key: current(req[key]).id for key in DERIVED}
    add_execution(db, req[field], numeric_value=None, text_value="<10", notes=None, actor_id=user.id)
    for key in invalidated:
        assert current(req[key]) is None and req[key].status == "Pendente"
        assert any(e.id == old[key] for e in req[key].executions)
    if field == "alcohol":
        assert current(req["carbohydrates"]).id == old["carbohydrates"]
    add_execution(db, req[field], numeric_value=INPUTS[field], text_value=None, notes=None, actor_id=user.id)
    assert all(current(req[key]) is not None for key in DERIVED)


def test_simulation_and_stale_screen_cannot_overwrite_results(records):
    db, sample, req, user = records
    before = (count(db, AnalysisExecution), count(db, AuditEvent))
    with pytest.raises(ValueError, match="vigentes"):
        save(records, {**INPUTS, "alcohol": D("7")})
    assert (count(db, AnalysisExecution), count(db, AuditEvent)) == before
    snapshot = calculation_snapshot(db, sample.id)
    add_execution(db, req["alcohol"], numeric_value=D("5"), text_value=None, notes=None, actor_id=user.id)
    before = (count(db, AnalysisExecution), count(db, AuditEvent))
    with pytest.raises(ValueError, match="mudaram"):
        save(records, snapshot=snapshot)
    assert (count(db, AnalysisExecution), count(db, AuditEvent)) == before


@pytest.mark.parametrize("value", ["NaN", "-1", "101", "<10"])
def test_invalid_brix_does_not_write_anything(records, value):
    db, _, _, _ = records
    before = (count(db, AnalysisExecution), count(db, AuditEvent))
    with pytest.raises(ValueError):
        save(records, {**INPUTS, "brix": value})
    assert (count(db, AnalysisExecution), count(db, AuditEvent)) == before


def test_no_new_requests_or_charges_and_closed_analyses_remain_unchanged(records):
    db, sample, req, user = records
    before = count(db, RequestedAnalysis)
    old = current(req["real_extract"])
    req["real_extract"].status = "Encerrada"
    add_execution(db, req["alcohol"], numeric_value=D("4"), text_value=None, notes=None, actor_id=user.id)
    assert count(db, RequestedAnalysis) == before
    assert req["real_extract"].status == "Encerrada" and current(req["real_extract"]).id == old.id
    with pytest.raises(ValueError, match="não encontrada"):
        save_calculations(db, sample.id + 100, INPUTS, actor_id=user.id, expected_snapshot={})


def test_only_requested_calculations_are_saved_and_other_samples_are_untouched(records):
    db, sample, req, user = records
    before = calculation_snapshot(db, sample.id)
    other = Sample(fq_number=2, brewery="Fictícia", product="Outra amostra")
    db.add(other)
    db.flush()
    other_requests = {
        key: RequestedAnalysis(sample=other, parameter=req[key].parameter, price_snapshot=D("12.34"))
        for key in ("alcohol", "apparent_extract", "calories")
    }
    db.add_all(other_requests.values())
    db.flush()
    for key in ("alcohol", "apparent_extract"):
        add_execution(db, other_requests[key], numeric_value=INPUTS[key], text_value=None, notes=None,
                      actor_id=user.id)
    save_calculations(db, other.id, INPUTS, actor_id=user.id, expected_snapshot=calculation_snapshot(db, other.id))
    assert len(db.scalars(select(RequestedAnalysis).where(RequestedAnalysis.sample_id == other.id)).all()) == 3
    assert all(r.price_snapshot == D("12.34") for r in other_requests.values())
    assert current(other_requests["calories"]).numeric_value == D("37.15")
    assert calculation_snapshot(db, sample.id) == before


def test_confirmed_legacy_reading_is_not_silently_replaced(records):
    db, sample, _, _ = records
    save(records)
    db.add(AuditEvent(entity_type="sample", entity_id=sample.id, action="legacy_reference",
                      details_json=json.dumps({"aba": "Calc_Carb", "linha": 9, "celulas": ["FQ01", "9"]})))
    db.flush()
    assert saved_brix(db, sample.id)[0] == D("6.5")
    assert db.scalar(select(func.count(AuditEvent.id)).where(AuditEvent.action == CALCULATION_INPUTS)) == 1


def test_transaction_rolls_back_measurement_and_automatic_calculations_together(records):
    db, sample, req, user = records
    save(records)
    db.commit()
    before = calculation_snapshot(db, sample.id)
    add_execution(db, req["alcohol"], numeric_value=D("4"), text_value=None, notes=None, actor_id=user.id)
    db.rollback()
    assert calculation_snapshot(db, sample.id) == before


def test_old_pdf_is_immutable_and_changed_calculations_require_new_review(records):
    db, sample, req, user = records
    save(records)
    issued = issue_report(db, record_review(db, build_draft(db, sample.id), user.id).id, user.id)
    pdf = verified_pdf(db, issued.id)
    draft = build_draft(db, sample.id, note="Observação fictícia", reason="Teste")
    review = record_review(db, draft, user.id)
    save(records, {**INPUTS, "brix": D("0")})
    with pytest.raises(ReportError, match="mudaram"):
        issue_report(db, review.id, user.id)
    assert verified_pdf(db, issued.id) == pdf
    assert current(req["carbohydrates"]).numeric_value == D("1.3995")
