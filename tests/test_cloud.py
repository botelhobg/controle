from dataclasses import replace

import pytest

from labeer import deliveries, report_mail
from labeer.config import DeploymentError, Settings, postgres_url
from labeer.reports import ReportError


PG = "postgresql+psycopg://piloto:ficticia@postgres.example.test/labeer_piloto?sslmode=verify-full"


def test_local_mode_only_accepts_a_loopback_service_with_sqlite():
    config = Settings(environment="development", database_url="sqlite:///labeer_test.db")
    config.validate_app("127.0.0.1")
    for address in (None, "0.0.0.0", "192.0.2.5"):
        with pytest.raises(DeploymentError, match="uso local"):
            config.validate_app(address)
    with pytest.raises(DeploymentError, match="SQLite"):
        replace(config, database_url=PG).validate_app("127.0.0.1")


def test_pilot_requires_explicit_acknowledgment_and_tls_verified_postgresql():
    config = Settings(environment="cloud_pilot", database_url=PG, private_pilot_confirmed=False)
    with pytest.raises(DeploymentError, match="piloto privado"):
        config.validate_app(None)
    config = replace(config, private_pilot_confirmed=True)
    config.validate_app(None)
    assert config.database_target().query["sslrootcert"] == "system"
    with pytest.raises(DeploymentError, match="PostgreSQL"):
        replace(config, database_url="sqlite:///sintetico.db").validate_app(None)
    with pytest.raises(DeploymentError, match="verify-full"):
        replace(config, database_url=PG.replace("verify-full", "require")).validate_app(None)
    with pytest.raises(DeploymentError, match="não suportados"):
        replace(config, database_url=PG + "&host=outro.example.test").validate_app(None)
    with pytest.raises(DeploymentError, match="LABEER_ENV"):
        replace(config, environment="outra").validate_app(None)
    assert "ficticia" not in repr(config)
    assert "temporaria" not in repr(replace(config, admin_password="temporaria"))
    assert postgres_url(PG + "&sslrootcert=/caminho/ca.pem").query["sslrootcert"].endswith("ca.pem")


def test_cloud_mode_keeps_smtp_off_even_if_accidentally_enabled(monkeypatch):
    config = Settings(environment="cloud_pilot")
    for module in (report_mail, deliveries):
        monkeypatch.setattr(module, "settings", config)
    monkeypatch.setenv("LABEER_SMTP_ENABLED", "true")
    assert report_mail.smtp_from_environment() is None
    with pytest.raises(ReportError, match="desativado"):
        deliveries.send_report(None, None, 1)
