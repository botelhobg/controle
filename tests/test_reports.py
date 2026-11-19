import json
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import Session

from labeer.db import Base
from labeer.models import AnalysisParameter, ReportReview, ReportVersion, RequestedAnalysis, Sample, User
from labeer.report_pdf import render_report_pdf
from labeer.reports import (
    ReportError, build_draft, cancel_report, changes_between, issue_report, pending_review, record_review,
    report_history, sha, unissued_review, verified_pdf, version_status,
)
from labeer.services import add_execution, seed_parameters


@pytest.fixture
def report_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as db:
            seed_parameters(db)
            admin = User(email="admin@example.test", name="Admin de teste", role="ADMIN")
            analyst = User(email="analyst@example.test", name="Analista de teste", role="ANALISTA")
            sample = Sample(fq_number=120, brewery="Cervejaria Exemplo", product="Pilsen de teste",
                            lot="T-01", received_date=date(2026, 11, 16), notes="Nota interna")
            db.add_all([admin, analyst, sample])
            db.flush()
            param = {p.key: p for p in db.scalars(select(AnalysisParameter))}
            requests = {
                key: RequestedAnalysis(sample=sample, parameter=param[key], include_in_report=include)
                for key, include in {"alcohol": True, "calories": False, "carbohydrates": True,
                                     "gluten": True, "ibu": None}.items()
            }
            db.add_all(requests.values())
            db.flush()
            add_execution(db, requests["alcohol"], numeric_value=D("6.0"), text_value=None,
                          notes=None, actor_id=analyst.id)
            add_execution(db, requests["calories"], numeric_value=D("45.988"), text_value=None,
                          notes=None, actor_id=analyst.id)
            add_execution(db, requests["carbohydrates"], numeric_value=D("0.934"), text_value=None,
                          notes=None, actor_id=analyst.id)
            add_execution(db, requests["gluten"], numeric_value=None, text_value="<10",
                          notes=None, actor_id=analyst.id)
            yield db, sample, admin, analyst, requests
    finally:
        engine.dispose()


def ready(db, sample, requests, **options):
    return build_draft(db, sample.id, decisions={str(requests["ibu"].id): False}, **options)


def test_pending_and_undecided_analyses_guard_emission(report_db):
    db, sample, admin, analyst, requests = report_db
    draft = build_draft(db, sample.id)
    assert any("Defina se Amargor" in problem for problem in draft.problems)
    with pytest.raises(ReportError, match="Defina se"):
        record_review(db, draft, analyst.id)
    draft = build_draft(db, sample.id, decisions={str(requests["ibu"].id): True})
    with pytest.raises(ReportError, match="não tem resultado vigente"):
        record_review(db, draft, admin.id)
    assert db.scalar(select(func.count(ReportReview.id))) == 0
    assert db.scalar(select(func.count(ReportVersion.id))) == 0
    assert sample.status == "Cadastrada"


def test_review_and_issue_preserve_scope_raw_result_and_pdf(report_db):
    db, sample, admin, analyst, requests = report_db
    draft = ready(db, sample, requests)
    assert not draft.problems
    assert "Nota interna" not in str(draft.content)
    assert draft.content["note"] is None
    actual = {r["key"]: (r["display"], r["numeric"]) for r in draft.content["results"]}
    assert actual == {"alcohol": ("6,0", "6.0"), "carbohydrates": ("0,93", "0.934"), "gluten": ("<10", None)}
    assert render_report_pdf(draft.content, number=draft.number).startswith(b"%PDF-")
    assert not report_history(db, sample.id)
    with pytest.raises(ReportError, match="Confirme a revisão"):
        issue_report(db, -1, analyst.id)
    review = record_review(db, draft, analyst.id)
    assert pending_review(db, draft).id == review.id
    assert unissued_review(db, sample.id).id == review.id
    issued = issue_report(db, review.id, admin.id)
    assert (issued.number, issued.replaces_id, issued.issued_by_id, review.reviewed_by_id) == (1, None, admin.id, analyst.id)
    assert verified_pdf(db, issued.id).startswith(b"%PDF-")
    assert sha(issued.pdf_bytes) == issued.pdf_digest
    assert unissued_review(db, sample.id) is None
    assert json.loads(issued.content_json)["results"] == draft.content["results"]
    assert requests["ibu"].include_in_report is None
    assert sample.status == "Laudo emitido"
    with pytest.raises(ReportError, match="já foi usada"):
        issue_report(db, review.id, admin.id)


