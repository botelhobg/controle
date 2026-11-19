from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

st.set_page_config(page_title="LaBeer", page_icon="🍺", layout="wide")
try:
    # Carrega os segredos de nível raiz antes de ler as variáveis de configuração.
    st.secrets.get("LABEER_ENV")
except FileNotFoundError:
    pass

from labeer.account_page import (
    authenticate, render_account_admin, render_my_account, show_account_notice, write_account_error,
)
from labeer.accounts import ROLES, AccountError, authorize_work, bootstrap_admin
from labeer.calculation_page import render_calculation_page
from labeer.config import DeploymentError, settings
from labeer.db import init_database, session_scope
from labeer.importer import LEGACY_REFERENCE, read_result
from labeer.models import AnalysisParameter, AuditEvent, CurrentResult, RequestedAnalysis, Sample, User, fq_code
from labeer.report_page import render_report_page
from labeer.services import add_execution, create_sample, format_result, next_fq_number, seed_parameters


try:
    settings.validate_app(st.get_option("server.address"))
except DeploymentError as error:
    st.error(str(error))
    st.stop()

if settings.is_cloud_pilot:
    st.sidebar.info("Piloto privado do LaBeer · use somente dados fictícios.")


@st.cache_resource(show_spinner=False)
def prepare_database() -> bool:
    init_database()
    with session_scope(lock_for_report=True) as db:
        bootstrap_admin(db, settings.admin_email, settings.admin_password)
        seed_parameters(db)
    return True


try:
    prepare_database()
except (ValueError, SQLAlchemyError) as error:
    write_account_error(error)
    st.stop()


def local_today():
    return datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def render_history(current: RequestedAnalysis) -> None:
    if current.executions:
        selected = current.current_result.execution_id if current.current_result else None
        rows = []
        for execution in sorted(current.executions, key=lambda e: e.execution_number):
            metadata = json.loads(execution.raw_inputs_json) if execution.raw_inputs_json else {}
            origin = ", ".join(f"{row['aba']}:{row['linha']}" for row in metadata.get("linhas", []))
            rows.append({
                "Registro": execution.execution_number,
                "Resultado": format_result(current.parameter, execution.numeric_value, execution.text_value),
                "Data": execution.performed_at.strftime("%d/%m/%Y") if execution.performed_at else "Sem data",
                "Origem": origin or "LaBeer",
                "Vigente": execution.id == selected,
            })
        with st.expander("Histórico de resultados"):
            st.dataframe(rows, width="stretch", hide_index=True)


def render_legacy_references(sample_id: int, parameters: dict[str, AnalysisParameter]) -> None:
    with session_scope() as db:
        events = db.scalars(
            select(AuditEvent).where(
                AuditEvent.entity_type == "sample",
                AuditEvent.entity_id == sample_id,
                AuditEvent.action == LEGACY_REFERENCE,
            ).order_by(AuditEvent.id)
        ).all()
    if not events:
        return
    rows = []
    for event in events:
        item = json.loads(event.details_json)
        parameter = parameters[item["parametro"]]
        numeric = Decimal(item["numerico"]) if item["numerico"] is not None else None
        rows.append({
            "Parâmetro": parameter.name,
            "Valor na origem": str(numeric).replace(".", ",") if numeric is not None else item["textual"] or "—",
            "Unidade": parameter.unit or "",
            "Origem": f"{item['aba']}:{item['linha']}",
        })
    with st.expander("Referências da planilha (não vigentes)"):
        st.caption("Registros das abas de apoio, inclusive de parâmetros sem análise solicitada.")
        st.dataframe(rows, width="stretch", hide_index=True)


authenticated = authenticate()
if not authenticated:
    st.stop()
account, grant = authenticated

with st.sidebar:
    st.title("LaBeer")
    st.caption(f"{account.name} · {ROLES[account.role]}")
    if st.button("Sair"):
        st.session_state.clear()
        st.rerun()
    if not account.must_change:
        pages = ["Dashboard", "Amostras", "Resultados", "Cálculos", "Laudos", "Minha conta"]
        if account.role == "ADMIN":
            pages.append("Administração")
        page = st.radio("Navegação", pages)

