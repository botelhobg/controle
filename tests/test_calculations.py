import json
from decimal import Decimal as D, localcontext

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from labeer.calculation_page import load_preview_data, suggested_values
from labeer.calculations import FORMULA_VERSION, calculate, decimal_text, parse_input
from labeer.db import Base
from labeer.importer import LEGACY_REFERENCE
from labeer.models import AnalysisExecution, AnalysisParameter, AuditEvent, RequestedAnalysis, Sample
from labeer.services import add_execution, format_result, seed_parameters


def test_formulas_match_independently_checked_values():
    with localcontext() as caller:
        caller.prec = 6
        results = calculate({"alcohol": D("5"), "apparent_extract": D("2"), "brix": D("4")})
    assert {key: result.value for key, result in results.items()} == {
        "real_extract": D("3.785307"), "original_extract": D("11.3919779"),
        "calories": D("42.970"), "carbohydrates": D("1.700"),
    }
    assert {result.formula.version for result in results.values()} == {FORMULA_VERSION}


def test_partial_inputs_distinguish_zero_from_missing():
    results = calculate({"alcohol": D("0"), "apparent_extract": D("0")})
    assert results["real_extract"].value == D("0.13004")
    assert results["calories"].value == D("0.72")
    assert results["carbohydrates"].value is None
    assert results["carbohydrates"].missing == ("brix",)
    carbs_only = calculate({"brix": D("0"), "apparent_extract": D("0")})
    assert carbs_only["carbohydrates"].value == D("1.03")
    assert carbs_only["original_extract"].missing == ("alcohol",)


def test_reanalysis_changes_only_the_dependent_predictions():
    original = {"alcohol": D("4.2"), "apparent_extract": D("2"), "brix": D("5")}
    revised = {**original, "alcohol": D("3.5")}
    before, after = calculate(original), calculate(revised)
    for name in ("real_extract", "original_extract", "calories"):
        assert before[name].value != after[name].value
    assert before["carbohydrates"].value == after["carbohydrates"].value


def test_raw_carbohydrate_values_and_display_are_separate():
    result = calculate({"apparent_extract": D("-0.6"), "brix": D("3.3")})["carbohydrates"]
    assert result.value == D("-0.08")
    assert decimal_text(result.value) == "-0,08"
    parameter = AnalysisParameter(key="carbohydrates", unit="g/100 mL", decimals=1)
    assert format_result(parameter, result.value, None) == "<0,1 g/100 mL"
    assert format_result(parameter, D("0.099"), None) == "<0,1 g/100 mL"
    assert format_result(parameter, D("0.1"), None) == "0,1 g/100 mL"
    calories = AnalysisParameter(key="calories", unit="kcal/100 mL", decimals=0)
    assert format_result(calories, D("32.5"), None) == "33 kcal/100 mL"


@pytest.mark.parametrize("bad", ["n.d.", "NaN", "<0,1", "3,5%", "1e90000", "-1", "101"])
def test_invalid_alcohol_is_not_a_numerical_input(bad):
    with pytest.raises(ValueError):
        parse_input("alcohol", bad)


def test_decimal_comma_and_unmeasured_inputs():
    assert parse_input("alcohol", " 3,50 ") == D("3.50")
    assert parse_input("brix", "  ") is None
    assert parse_input("apparent_extract", "-0,6") == D("-0.6")


def test_preview_respects_current_result_and_ambiguous_brix():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            seed_parameters(db)
            parameter = db.scalar(select(AnalysisParameter).where(AnalysisParameter.key == "alcohol"))
            sample = Sample(fq_number=18, brewery="Exemplo", product="Pilsen")
            req = RequestedAnalysis(sample=sample, parameter=parameter)
            db.add(req)
            db.flush()
            add_execution(db, req, numeric_value=D("3.1"), text_value=None, notes=None, actor_id=None)
            latest = add_execution(db, req, numeric_value=D("3.5"), text_value=None, notes=None, actor_id=None)

            def archive(sheet, line, value):
                db.add(AuditEvent(
                    entity_type="sample", entity_id=sample.id, action=LEGACY_REFERENCE,
                    details_json=json.dumps({"aba": sheet, "linha": line, "celulas": ["FQ18", value]}),
                ))
                db.flush()

            archive("Calc_Alcool", 6, "3.1")
            archive("Calc_Extrato_Aparente", 9, "1.9")
            archive("Calc_Carb", 12, "4.2")
            archive("Calc_Carb", 13, "4.20")
            current, reading = load_preview_data(db, sample.id)
            defaults = suggested_values(current, reading)
            assert defaults["alcohol"].value == D("3.5")
            assert "apparent_extract" not in defaults
            assert defaults["brix"].source == "Calc_Carb!B12, Calc_Carb!B13"
            assert [r.value for r in reading if r.field == "alcohol"] == [D("3.1")]

            archive("Calc_Carb", 14, "4.9")
            current, reading = load_preview_data(db, sample.id)
            assert "brix" not in suggested_values(current, reading)
            assert not db.dirty and req.current_result.execution_id == latest.id
            assert db.scalar(select(func.count(AnalysisExecution.id))) == 2
    finally:
        engine.dispose()
