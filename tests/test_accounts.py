import hashlib
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from labeer import accounts
from labeer.accounts import (
    AccountError, authorize_work, bootstrap_admin, change_password, create_account, list_accounts,
    login, refresh_session, reset_account_password, set_account_active,
)
from labeer.db import Base
from labeer.models import AccountState, AuditEvent, Sample, User
from labeer.security import hash_password, needs_rehash, validate_new_password, verify_password


ADMIN = "admin@example.test"
ADMIN_PASS = "somente-teste-admin."
ANALYST = "analista@example.test"
NEW_PASS = "outra-senha-fictícia."


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def admin(sessions):
    with sessions.begin() as db:
        assert bootstrap_admin(db, ADMIN, ADMIN_PASS)
        grant = login(db, ADMIN, ADMIN_PASS)
        assert grant is not None
        return grant


def _add_analyst(db, admin):
    temporary = create_account(db, admin, "Pessoa fictícia", ANALYST, "ANALISTA")
    grant = login(db, ANALYST.upper(), temporary)
    assert grant is not None
    return temporary, grant


def test_bootstrap_only_on_first_database_setup(sessions, admin):
    with sessions.begin() as db:
        assert not bootstrap_admin(db, "extra@example.test", "senha-diferente-para-teste")
        assert not bootstrap_admin(db, "", "")
        assert [row.email for row in list_accounts(db, admin)] == [ADMIN]
        assert login(db, ADMIN, ADMIN_PASS) is not None
        assert db.scalar(select(User).where(User.email == ADMIN)).password_hash != ADMIN_PASS


def test_account_is_not_allowed_to_work_until_changing_its_temporary_password(sessions, admin):
    with sessions.begin() as db:
        temporary, pending = _add_analyst(db, admin)
        view, _ = refresh_session(db, pending)
        assert view.must_change and view.role == "ANALISTA"
        with pytest.raises(AccountError, match="senha provisória"):
            authorize_work(db, pending)
        with pytest.raises(AccountError, match="confirmação"):
            change_password(db, pending, temporary, NEW_PASS, ADMIN_PASS)
        with pytest.raises(AccountError, match="Senha atual"):
            change_password(db, pending, "incorreta", NEW_PASS, NEW_PASS)
        grant = change_password(db, pending, temporary, NEW_PASS, NEW_PASS)
        assert authorize_work(db, grant) == view.id
        with pytest.raises(AccountError, match="Sessão encerrada"):
            refresh_session(db, pending)
        assert login(db, ANALYST, temporary) is None
        assert login(db, ANALYST, NEW_PASS) is not None


def test_only_admin_can_manage_accounts_and_cannot_disable_own(sessions, admin):
    with sessions.begin() as db:
        temporary, pending = _add_analyst(db, admin)
        analyst = change_password(db, pending, temporary, NEW_PASS, NEW_PASS)
        with pytest.raises(AccountError, match="própria"):
            set_account_active(db, admin, admin.user_id, False)
        with pytest.raises(AccountError, match="própria"):
            reset_account_password(db, admin, admin.user_id)
        for call in (
            lambda: list_accounts(db, analyst),
            lambda: create_account(db, analyst, "Pessoa extra", "extra@example.test", "ADMIN"),
            lambda: reset_account_password(db, analyst, admin.user_id),
            lambda: set_account_active(db, analyst, admin.user_id, False),
        ):
            with pytest.raises(AccountError, match="restrito"):
                call()
        for email, role, message in (
            (ANALYST.upper(), "ANALISTA", "Já existe"),
            ("analista@@example.test", "ANALISTA", "e-mail válido"),
            ("extra@example.test", "OUTRO", "dois perfis"),
        ):
            with pytest.raises(AccountError, match=message):
                create_account(db, admin, "Pessoa extra", email, role)


def test_disabling_revokes_both_sessions_and_preserves_assignment(sessions, admin):
    with sessions.begin() as db:
        temporary, pending = _add_analyst(db, admin)
        first = change_password(db, pending, temporary, NEW_PASS, NEW_PASS)
        second = login(db, ANALYST, NEW_PASS)
        db.add(Sample(fq_number=997, responsible_id=first.user_id))
    with sessions.begin() as db:
        set_account_active(db, admin, first.user_id, False)
    with sessions.begin() as db:
        for grant in (first, second):
            with pytest.raises(AccountError, match="Sessão encerrada"):
                authorize_work(db, grant)
        assert login(db, ANALYST, NEW_PASS) is None
        assert db.scalar(select(Sample).where(Sample.fq_number == 997)).responsible_id == first.user_id
        set_account_active(db, admin, first.user_id, True)
        with pytest.raises(AccountError, match="Sessão encerrada"):
            refresh_session(db, first)
        assert login(db, ANALYST, NEW_PASS) is not None
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "account_disabled"))
        assert (event.entity_id, event.user_id, event.details_json) == (first.user_id, admin.user_id, None)


