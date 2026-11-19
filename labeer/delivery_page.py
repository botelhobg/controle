"""Conferência e histórico de envio da versão selecionada em Laudos."""

from __future__ import annotations

import json
from datetime import datetime

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from .accounts import Grant, authorize_work
from .config import settings
from .db import session_scope
from .deliveries import (
    can_reconcile, confirmed_addresses, delivery_history, delivery_status, draft_email,
    reconcile_delivery, record_external, send_report, targets,
)
from .models import ReportVersion, utc_now
from .report_mail import ACCEPTED, BODY_LIMIT, SUBJECT_LIMIT, MailConfigError, SMTPConfig, smtp_from_environment
from .report_pdf import ZONE, local_timestamp, report_filename


def _when(value: datetime | None) -> str:
    return local_timestamp(value).strftime("%d/%m/%Y às %H:%M") if value is not None else "---"


def _notice(report_id: int, message: str, okay: bool = True) -> None:
    st.session_state[f"delivery:notice:{report_id}"] = (okay, message)
    st.rerun()


def _error(error: Exception) -> None:
    if isinstance(error, ValueError):
        st.error(str(error))
    else:
        st.error("Não foi possível concluir o registro. Confira o histórico e a caixa de enviados antes de tentar novamente.")


def _send_form(report: ReportVersion, config: SMTPConfig, grant: Grant) -> None:
    code = json.loads(report.content_json)["sample"]["code"]
    key = f"delivery:smtp:{report.id}"
    recipients = st.text_area(
        "Destinatários (Para)", placeholder="Digite e confira os endereços",
        help="Separe por vírgula, ponto e vírgula ou quebra de linha. Os destinatários veem o campo Para.",
        key=f"{key}:to", max_chars=6000,
    )
    title = "Laudo corrigido LaBeer" if report.replaces_id is not None else "Laudo LaBeer"
    subject = st.text_input("Assunto", value=f"{title} - {code} v{report.number:02d}",
                            max_chars=SUBJECT_LIMIT, key=f"{key}:subject")
    replaces = " Esta versão substitui a versão anterior do aplicativo." if report.replaces_id else ""
    body = st.text_area(
        "Mensagem", value=f"Olá,\n\nSegue em anexo o laudo da amostra {code}, versão v{report.number:02d}."
                          f"{replaces}\n\nAtenciosamente,\nLaBeer",
        max_chars=BODY_LIMIT, key=f"{key}:body",
    )
    if not recipients.strip():
        return
    try:
        draft = draft_email(report, recipients, subject, body, config)
    except ValueError as error:
        st.warning(str(error))
        return
    st.text(f"De: {draft.sender}\nPara: {', '.join(draft.recipients)}\nAnexo: {draft.filename}")
    # A confirmação não continua marcada depois de trocar o anexo, os endereços ou o texto.
    confirm = st.checkbox("Conferi o anexo e autorizo o envio aos endereços acima.",
                          key=f"delivery:confirm:{draft.fingerprint}")
    if st.button("Enviar PDF por e-mail", type="primary", disabled=not confirm,
                 key=f"delivery:send:{draft.fingerprint}"):
        try:
            result = send_report(draft, config, grant.user_id, grant=grant)
            state = delivery_status(result)
            message = f"{draft.filename}: {state}."
            if result.detail:
                message += " " + result.detail
            _notice(report.id, message, result.outcome == ACCEPTED)
        except (ValueError, SQLAlchemyError) as error:
            _error(error)


def _external_form(report: ReportVersion, grant: Grant) -> None:
    now = local_timestamp(utc_now())
    code = json.loads(report.content_json)["sample"]["code"]
    st.caption(f"Anexo a registrar: {report_filename(code, report.number)}. Use somente após enviar esse PDF por fora.")
    with st.form(f"delivery:external:{report.id}"):
        recipients = st.text_area("Destinatários do envio externo", max_chars=6000)
        a, b = st.columns(2)
        day = a.date_input("Data do envio", value=now.date(), max_value=now.date(), format="DD/MM/YYYY")
        hour = b.time_input("Hora (Brasília)", value=now.time().replace(second=0, microsecond=0), step=60)
        reference = st.text_input("Message-ID da mensagem (opcional)", max_chars=255)
        confirmed = st.checkbox("Confirmo que anexei esta versão e que fiz o envio externamente.")
        pressed = st.form_submit_button("Registrar envio externo")
    if pressed:
        if not confirmed or day is None or hour is None:
            st.error("Confirme o envio e informe data e hora.")
            return
        try:
            with session_scope(lock_for_report=True) as db:
                record_external(db, report.id, recipients, datetime.combine(day, hour, tzinfo=ZONE),
                                authorize_work(db, grant), message_id=reference)
            _notice(report.id, "Envio externo registrado. Nenhum e-mail foi disparado pelo aplicativo.")
        except (ValueError, SQLAlchemyError) as error:
            _error(error)


