from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from labeer.db import Base
from labeer.models import AnalysisParameter, RequestedAnalysis, Sample
from labeer.services import add_execution, create_sample, deadline_for, format_result, next_fq_number


def test_deadline_is_twenty_calendar_days():
    assert deadline_for(date(2026, 11, 1)) == date(2026, 11, 21)


def test_next_code_uses_only_fq_numeric_sequence():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert next_fq_number(session) == 1
        session.add(
            Sample(
                fq_number=70,
                brewery="Teste",
                product="Teste",
                received_date=date(2026, 1, 1),
                due_date=date(2026, 1, 21),
            )
        )
        session.flush()
        assert next_fq_number(session) == 71


def test_report_display_rules():
    carbs = AnalysisParameter(key="carbohydrates", name="Carboidratos", unit="g/100 mL", kind="calculated", decimals=1)
    calories = AnalysisParameter(key="calories", name="Calorias", unit="kcal/100 mL", kind="calculated", decimals=0)
    alcohol = AnalysisParameter(key="alcohol", name="Álcool", unit="% v/v", kind="measured", decimals=1)

    assert format_result(carbs, Decimal("0.07"), None) == "<0,1 g/100 mL"
    assert format_result(calories, Decimal("42.6"), None) == "43 kcal/100 mL"
    assert format_result(alcohol, Decimal("3.45"), None) == "3,5 % v/v"


def test_reanalysis_preserves_history_and_changes_current_result():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        parameter = AnalysisParameter(
            key="alcohol", name="Álcool", unit="% v/v", kind="measured", decimals=1, price=Decimal("40")
        )
        session.add(parameter)
        session.flush()
        sample = create_sample(
            session,
            brewery="Teste",
            product="Pilsen",
            style=None,
            lot=None,
            received_date=date(2026, 11, 1),
            notes=None,
            responsible_id=None,
            parameter_ids=[parameter.id],
            actor_id=None,
        )
        session.flush()
        requested = session.query(RequestedAnalysis).filter_by(sample_id=sample.id).one()

        first = add_execution(
            session, requested, numeric_value=Decimal("4.4"), text_value=None, notes=None, actor_id=None
        )
        second = add_execution(
            session, requested, numeric_value=Decimal("3.5"), text_value=None, notes="segunda análise", actor_id=None
        )

        assert [item.numeric_value for item in requested.executions] == [Decimal("4.4"), Decimal("3.5")]
        assert requested.current_result.execution_id == second.id
        assert first.id != second.id