show_account_notice()
if account.must_change:
    render_my_account(account, grant)
    st.stop()

if page == "Dashboard":
    st.header("Dashboard")
    today = local_today()
    with session_scope() as db:
        samples = db.scalars(select(Sample).order_by(Sample.due_date.nulls_last(), Sample.fq_number)).all()
        pending = db.scalars(select(RequestedAnalysis).where(RequestedAnalysis.status == "Pendente")).all()
    overdue = [s for s in samples if s.due_date is not None and s.due_date < today and s.status != "Laudo enviado"]
    c1, c2, c3 = st.columns(3)
    c1.metric("Amostras", len(samples))
    c2.metric("Análises pendentes", len(pending))
    c3.metric("Amostras atrasadas", len(overdue))
    st.subheader("Prioridades")
    st.dataframe(
        [
            {
                "FQ": s.code,
                "Cervejaria": s.brewery,
                "Produto": s.product,
                "Prazo": s.due_date.strftime("%d/%m/%Y") if s.due_date else "Sem data",
                "Status": s.status,
            }
            for s in samples[:30]
        ],
        width="stretch",
        hide_index=True,
    )

elif page == "Amostras":
    st.header("Amostras")
    with session_scope() as db:
        preview = next_fq_number(db)
        parameters = db.scalars(
            select(AnalysisParameter).where(AnalysisParameter.active.is_(True)).order_by(AnalysisParameter.name)
        ).all()
        registry = {p.key: p for p in db.scalars(select(AnalysisParameter)).all()}
        users = db.scalars(select(User).where(User.active.is_(True), User.role.in_(ROLES))
                           .order_by(User.name, User.email)).all()
        parameter_options = {f"{p.name} ({p.unit or 'sem unidade'})": p.id for p in parameters}
        user_options = {u.id: f"{u.name} · {u.email}" for u in users}

    st.subheader(f"Nova amostra · próximo código: {fq_code(preview)}")
    with st.form("new_sample", clear_on_submit=True):
        a, b = st.columns(2)
        brewery = a.text_input("Cervejaria *")
        product = b.text_input("Produto *")
        c, d = st.columns(2)
        style = c.text_input("Estilo")
        lot = d.text_input("Lote")
        e, f = st.columns(2)
        received = e.date_input("Recebimento", value=local_today())
        responsible_id = f.selectbox("Responsável", list(user_options), format_func=user_options.__getitem__)
        selected_names = st.multiselect("Análises solicitadas", list(parameter_options))
        notes = st.text_area("Observações")
        save = st.form_submit_button("Cadastrar amostra", type="primary")
    if save:
        if not brewery.strip() or not product.strip():
            st.error("Informe cervejaria e produto.")
        else:
            try:
                with session_scope(lock_for_report=True) as db:
                    actor_id = authorize_work(db, grant)
                    responsible = db.get(User, responsible_id) if responsible_id is not None else None
                    if not responsible or not responsible.active or responsible.role not in ROLES:
                        raise AccountError("Escolha uma conta ativa como responsável.")
                    sample = create_sample(
                        db, brewery=brewery, product=product, style=style, lot=lot,
                        received_date=received, notes=notes, responsible_id=responsible.id,
                        parameter_ids=[parameter_options[name] for name in selected_names], actor_id=actor_id,
                    )
                    code = sample.code
                st.success(f"{code} cadastrada com sucesso.")
            except (AccountError, SQLAlchemyError) as error:
                write_account_error(error)

    with session_scope() as db:
        samples = db.scalars(select(Sample).order_by(Sample.fq_number.desc())).all()
    st.subheader("Amostras cadastradas")
    st.dataframe(
        [{"FQ": s.code, "Cervejaria": s.brewery, "Produto": s.product, "Recebimento": s.received_date, "Prazo": s.due_date, "Status": s.status} for s in samples],
        width="stretch",
        hide_index=True,
    )
    if samples:
        sample_index = {sample.code: sample for sample in samples}
        sample_code = st.selectbox("Consultar referências por FQ", list(sample_index), index=None)
        if sample_code:
            render_legacy_references(sample_index[sample_code].id, registry)