def _reconcile_form(report_id: int, entries, grant: Grant) -> None:
    candidates = {entry.id: entry for entry in entries if can_reconcile(entry)}
    if not candidates:
        return
    st.write("Conferir tentativa sem confirmação")
    st.caption("Confira a caixa de enviados ou o provedor antes de liberar um novo envio. Não cole credenciais.")
    with st.form(f"delivery:reconcile:{report_id}"):
        choice = st.selectbox("Tentativa", list(candidates), format_func=lambda i: f"#{i} · {_when(candidates[i].recorded_at)}")
        verdict = st.selectbox(
            "O que foi verificado?",
            [None, "sent", "not_sent"],
            format_func=lambda v: {
                None: "Selecione...", "sent": "Todos os destinatários foram enviados",
                "not_sent": "Nenhum destinatário foi enviado",
            }[v],
        )
        note = st.text_area("Evidência para o registro", max_chars=500)
        pressed = st.form_submit_button("Registrar conferência")
    if pressed:
        if verdict is None:
            st.error("Escolha o resultado verificado.")
            return
        try:
            with session_scope(lock_for_report=True) as db:
                reconcile_delivery(db, choice, verdict == "sent", note, authorize_work(db, grant))
            _notice(report_id, "Conferência registrada. Nenhum e-mail foi disparado pelo aplicativo.")
        except (ValueError, SQLAlchemyError) as error:
            _error(error)


def render_delivery_section(report: ReportVersion, grant: Grant, *, current: bool, is_admin: bool) -> None:
    st.subheader(f"Envios da versão v{report.number:02d}")
    notice = st.session_state.pop(f"delivery:notice:{report.id}", None)
    if notice:
        (st.success if notice[0] else st.warning)(notice[1])
    with session_scope() as db:
        entries = delivery_history(db, report.id)
    if entries:
        st.dataframe([{
            "Tentativa": row.id, "Situação": delivery_status(row), "Destinatários": ", ".join(targets(row)),
            "Aceitos / informados": ", ".join(confirmed_addresses(row)) or "---",
            "Envio / aceite": _when(row.sent_at), "Registro": _when(row.recorded_at),
            "Responsável": row.recorded_by_name, "Remetente": row.sender or "---",
            "Assunto": row.subject or "---", "Message-ID": row.message_id or "---",
            "Detalhe": row.detail or "---",
            "Conferência": (
                f"{_when(row.resolved_at)} · {row.resolved_by_name}: {row.resolution_note}" if row.resolved_at else "---"
            ),
        } for row in entries], hide_index=True, width="stretch")
    else:
        st.caption("Sem envio registrado para esta versão no aplicativo.")
    if is_admin:
        _reconcile_form(report.id, entries, grant)
    if not current:
        st.caption("Esta versão está no histórico e não pode ser enviada novamente pelo aplicativo.")
        return

    st.caption("O aceite SMTP confirma a entrada no servidor de envio; não comprova entrega ou leitura pelo destinatário.")
    with st.expander("Enviar ou registrar", expanded=False):
        if settings.is_cloud_pilot:
            st.info("Piloto: envio de e-mails desativado. Use apenas endereços fictícios nos registros de teste.")
            _external_form(report, grant)
            return
        try:
            config = smtp_from_environment()
        except MailConfigError as error:
            st.warning(str(error))
            config = None
        if config is None:
            st.info("Envio pelo aplicativo desativado. Configure o SMTP ou registre um envio já feito por fora.")
            _external_form(report, grant)
        else:
            mode = st.radio("Operação", ["Enviar por e-mail", "Registrar envio externo"],
                            key=f"delivery:mode:{report.id}")
            if mode == "Enviar por e-mail":
                _send_form(report, config, grant)
            else:
                _external_form(report, grant)
