import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import URL, create_engine, select
from sqlalchemy.orm import Session, selectinload

from labeer.importer import ANALYSIS_HEADER, LEGACY_REFERENCE, SAMPLE_HEADER, Snapshot, plan_import, read_result, write_new_database
from labeer.models import AnalysisParameter, AuditEvent, RequestedAnalysis, Sample
from labeer.services import next_fq_number


@pytest.fixture
def snapshot():
    return Snapshot("Dados fictícios", {
        "Amostras": [
            ["B001", "B001", "Legado"] + [""] * 6 + ["Data do laudo"],
            list(SAMPLE_HEADER),
            ["FQ068", "FQ68", "Exemplo", "Pilsen", "", "0001", "01/09/2026", "", "Enviado", "25/09/2026"],
            ["FQ096", "FQ96", "Exemplo", "", "", "", "", "", ""],
            ["FQ98", "", "", "", "", "", "", "", "", "", "Sem análises cadastradas"],
        ],
        "Analises": [
            ["B001", "Álcool", "", "", "", "", 7, "Concluída", "Sim"],
            list(ANALYSIS_HEADER),
            ["FQ68", "Álcool", "3,7", "", "", "25/09/2026", 3.7, "Concluída", "Sim"],
            ["FQ068", "Álcool", "", "", "", "25/09/2026", 3.7, "Concluída", "Sim"],
            ["FQ68", "Extrato Real", "", "", "", "", "2.07009221111111111", "Concluída", "Sim"],
            ["FQ68", "Calorias", "", "", "", "", "", "Encerrada sem resultado", "Não"],
            ["FQ96", "Gluten", "", "ppm", "", "", "n.d.", "Concluída", "Sim"],
            ["FQ096", "Shelf Life", "", "dias", "", "", ">365", "Concluída", "Sim"],
            ["FQ96", "Amargor (IBU)", "", "IBU", "", "", "", "Pendente", ""],
            ["FQ96", "Densidade", "", "", "", "", 0, "Concluída", "Não"],
        ],
        "Calc_Alcool": [
            ["Código FQ", "Álcool (% v/v)", "Código FQ"],
            ["B001", 8, "B001"],
            ["FQ68", 3.6, "FQ68"],
            ["FQ068", 3.7, "FQ68"],
        ],
    })


def test_preview_aliases_placeholders_and_historical_values(snapshot):
    plan = plan_import(snapshot)
    assert not plan.errors
    assert set(plan.samples) == {68, 96}
    assert plan.ignored_placeholders == [98]
    assert plan.legacy_rows == 3
    assert plan.samples[68].received == date(2026, 9, 1)
    assert plan.samples[68].report_date == date(2026, 9, 25)
    assert plan.samples[96].received is None
    assert plan.samples[68].status == "Laudo enviado"
    alcohol = plan.samples[68].analyses[0]
    assert alcohol.current.numeric == Decimal("3.7")
    assert len(alcohol.executions) == 1
    assert [row.source.number for row in alcohol.executions[0]] == [3, 4]
    assert [r.result.numeric for r in plan.references] == [Decimal("3.6"), Decimal("3.7")]
    assert plan.samples[96].analyses[0].current.textual == "n.d."
    assert plan.samples[96].analyses[1].current.textual == ">365"
    assert plan.samples[96].analyses[2].status == "Pendente"
    assert plan.samples[96].analyses[3].current.numeric == Decimal("0")


def test_write_preserves_exact_values_dates_and_source(snapshot, tmp_path):
    plan = plan_import(snapshot)
    path = tmp_path / "migracao.db"
    write_new_database(plan, path)
    engine = create_engine(URL.create("sqlite", database=str(path)))
    try:
        with Session(engine) as db:
            sample = db.scalar(select(Sample).where(Sample.fq_number == 68))
            assert sample.lot == "0001"
            assert sample.due_date == date(2026, 9, 21)
            other = db.scalar(select(Sample).where(Sample.fq_number == 96))
            assert other.due_date is None
            assert next_fq_number(db) == 97
            extract = db.scalar(
                select(RequestedAnalysis).join(AnalysisParameter)
                .where(AnalysisParameter.key == "real_extract")
            )
            assert extract.price_snapshot is None
            assert extract.current_result.execution.numeric_value == Decimal("2.07009221111111111")
            assert extract.current_result.execution.performed_at is None
            refs = db.scalars(select(AuditEvent).where(AuditEvent.action == LEGACY_REFERENCE)).all()
            assert [(d["linha"], d["numerico"]) for d in (json.loads(r.details_json) for r in refs)] == [
                (3, "3.6"), (4, "3.7"),
            ]
            all_pending = db.scalars(
                select(RequestedAnalysis).where(RequestedAnalysis.status == "Pendente")
                .options(selectinload(RequestedAnalysis.current_result))
            ).all()
            assert len(all_pending) == 1
            assert all_pending[0].current_result is None and all_pending[0].include_in_report is None
    finally:
        engine.dispose()

    prior = path.read_bytes()
    with pytest.raises(FileExistsError):
        write_new_database(plan, path)
    assert path.read_bytes() == prior


def test_two_current_values_block_without_writing(snapshot, tmp_path):
    snapshot.sheets["Analises"][3][6] = 3.8
    plan = plan_import(snapshot)
    assert any("mais de um resultado vigente" in error for error in plan.errors)
    assert len(plan.samples[68].analyses[0].executions) == 2
    output = tmp_path / "nao_escrever.db"
    with pytest.raises(ValueError, match="bloqueios"):
        write_new_database(plan, output)
    assert not output.exists()


def test_explicit_report_choice_preserves_the_other_result(snapshot):
    snapshot.sheets["Analises"][3][6] = 3.8
    snapshot.sheets["Analises"][3][8] = "Não"
    plan = plan_import(snapshot)
    assert not plan.errors
    result = plan.samples[68].analyses[0]
    assert result.current.numeric == Decimal("3.7")
    assert [items[0].result.numeric for items in result.executions] == [Decimal("3.7"), Decimal("3.8")]


def test_broken_result_missing_cache_and_mismatched_id_are_reported(snapshot):
    snapshot.sheets["Analises"][2][6] = "#REF!"
    snapshot.sheets["Amostras"][2][1] = "FQ69"
    snapshot = Snapshot(snapshot.name, snapshot.sheets, uncached_status=frozenset({3}))
    plan = plan_import(snapshot)
    assert any("resultado inválido" in s for s in plan.errors)
    assert any("fórmula sem valor salvo" in s for s in plan.errors)
    assert any("IDs diferentes" in s for s in plan.errors)


def test_second_row_with_only_report_status_is_not_a_second_sample(snapshot):
    snapshot.sheets["Amostras"].append(["FQ68", "FQ68", "", "", "", "", "", "", "Enviado", "20/09/2026"])
    plan = plan_import(snapshot)
    assert not plan.errors
    sample = plan.samples[68]
    assert sample.report_date == date(2026, 9, 25)
    assert sample.extra_rows[0].cells[9] == "20/09/2026"


def test_duplicate_fq_with_conflicting_identification_blocks(snapshot):
    snapshot.sheets["Amostras"].append(["FQ68", "FQ68", "Outra", "IPA", "", "", "", "", "Enviado"])
    plan = plan_import(snapshot)
    assert any("cadastros conflitantes" in s for s in plan.errors)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "1e10000", "#VALUE!", "—"])
def test_invalid_result_does_not_become_a_measurement(value):
    with pytest.raises(ValueError):
        read_result(value)