def test_reset_revokes_prior_password_and_existing_session(sessions, admin):
    with sessions.begin() as db:
        temporary, pending = _add_analyst(db, admin)
        first = change_password(db, pending, temporary, NEW_PASS, NEW_PASS)
        renewed = reset_account_password(db, admin, first.user_id)
        assert renewed != temporary and renewed != NEW_PASS
    with sessions.begin() as db:
        with pytest.raises(AccountError, match="Sessão encerrada"):
            refresh_session(db, first)
        assert login(db, ANALYST, NEW_PASS) is None
        second = login(db, ANALYST, renewed)
        assert refresh_session(db, second)[0].must_change
        assert db.get(User, first.user_id).password_hash != renewed
        assert all(event.details_json is None for event in db.scalars(select(AuditEvent)))


def test_temporary_password_and_grants_expire(sessions, monkeypatch):
    start = datetime(2026, 1, 1, 12)
    monkeypatch.setattr(accounts, "utc_now", lambda: start)
    with sessions.begin() as db:
        bootstrap_admin(db, ADMIN, ADMIN_PASS)
        admin = login(db, ADMIN, ADMIN_PASS)
        temporary, pending = _add_analyst(db, admin)
        for expired in (
            replace(admin, last_seen=start - accounts.SESSION_IDLE),
            replace(admin, issued_at=start - accounts.SESSION_MAX),
        ):
            with pytest.raises(AccountError, match="Sessão encerrada"):
                refresh_session(db, expired)
        monkeypatch.setattr(accounts, "utc_now", lambda: start + accounts.TEMPORARY_LIFETIME)
        assert login(db, ANALYST, temporary) is None
        time_adjusted = replace(pending, issued_at=accounts.utc_now(), last_seen=accounts.utc_now())
        with pytest.raises(AccountError, match="Sessão encerrada"):
            refresh_session(db, time_adjusted)


def test_throttling_is_per_account_and_survives_new_transactions(sessions, monkeypatch):
    start = datetime(2026, 1, 1, 12)
    monkeypatch.setattr(accounts, "utc_now", lambda: start)
    with sessions.begin() as db:
        bootstrap_admin(db, ADMIN, ADMIN_PASS)
        admin = login(db, ADMIN, ADMIN_PASS)
        temporary, _ = _add_analyst(db, admin)
    for _ in range(accounts.FAILURE_LIMIT):
        with sessions.begin() as db:
            assert login(db, ADMIN, "incorreta") is None
    with sessions.begin() as db:
        assert login(db, ADMIN, ADMIN_PASS) is None
        assert login(db, ANALYST, temporary) is not None
        assert db.get(AccountState, admin.user_id).locked_until == start + accounts.LOCK_PERIOD
    monkeypatch.setattr(accounts, "utc_now", lambda: start + accounts.LOCK_PERIOD + timedelta(seconds=1))
    with sessions.begin() as db:
        assert login(db, ADMIN, ADMIN_PASS) is not None
        assert db.get(AccountState, admin.user_id).failures == 0


def test_v05_hash_remains_valid_after_adding_state_table(tmp_path):
    salt = bytes(range(16))
    expected = hashlib.scrypt(ADMIN_PASS.encode(), salt=salt, n=2**14, r=8, p=1).hex()
    legacy_hash = f"scrypt${salt.hex()}${expected}"
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(engine, tables=[t for t in Base.metadata.sorted_tables
                                             if t.name != AccountState.__tablename__])
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as db:
        db.add(User(email=ADMIN, name="Conta anterior", role="ADMIN", password_hash=legacy_hash))
    before = inspect(engine).get_table_names()
    Base.metadata.create_all(engine)
    assert set(inspect(engine).get_table_names()) - set(before) == {AccountState.__tablename__}
    with sessions.begin() as db:
        assert not bootstrap_admin(db, "", "")
        grant = login(db, ADMIN, ADMIN_PASS)
        user, _ = refresh_session(db, grant)
        assert user.name == "Conta anterior"
        assert not needs_rehash(db.get(User, user.id).password_hash)
    engine.dispose()


def test_password_hash_formats_and_length_validation():
    password = " minha frase secreta de teste "
    first = hash_password(password)
    assert first != hash_password(password)
    assert verify_password(password, first)
    assert not verify_password(password.strip(), first)
    assert not verify_password(password, "hash-invalid")
    for value in ("curta", "x" * 129, "troque-esta-senha", " " * 16):
        with pytest.raises(ValueError):
            validate_new_password(value)
    validate_new_password("a" * 128)
