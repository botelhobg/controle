"""Tela do Streamlit para revisar, emitir e consultar versões dos laudos."""

from __future__ import annotations

import json
from datetime import datetime

import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from .accounts import AccountError, Grant, authorize_work
from .db import session_scope
from .delivery_page import render_delivery_section
from .models import Sample, fq_code
from .report_pdf import local_timestamp, render_report_pdf, report_filename
from .reports import (
    NOTE_LIMIT, REASON_LIMIT, ReportDraft, ReportError, build_draft, cancel_report, changes_between,
    issue_report, pending_review, record_review, report_actor, report_history, unissued_review,
    verified_pdf, version_status,
)


def _br_time(value: datetime) -> str:
    return local_timestamp(value).strftime("%d/%m/%Y às %H:%M")


def _notice(message: str) -> None:
    st.session_state["report_notice"] = message
    st.rerun()


def _write_error(error: Exception) -> None:
    if isinstance(error, (ReportError, AccountError)):
        st.error(str(error))
    else:
        st.error("Não foi possível gravar. Reabra a amostra e confira o histórico antes de tentar novamente.")


def _render_history(sample_id: int, grant: Grant, *, is_admin: bool) -> None:
    st.subheader("Histórico de versões")
    with session_scope() as db:
        history = report_history(db, sample_id)
    if not history:
        st.caption("Ainda não há PDF emitido pelo aplicativo para esta FQ.")
        return
    previous_by_id = {v.id: v for v in history}
    statuses = {v.number: version_status(v, history[0].number) for v in history}
    st.dataframe([{
        "Versão": f"v{v.number:02d}", "Situação": statuses[v.number], "Emissão": _br_time(v.issued_at),
        "Emitido por": v.issued_by_name, "Revisado por": v.review.reviewed_by_name,
        "Substitui": f"v{previous_by_id[v.replaces_id].number:02d}" if v.replaces_id in previous_by_id else "---",
    } for v in history], hide_index=True, width="stretch")

    by_number = {v.number: v for v in history}
    number = st.selectbox("Versão para consultar", list(by_number), format_func=lambda n: f"v{n:02d} · {statuses[n]}",
                          key=f"report:history:{sample_id}:{history[0].id}")
    record = by_number[number]
    payload = json.loads(record.content_json)
    if record.reason:
        st.write("Motivo registrado:", record.reason)
    if record.cancellation:
        cancellation = record.cancellation
        st.warning(f"Versão invalidada em {_br_time(cancellation.canceled_at)} por {cancellation.actor_name}. "
                   f"Motivo: {cancellation.reason}")
    elif statuses[number] == "Substituída":
        st.info("Esta cópia está no histórico. Não é a versão vigente.")
    if record.replaces_id in previous_by_id:
        original = previous_by_id[record.replaces_id]
        delta = changes_between(json.loads(original.content_json), payload)
        if delta:
            with st.expander("O que mudou nesta versão"):
                st.dataframe(delta, hide_index=True, width="stretch")
    try:
        with session_scope() as db:
            pdf = verified_pdf(db, record.id)
        st.download_button("Baixar PDF desta versão", pdf, report_filename(payload["sample"]["code"], record.number),
                           mime="application/pdf", key=f"report:download:{record.id}")
    except ReportError as error:
        st.error(str(error))

    render_delivery_section(record, grant, current=statuses[number] == "Vigente", is_admin=is_admin)

    if is_admin and statuses[number] == "Vigente":
        with st.expander("Invalidar sem substituição"):
            st.caption("O PDF permanece no histórico. Nenhuma versão anterior voltará a ficar vigente.")
            with st.form(f"report:cancel:{record.id}", clear_on_submit=False):
                reason = st.text_area("Motivo da invalidação", max_chars=REASON_LIMIT)
                confirm = st.checkbox("Confirmo a invalidação desta versão.")
                submit = st.form_submit_button("Invalidar versão vigente")
            if submit:
                if not confirm:
                    st.error("Confirme a invalidação.")
                else:
                    try:
                        with session_scope(lock_for_report=True) as db:
                            cancel_report(db, record.id, reason, authorize_work(db, grant))
                        _notice(f"Laudo v{record.number:02d} invalidado. O PDF foi preservado no histórico.")
                    except (ReportError, AccountError, IntegrityError, OperationalError) as error:
                        _write_error(error)


def _draft_inputs(base: ReportDraft, defaults: dict) -> dict:
    suffix = f"{base.sample_id}:{base.number}"
    decisions = {}
    undecided = [row for row in base.grid if row["default"] is None]
    if undecided:
        st.caption("Defina as análises sem indicação. A escolha vale só para esta versão.")
    for row in undecided:
        key = str(row["request_id"])
        options = [None, True, False]
        selected = st.selectbox(
            f"{row['name']} entra no laudo?",
            options,
            index=options.index(defaults.get("decisions", {}).get(key)),
            format_func=lambda v: {None: "Escolha...", True: "Incluir", False: "Não incluir"}[v],
            key=f"report:scope:{suffix}:{key}",
        )
        if selected is not None:
            decisions[key] = selected
    note = st.text_area("Observação para o PDF (opcional)", value=defaults.get("note", ""), max_chars=NOTE_LIMIT,
                        key=f"report:note:{suffix}", help="Aparece no PDF. As observações internas da amostra não são copiadas.")
    reason = ""
    if base.requires_reason:
        reason = st.text_input("Motivo da reemissão ou correção (interno)", value=defaults.get("reason", ""),
                               max_chars=REASON_LIMIT, key=f"report:reason:{suffix}")
    return {"decisions": decisions, "note": note, "reason": reason}