elif page == "Resultados":
    st.header("Resultados e repetições")
    with session_scope() as db:
        requested = db.scalars(
            select(RequestedAnalysis)
            .options(
                selectinload(RequestedAnalysis.sample),
                selectinload(RequestedAnalysis.parameter),
                selectinload(RequestedAnalysis.executions),
                selectinload(RequestedAnalysis.current_result).selectinload(CurrentResult.execution),
            )
            .order_by(RequestedAnalysis.id.desc())
        ).all()
        registry = {p.key: p for p in db.scalars(select(AnalysisParameter)).all()}
        options = {f"{r.sample.code} · {r.parameter.name}": r.id for r in requested}
    if not options:
        st.info("Cadastre uma amostra com análises solicitadas para lançar resultados.")
    else:
        label = st.selectbox("Amostra e análise", list(options))
        selected_id = options[label]
        current = next(r for r in requested if r.id == selected_id)
        st.caption(f"Situação da análise: {current.status}")
        if current.current_result:
            execution = current.current_result.execution
            st.info(
                "Resultado atual: "
                + format_result(current.parameter, execution.numeric_value, execution.text_value)
                + f" · execução {execution.execution_number}"
            )
        render_history(current)
        render_legacy_references(current.sample_id, registry)
        st.caption("Cada novo lançamento cria uma repetição e passa a ser o resultado atual; o histórico é preservado.")
        with st.form("result_form", clear_on_submit=True):
            numeric_text = st.text_input("Valor numérico", help="Use vírgula ou ponto. Deixe vazio para resultado textual.")
            text_value = st.text_input("Resultado textual", help="Ex.: n.d., <10 ou >365")
            notes = st.text_area("Observações da execução")
            submit_result = st.form_submit_button("Registrar nova execução", type="primary")
        if submit_result:
            if bool(numeric_text.strip()) == bool(text_value.strip()):
                st.error("Informe somente um valor numérico ou textual.")
            else:
                try:
                    value = read_result(numeric_text or text_value)
                    if value is None or bool(numeric_text.strip()) != (value.numeric is not None):
                        raise ValueError
                except ValueError:
                    st.error("Resultado inválido. Use um número, n.d. ou um limite como <10.")
                    st.stop()
                try:
                    with session_scope(lock_for_report=True) as db:
                        actor_id = authorize_work(db, grant)
                        req = db.scalar(
                            select(RequestedAnalysis).where(RequestedAnalysis.id == selected_id)
                            .options(selectinload(RequestedAnalysis.executions), selectinload(RequestedAnalysis.current_result))
                        )
                        if req is None:
                            raise AccountError("Análise não encontrada. Selecione novamente.")
                        execution = add_execution(db, req, numeric_value=value.numeric, text_value=value.textual,
                                                  notes=notes, actor_id=actor_id)
                        number = execution.execution_number
                    st.success(f"Execução {number} registrada e selecionada como resultado atual.")
                except (AccountError, SQLAlchemyError) as error:
                    write_account_error(error)

elif page == "Cálculos":
    render_calculation_page()

elif page == "Laudos":
    render_report_page(grant)

elif page == "Minha conta":
    render_my_account(account, grant)

elif page == "Administração":
    st.header("Administração")
    if account.role != "ADMIN":
        st.error("Acesso restrito ao Administrador.")
        st.stop()
    render_account_admin(grant)
    st.divider()
    st.subheader("Parâmetros e preços")
    with session_scope() as db:
        parameters = db.scalars(select(AnalysisParameter).order_by(AnalysisParameter.name)).all()
    st.dataframe(
        [
            {
                "Parâmetro": p.name,
                "Unidade": p.unit,
                "Tipo": p.kind,
                "Casas": p.decimals,
                "Fórmula": p.formula_key,
                "Preço": f"R$ {p.price:.2f}".replace(".", ","),
                "Ativo": p.active,
            }
            for p in parameters
        ],
        width="stretch",
        hide_index=True,
    )
    st.caption("Parâmetros e preços ficam disponíveis para consulta nesta versão.")

