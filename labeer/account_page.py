"""Telas Streamlit de login, troca de senha e administração de contas."""

from __future__ import annotations

import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from .accounts import (
    ROLES, AccountError, AccountView, Grant, change_password, create_account,
    list_accounts, login, refresh_session, reset_account_password, set_account_active,
)
from .db import session_scope
from .security import MAX_LOGIN_LENGTH, MAX_PASSWORD_LENGTH, MIN_PASSWORD_LENGTH


GRANT_KEY = "auth:grant"
_NOTICE_KEY = "auth:notice"
_TEMPORARY_KEY = "auth:temporary"


def write_account_error(error: Exception) -> None:
    if isinstance(error, ValueError):
        st.error(str(error))
    else:
        st.error("Não foi possível acessar o banco neste momento. Tente novamente.")


def authenticate() -> tuple[AccountView, Grant] | None:
    grant = st.session_state.get(GRANT_KEY)
    if grant is not None:
        try:
            with session_scope() as db:
                account, grant = refresh_session(db, grant)
            st.session_state[GRANT_KEY] = grant
            return account, grant
        except AccountError:
            st.session_state.clear()
            st.info("Sessão encerrada. Entre novamente.")
        except SQLAlchemyError as error:
            write_account_error(error)
            return None

    st.title("LaBeer")
    st.caption("Gestão de amostras, análises e laudos")
    with st.form("login", clear_on_submit=True):
        email = st.text_input("E-mail", max_chars=254)
        password = st.text_input("Senha", type="password", max_chars=MAX_LOGIN_LENGTH)
        entered = st.form_submit_button("Entrar", width="stretch")
    if entered:
        try:
            with session_scope(lock_for_report=True) as db:
                grant = login(db, email, password)
        except SQLAlchemyError as error:
            write_account_error(error)
            return None
        if grant is None:
            st.error("Não foi possível entrar. Confira os dados ou tente mais tarde.")
        else:
            st.session_state[GRANT_KEY] = grant
            st.rerun()
    return None


def show_account_notice() -> None:
    notice = st.session_state.pop(_NOTICE_KEY, None)
    if notice:
        st.success(notice)


def render_my_account(account: AccountView, grant: Grant) -> None:
    st.header("Trocar senha provisória" if account.must_change else "Minha conta")
    st.caption(f"{account.email} · {ROLES[account.role]}")
    if account.must_change:
        st.info("Antes de acessar as amostras e os laudos, escolha uma senha que só você conheça.")
    with st.form("auth:change", clear_on_submit=True):
        current = st.text_input("Senha provisória" if account.must_change else "Senha atual",
                                type="password", max_chars=MAX_LOGIN_LENGTH)
        new = st.text_input("Nova senha", type="password", max_chars=MAX_PASSWORD_LENGTH,
                            help=f"Use de {MIN_PASSWORD_LENGTH} a {MAX_PASSWORD_LENGTH} caracteres.")
        confirmation = st.text_input("Confirme a nova senha", type="password", max_chars=MAX_PASSWORD_LENGTH)
        changed = st.form_submit_button("Salvar nova senha")
    if changed:
        try:
            with session_scope(lock_for_report=True) as db:
                renewed = change_password(db, grant, current, new, confirmation)
            st.session_state[GRANT_KEY] = renewed
            st.session_state[_NOTICE_KEY] = "Senha alterada. As outras sessões desta conta foram encerradas."
            st.rerun()
        except (ValueError, SQLAlchemyError) as error:
            write_account_error(error)


def _status(account: AccountView) -> str:
    if not account.active:
        return "Desativada"
    if account.temporary_expired:
        return "Senha provisória expirada"
    if account.must_change:
        return "Aguardando troca de senha"
    return "Ativa"


def _new_credential(email: str, temporary: str) -> None:
    st.session_state[_TEMPORARY_KEY] = (email.strip().lower(), temporary)
    st.rerun()


def render_account_admin(grant: Grant) -> None:
    st.subheader("Usuários")
    try:
        with session_scope() as db:
            accounts = list_accounts(db, grant)
    except (AccountError, SQLAlchemyError) as error:
        st.session_state.pop(_TEMPORARY_KEY, None)
        write_account_error(error)
        return
    credential = st.session_state.pop(_TEMPORARY_KEY, None)
    if credential is not None:
        email, temporary = credential
        st.success(f"Senha provisória gerada para {email}.")
        st.caption("Copie agora e compartilhe em particular com a pessoa. Expira em 24 horas e só aparece aqui uma vez.")
        st.code(temporary, language=None)

    st.dataframe([{
        "Nome": a.name, "E-mail": a.email, "Perfil": ROLES.get(a.role, a.role), "Situação": _status(a),
    } for a in accounts], hide_index=True, width="stretch")
    with st.expander("Criar usuário"):
        with st.form("auth:create", clear_on_submit=True):
            name = st.text_input("Nome", max_chars=120)
            email = st.text_input("E-mail da conta", max_chars=254)
            role = st.selectbox("Perfil", ["ANALISTA", "ADMIN"], format_func=ROLES.__getitem__)
            created = st.form_submit_button("Criar conta")
        if created:
            try:
                with session_scope(lock_for_report=True) as db:
                    temporary = create_account(db, grant, name, email, role)
                _new_credential(email, temporary)
            except (ValueError, SQLAlchemyError) as error:
                write_account_error(error)

    choices = {a.id: a for a in accounts if a.id != grant.user_id and a.role in ROLES}
    if not choices:
        return
    with st.expander("Gerenciar conta existente"):
        selected = st.selectbox(
            "Conta", list(choices), format_func=lambda i: f"{choices[i].name} · {choices[i].email}", index=None,
        )
        if selected is None:
            return
        target = choices[selected]
        state_action = "Desativar conta" if target.active else "Reativar conta"
        actions = [state_action, "Gerar nova senha provisória"] if target.active else [state_action]
        with st.form(f"auth:manage:{target.id}:{target.active}", clear_on_submit=True):
            action = st.selectbox("Operação de conta", actions)
            confirm = st.checkbox("Conferi a conta e confirmo a operação selecionada.")
            pressed = st.form_submit_button("Aplicar operação")
        if pressed:
            if not confirm:
                st.error("Confirme antes de aplicar.")
                return
            try:
                with session_scope(lock_for_report=True) as db:
                    if action == "Gerar nova senha provisória":
                        temporary = reset_account_password(db, grant, target.id)
                    else:
                        set_account_active(db, grant, target.id, not target.active)
                if action == "Gerar nova senha provisória":
                    _new_credential(target.email, temporary)
                else:
                    st.session_state[_NOTICE_KEY] = f"{state_action}: operação concluída para {target.email}."
                    st.rerun()
            except (ValueError, SQLAlchemyError) as error:
                write_account_error(error)
