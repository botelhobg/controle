"""Contas individuais e autorização por sessão (sem credenciais em logs)."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AccountState, AuditEvent, User, utc_now
from .security import (
    CURRENT_PASSWORD_FORMAT, MAX_LOGIN_LENGTH, hash_password, needs_rehash, validate_new_password,
    verify_password,
)


ROLES = {"ADMIN": "Administrador", "ANALISTA": "Analista/Responsável"}
TEMPORARY_LIFETIME = timedelta(hours=24)
SESSION_IDLE = timedelta(minutes=30)
SESSION_MAX = timedelta(hours=8)
FAILURE_LIMIT = 5
FAILURE_WINDOW = timedelta(minutes=15)
LOCK_PERIOD = timedelta(minutes=5)
_DUMMY_HASH = "$".join((CURRENT_PASSWORD_FORMAT, "0" * 32, "0" * 128))
_EMAIL_LOCAL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", re.ASCII)
_DOMAIN_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


class AccountError(ValueError):
    """Erro sem detalhes internos, para exibir no aplicativo."""


@dataclass(frozen=True)
class Grant:
    user_id: int
    generation: int
    issued_at: datetime
    last_seen: datetime


@dataclass(frozen=True)
class AccountView:
    id: int
    name: str
    email: str
    role: str
    active: bool
    must_change: bool
    temporary_expired: bool


def _email(value: str) -> str:
    email = value.strip().lower()
    local, _, host = email.partition("@")
    if (len(email) > 254 or not _EMAIL_LOCAL.fullmatch(local) or local.startswith(".")
            or local.endswith(".") or ".." in local or "." not in host
            or any(not _DOMAIN_LABEL.fullmatch(label) for label in host.split("."))):
        raise AccountError("Informe um endereço de e-mail válido.")
    return email


def _state(db: Session, user: User) -> AccountState:
    state = db.get(AccountState, user.id)
    if state is None:
        state = AccountState(user_id=user.id)
        db.add(state)
        db.flush()
    return state


def _audit(db: Session, action: str, target: User, actor: User | None) -> None:
    db.add(AuditEvent(
        entity_type="account", entity_id=target.id, action=action, user_id=actor.id if actor else None,
    ))


def bootstrap_admin(db: Session, email: str, password: str) -> bool:
    """Use a configuração só quando ainda não houver um administrador no banco."""
    if db.scalar(select(User.id).where(User.role == "ADMIN").limit(1)) is not None:
        return False
    if not email or not password:
        raise AccountError("No primeiro acesso, defina LABEER_ADMIN_EMAIL e LABEER_ADMIN_PASSWORD no .env.")
    email = _email(email)
    validate_new_password(password)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise AccountError("O e-mail de administrador inicial já pertence a outra conta.")
    user = User(email=email, name="Administrador LaBeer", role="ADMIN", password_hash=hash_password(password))
    db.add(user)
    db.flush()
    _state(db, user)
    _audit(db, "account_bootstrapped", user, None)
    return True


def _clear_failures(state: AccountState) -> None:
    state.failures, state.failure_window, state.locked_until = 0, None, None


def _temporary_expired(state: AccountState | None, now: datetime) -> bool:
    return bool(state and state.must_change and state.temporary_until and now >= state.temporary_until)


def login(db: Session, email: str, password: str) -> Grant | None:
    """A sessão de chamada deve serializar escritas. Falhas retornam sem abortar a transação."""
    email = email.strip().lower()
    if not email or len(email) > 254 or not 1 <= len(password) <= MAX_LOGIN_LENGTH:
        return None
    now = utc_now()
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.active or user.role not in ROLES or not user.password_hash:
        verify_password(password, _DUMMY_HASH)
        return None
    state = _state(db, user)
    if _temporary_expired(state, now):
        verify_password(password, _DUMMY_HASH)
        return None
    if state.locked_until is not None:
        if now < state.locked_until:
            return None
        _clear_failures(state)
    if not verify_password(password, user.password_hash):
        if state.failure_window is None or now - state.failure_window >= FAILURE_WINDOW:
            _clear_failures(state)
            state.failure_window = now
        state.failures += 1
        if state.failures >= FAILURE_LIMIT:
            state.locked_until = now + LOCK_PERIOD
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    _clear_failures(state)
    return Grant(user.id, state.generation, now, now)


def _authorized(
    db: Session, grant: Grant, *, admin_only: bool = False, allow_temporary: bool = False,
) -> tuple[User, AccountState]:
    now = utc_now()
    if now - grant.issued_at >= SESSION_MAX or now - grant.last_seen >= SESSION_IDLE:
        raise AccountError("Sessão encerrada. Entre novamente.")
    user, state = db.get(User, grant.user_id), db.get(AccountState, grant.user_id)
    if (not user or not state or not user.active or user.role not in ROLES
            or state.generation != grant.generation or _temporary_expired(state, now)):
        raise AccountError("Sessão encerrada. Entre novamente.")
    if state.must_change and not allow_temporary:
        raise AccountError("Troque a senha provisória antes de acessar o aplicativo.")
    if admin_only and user.role != "ADMIN":
        raise AccountError("Acesso restrito ao Administrador.")
    return user, state


def _view(user: User, state: AccountState | None, now: datetime) -> AccountView:
    return AccountView(user.id, user.name, user.email, user.role, user.active,
                       bool(state and state.must_change), _temporary_expired(state, now))


def refresh_session(db: Session, grant: Grant) -> tuple[AccountView, Grant]:
    user, state = _authorized(db, grant, allow_temporary=True)
    now = utc_now()
    return _view(user, state, now), replace(grant, last_seen=now)


def authorize_work(db: Session, grant: Grant) -> int:
    user, _ = _authorized(db, grant)
    return user.id


def list_accounts(db: Session, grant: Grant) -> list[AccountView]:
    _authorized(db, grant, admin_only=True)
    rows = db.execute(select(User, AccountState).outerjoin(AccountState, AccountState.user_id == User.id)
                      .order_by(User.name, User.email))
    now = utc_now()
    return [_view(user, state, now) for user, state in rows]


def _target(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or user.role not in ROLES:
        raise AccountError("Conta não encontrada.")
    return user


def _new_temporary(user: User, state: AccountState) -> str:
    password = secrets.token_urlsafe(24)
    user.password_hash = hash_password(password)
    state.generation += 1
    state.must_change = True
    state.temporary_until = utc_now() + TEMPORARY_LIFETIME
    _clear_failures(state)
    return password


def create_account(db: Session, grant: Grant, name: str, email: str, role: str) -> str:
    actor, _ = _authorized(db, grant, admin_only=True)
    name, email = name.strip(), _email(email)
    if not 1 <= len(name) <= 120 or not name.isprintable() or role not in ROLES:
        raise AccountError("Informe um nome e escolha um dos dois perfis.")
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise AccountError("Já existe uma conta para este e-mail.")
    user = User(name=name, email=email, role=role)
    db.add(user)
    db.flush()
    temporary = _new_temporary(user, _state(db, user))
    _audit(db, "account_created", user, actor)
    return temporary


def set_account_active(db: Session, grant: Grant, user_id: int, active: bool) -> None:
    actor, _ = _authorized(db, grant, admin_only=True)
    user = _target(db, user_id)
    if actor.id == user.id:
        raise AccountError("Você não pode desativar a própria conta de administrador.")
    if user.active == active:
        return
    state = _state(db, user)
    user.active = active
    state.generation += 1
    _clear_failures(state)
    _audit(db, "account_reactivated" if active else "account_disabled", user, actor)


def reset_account_password(db: Session, grant: Grant, user_id: int) -> str:
    actor, _ = _authorized(db, grant, admin_only=True)
    user = _target(db, user_id)
    if actor.id == user.id:
        raise AccountError("Para trocar a própria senha, use Minha conta.")
    if not user.active:
        raise AccountError("Reative a conta antes de redefinir a senha.")
    temporary = _new_temporary(user, _state(db, user))
    _audit(db, "account_password_reset", user, actor)
    return temporary


def change_password(db: Session, grant: Grant, current: str, new: str, confirmation: str) -> Grant:
    user, state = _authorized(db, grant, allow_temporary=True)
    validate_new_password(new)
    if new != confirmation:
        raise AccountError("A confirmação da nova senha não confere.")
    if not 1 <= len(current) <= MAX_LOGIN_LENGTH or not verify_password(current, user.password_hash):
        raise AccountError("Senha atual incorreta.")
    if new == current:
        raise AccountError("A nova senha precisa ser diferente da atual.")
    user.password_hash = hash_password(new)
    state.must_change = False
    state.temporary_until = None
    state.generation += 1
    _clear_failures(state)
    _audit(db, "account_password_changed", user, user)
    now = utc_now()
    return Grant(user.id, state.generation, now, now)
