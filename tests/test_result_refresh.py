from pathlib import Path

import pytest
import streamlit as st
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from streamlit.testing.v1 import AppTest

from labeer import config, db as database
from labeer.accounts import bootstrap_admin, login
from labeer.calculation_page import calculation_save_key
from labeer.models import AnalysisParameter, RequestedAnalysis, Sample
from labeer.services import seed_parameters


@pytest.fixture
def app(tmp_path, monkeypatch):
    # Banco descartável e credencial fictícia: nunca usa a conexão do piloto.
    uri = f"sqlite:///{tmp_path / 'ui_test.sqlite'}"
    engine = create_engine(uri, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(database, "get_engine", lambda: engine)
    monkeypatch.setattr(database, "get_session_factory", lambda: factory)
    monkeypatch.setattr(config, "settings", config.Settings(
        environment="development", database_url=uri, admin_email="admin@example.test",
        admin_password="senha-somente-de-teste",
    ))
    get_option = st.get_option
    monkeypatch.setattr(st, "get_option", lambda key: "127.0.0.1" if key == "server.address" else get_option(key))
    st.cache_resource.clear()
    database.init_database()
    with factory.begin() as db:
        bootstrap_admin(db, "admin@example.test", "senha-somente-de-teste")
        grant = login(db, "admin@example.test", "senha-somente-de-teste")
        seed_parameters(db)
        sample = Sample(fq_number=1, brewery="Fictícia", product="Teste")
        db.add(sample)
        for parameter in db.scalars(select(AnalysisParameter)):
            db.add(RequestedAnalysis(sample=sample, parameter=parameter, include_in_report=True))
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=20)
    at.session_state["auth:grant"] = grant
    at.run()
    assert not at.exception
    yield at
    st.cache_resource.clear()
    engine.dispose()


def input_by_label(at, label):
    return next(element for element in at.text_input if element.label == label)


def button(at, label):
    return next(element for element in at.button if element.label == label)


def record(at, analysis, value):
    at.selectbox[0].set_value(f"FQ01 · {analysis}").run()
    input_by_label(at, "Valor numérico").set_value(value)
    button(at, "Registrar nova execução").click().run()
    assert not at.exception


def test_result_summary_and_history_refresh_in_the_same_submission(app):
    app.radio[0].set_value("Resultados").run()
    record(app, "Álcool", "4,8")
    assert any("Resultado atual: 4,8 % v/v · execução 1" in item.value for item in app.info)
    record(app, "Álcool", "5,0")
    assert any("Resultado atual: 5,0 % v/v · execução 2" in item.value for item in app.info)
    assert any("Execução 2 registrada" in item.value for item in app.success)
    assert list(app.dataframe[0].value["Vigente"]) == [False, True]


def test_calculation_page_saves_and_refreshes_without_manual_result_entry(app):
    app.radio[0].set_value("Resultados").run()
    record(app, "Álcool", "5,0")
    record(app, "Extrato aparente", "0,5")
    app.radio[0].set_value("Cálculos").run()
    app.selectbox[0].set_value(1).run()
    input_by_label(app, "Leitura (°Brix)").set_value("6,5").run()
    button(app, "Salvar cálculos da amostra").click().run()
    assert not app.exception
    assert any("Cálculos salvos" in item.value for item in app.success)
    table = app.dataframe[0].value
    assert list(table["Valor vigente"]) == ["2,3144505", "9,9991139", "37,15", "0,0865"]
    assert list(table["Como seria exibido"])[2:] == ["37 kcal/100 mL", "<0,1 g/100 mL"]
    app.radio[0].set_value("Resultados").run()
    record(app, "Álcool", "4,0")
    app.radio[0].set_value("Cálculos").run()
    app.selectbox[0].set_value(1).run()
    table = app.dataframe[0].value
    assert list(table["Valor vigente"])[2:] == ["30,252", "0,0865"]
    assert input_by_label(app, "Leitura (°Brix)").value == "6,5"


def test_save_button_identity_changes_with_reviewed_inputs_and_source_executions():
    inputs = {"alcohol": "5", "apparent_extract": "0.5", "brix": "6.5"}
    snapshot = {"alcohol": 1, "apparent_extract": 2, "__brix__": None}
    key = calculation_save_key(1, snapshot, inputs)
    assert calculation_save_key(1, dict(snapshot), dict(inputs)) == key
    assert calculation_save_key(1, {**snapshot, "alcohol": 3}, inputs) != key
    assert calculation_save_key(1, snapshot, {**inputs, "brix": "7"}) != key
    assert calculation_save_key(2, snapshot, inputs) != key