def test_changed_result_blocks_an_obsolete_review(report_db):
    db, sample, admin, analyst, requests = report_db
    draft = ready(db, sample, requests)
    review = record_review(db, draft, analyst.id)
    add_execution(db, requests["alcohol"], numeric_value=D("5.9"), text_value=None, notes=None, actor_id=analyst.id)
    with pytest.raises(ReportError, match="mudaram após a revisão"):
        issue_report(db, review.id, admin.id)
    with pytest.raises(ReportError, match="prévia mudou"):
        record_review(db, draft, analyst.id)
    assert not report_history(db, sample.id)


def test_correction_preserves_old_bytes_and_requires_reason(report_db):
    db, sample, admin, analyst, requests = report_db
    original = issue_report(db, record_review(db, ready(db, sample, requests), admin.id).id, admin.id)
    original_pdf = verified_pdf(db, original.id)
    unchanged = ready(db, sample, requests, reason="Uma nova cópia")
    assert any("já existe" in s.casefold() for s in unchanged.problems)

    add_execution(db, requests["gluten"], numeric_value=D("18"), text_value=None, notes=None, actor_id=analyst.id)
    with pytest.raises(ReportError, match="Informe o motivo"):
        record_review(db, ready(db, sample, requests), admin.id)
    revised = ready(db, sample, requests, reason="Confirmação do glúten")
    new = issue_report(db, record_review(db, revised, analyst.id).id, analyst.id)
    assert new.number == 2 and new.replaces_id == original.id
    assert [version_status(v, 2) for v in report_history(db, sample.id)] == ["Vigente", "Substituída"]
    diff = changes_between(json.loads(original.content_json), json.loads(new.content_json))
    assert [d["Campo"] for d in diff] == ["Glúten"]
    assert "<10 ppm" in diff[0]["Antes"] and "18 ppm" in diff[0]["Depois"]
    assert verified_pdf(db, original.id) == original_pdf
    assert new.reason == "Confirmação do glúten"


def test_invalidating_is_admin_only_and_never_reactivates_an_older_version(report_db):
    db, sample, admin, analyst, requests = report_db
    one = issue_report(db, record_review(db, ready(db, sample, requests), admin.id).id, admin.id)
    draft = ready(db, sample, requests, note="Nota externa", reason="Inclusão de observação")
    two = issue_report(db, record_review(db, draft, admin.id).id, admin.id)
    two_bytes = verified_pdf(db, two.id)
    with pytest.raises(ReportError, match="Apenas o Administrador"):
        cancel_report(db, two.id, "Erro", analyst.id)
    with pytest.raises(ReportError, match="Só a versão vigente"):
        cancel_report(db, one.id, "Erro", admin.id)
    with pytest.raises(ReportError, match="Informe um motivo"):
        cancel_report(db, two.id, "  ", admin.id)
    cancel_report(db, two.id, "Invalidar edição", admin.id)
    assert [version_status(v, 2) for v in report_history(db, sample.id)] == ["Invalidada", "Substituída"]
    assert sample.status == "Laudo invalidado"
    assert verified_pdf(db, two.id) == two_bytes

    same_content = ready(db, sample, requests, note="Nota externa", reason="Reemissão conferida")
    three = issue_report(db, record_review(db, same_content, analyst.id).id, analyst.id)
    assert three.number == 3 and three.replaces_id is None
    assert [version_status(v, 3) for v in report_history(db, sample.id)] == ["Vigente", "Invalidada", "Substituída"]


def test_imported_status_does_not_create_fake_prior_versions(report_db):
    db, sample, admin, analyst, requests = report_db
    sample.status = "Laudo enviado"
    db.flush()
    draft = ready(db, sample, requests)
    assert draft.legacy_status == "Laudo enviado" and draft.number == 1
    assert draft.previous is None and draft.requires_reason
    with pytest.raises(ReportError, match="Informe o motivo"):
        record_review(db, draft, admin.id)
    assert not report_history(db, sample.id)
    approved = record_review(db, ready(db, sample, requests, reason="Registrar no aplicativo"), admin.id)
    assert issue_report(db, approved.id, analyst.id).replaces_id is None


def test_corrupted_pdf_is_reported_without_regeneration(report_db):
    db, sample, admin, analyst, requests = report_db
    report = issue_report(db, record_review(db, ready(db, sample, requests), admin.id).id, admin.id)
    db.execute(update(ReportVersion).where(ReportVersion.id == report.id).values(pdf_bytes=b"corrompido"))
    db.expire_all()
    with pytest.raises(ReportError, match="integridade"):
        verified_pdf(db, report.id)
