import pytest
from sqlalchemy.exc import OperationalError

from labeer.database_diagnostics import database_error_code


class DriverError(Exception):
    def __init__(self, message, state=None):
        super().__init__(message)
        self.sqlstate = state


@pytest.mark.parametrize(("message", "state", "expected"), [
    ("password authentication failed for user test", None, "DB_AUTHENTICATION"),
    ("unavailable", "28P01", "DB_AUTHENTICATION"),
    ("channel binding required", None, "DB_TLS_CHANNEL_BINDING"),
    ("SSL error: certificate verify failed", None, "DB_TLS_CERTIFICATE"),
    ("root certificate file does not exist", None, "DB_TLS_CERTIFICATE"),
    ("server certificate does not match host name", None, "DB_TLS_HOSTNAME"),
    ("server does not support SSL", None, "DB_TLS_REQUIRED"),
    ("permission denied for database", "42501", "DB_PERMISSION"),
    ("database does not exist", "3D000", "DB_DATABASE"),
    ("invalid schema", "3F000", "DB_SCHEMA"),
    ("quota exceeded", "53000", "DB_QUOTA"),
    ("could not translate host name", None, "DB_DNS"),
    ("connection timeout expired", None, "DB_TIMEOUT"),
    ("invalid connection option", None, "DB_CONNECTION_OPTIONS"),
    ("unsupported startup parameter", None, "DB_CONNECTION_OPTIONS"),
    ("connection refused", None, "DB_NETWORK"),
    ("unknown failure", None, "DB_UNAVAILABLE"),
])
def test_only_static_diagnostic_codes_are_returned(message, state, expected):
    error = OperationalError("SELECT 1", {}, DriverError(message, state))
    assert database_error_code(error) == expected


def test_diagnostics_never_return_driver_messages_or_connection_secrets(monkeypatch):
    from labeer import account_page

    private_value = "fake-private-value-not-a-real-password"
    original = DriverError("certificate verify failed " + private_value)
    error = OperationalError("postgresql://test:" + private_value + "@example.test/db", {}, original)
    rendered = []
    monkeypatch.setattr(account_page.st, "error", rendered.append)
    account_page.write_account_error(error)
    assert len(rendered) == 1
    assert "DB_TLS_CERTIFICATE" in rendered[0]
    assert private_value not in rendered[0]
    assert "postgresql://" not in rendered[0]


def test_unrecognized_sqlstate_is_not_rendered():
    original = DriverError("unknown failure", "fake-sensitive-value")
    assert database_error_code(OperationalError("SELECT 1", {}, original)) == "DB_UNAVAILABLE"