def _render_draft(sample_id: int, grant: Grant) -> None:
    with session_scope() as db:
        base = build_draft(db, sample_id)
        prior_review = unissued_review(db, sample_id)
        defaults = {}
        if base.previous:
            saved = json.loads(base.previous.review.options_json)
            defaults = {"note": saved.get("note", ""), "decisions": saved.get("decisions", {})}
        if prior_review is not None:
            reviewed_options = json.loads(prior_review.options_json)
            same = build_draft(db, sample_id, **reviewed_options)
            if same.review_digest == prior_review.draft_digest and not same.problems:
                defaults = reviewed_options
    st.subheader(f"Prévia v{base.number:02d}")
    sample = base.content["sample"]
    st.text(f"{sample['code']}  |  Cervejaria: {sample['brewery'] or '---'}  |  "
            f"Cerveja: {sample['product'] or '---'}  |  Lote: {sample['lot'] or '---'}")
    if base.legacy_status:
        st.info("Há uma emissão registrada na planilha antiga, mas o PDF não está neste banco. "
                "A numeração começa em v01 no aplicativo e não altera o documento anterior.")
    inputs = _draft_inputs(base, defaults)
    with session_scope() as db:
        draft = build_draft(db, sample_id, **inputs)
        reviewed = pending_review(db, draft)

    if draft.grid:
        st.dataframe([{
            "Parâmetro": f"{r['name']} ({r['unit']})" if r["unit"] else r["name"],
            "Situação da análise": r["status"],
            "No laudo": {True: "Sim", False: "Não", None: "A decidir"}[r["included"]],
            "Resultado na prévia": (
                "Não incluído" if r["included"] is False else r["display"] or "Sem resultado vigente"
            ),
        } for r in draft.grid], hide_index=True, width="stretch")
    if draft.previous:
        delta = changes_between(json.loads(draft.previous.content_json), draft.content)
        if delta:
            st.write("Alterações em relação ao último PDF")
            st.dataframe(delta, hide_index=True, width="stretch")
    if draft.problems:
        for issue in draft.problems:
            st.warning(issue)
        return

    preview = render_report_pdf(draft.content, number=draft.number, replaces_number=draft.replaces_number)
    st.download_button(
        "Baixar prévia do PDF", preview, report_filename(draft.content["sample"]["code"], draft.number, preview=True),
        mime="application/pdf", key=f"report:preview:{sample_id}",
    )
    if reviewed:
        st.success(f"Revisão confirmada por {reviewed.reviewed_by_name} em {_br_time(reviewed.reviewed_at)}.")
    elif prior_review:
        st.info("O conteúdo atual não corresponde à revisão anterior. Confira a nova prévia.")
    # Uma interação de uma tela antiga não confirma os dados de uma prévia nova.
    if st.button("Revisado e pronto para emissão", disabled=reviewed is not None,
                 key=f"report:review:{draft.review_digest}"):
        try:
            with session_scope(lock_for_report=True) as db:
                record_review(db, draft, authorize_work(db, grant))
            st.rerun()
        except (ReportError, AccountError, IntegrityError, OperationalError) as error:
            _write_error(error)
    if st.button("Emitir PDF e registrar versão", type="primary", disabled=reviewed is None,
                 key=f"report:issue:{draft.review_digest}:{reviewed.id if reviewed else ''}"):
        try:
            with session_scope(lock_for_report=True) as db:
                issued = issue_report(db, reviewed.id, authorize_work(db, grant))
            _notice(f"Laudo v{issued.number:02d} emitido. Baixe o PDF no histórico de versões.")
        except (ReportError, AccountError, IntegrityError, OperationalError) as error:
            _write_error(error)


def render_report_page(grant: Grant) -> None:
    st.header("Laudos")
    st.caption("Conferir, emitir e enviar uma versão por vez. Nenhum laudo sai sem confirmação.")
    message = st.session_state.pop("report_notice", None)
    if message:
        st.success(message)
    try:
        with session_scope() as db:
            actor = report_actor(db, authorize_work(db, grant))
            samples = dict(db.execute(select(Sample.id, Sample.fq_number).order_by(Sample.fq_number.desc())).all())
    except (ReportError, AccountError) as error:
        st.error(str(error))
        return
    if not samples:
        st.info("Cadastre ou importe uma amostra FQ para gerar laudos.")
        return
    sample_id = st.selectbox("Amostra FQ", list(samples), format_func=lambda i: fq_code(samples[i]), index=None)
    if sample_id is None:
        return
    _render_history(sample_id, grant, is_admin=actor.role == "ADMIN")
    st.divider()
    _render_draft(sample_id, grant)
