from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


load_dotenv()


class DeploymentError(ValueError):
    """Mensagem segura para exibição, sem a URL ou as credenciais."""


def _parse_url(raw: str) -> URL:
    try:
        return make_url(raw)
    except (ArgumentError, TypeError, ValueError):
        raise DeploymentError("A URL do banco não está no formato esperado.") from None


def postgres_url(raw: str) -> URL:
    url = _parse_url(raw)
    if (url.drivername != "postgresql+psycopg" or not url.host or not url.database
            or not url.username or not url.password):
        raise DeploymentError("Informe um PostgreSQL dedicado no formato postgresql+psycopg.")
    if url.query.get("sslmode") != "verify-full":
        raise DeploymentError("Use sslmode=verify-full para verificar o certificado e o endereço do banco.")
    allowed = {"sslmode", "sslrootcert", "channel_binding"}
    if set(url.query) - allowed or any(not isinstance(v, str) or not v for v in url.query.values()):
        raise DeploymentError("A URL do PostgreSQL tem parâmetros de conexão não suportados.")
    if "sslrootcert" not in url.query:
        url = url.update_query_dict({"sslrootcert": "system"})
    return url


@dataclass(frozen=True)
class Settings:
    environment: str = field(default_factory=lambda: os.getenv("LABEER_ENV", "development").strip().lower())
    database_url: str = field(default_factory=lambda: os.getenv("LABEER_DATABASE_URL", "sqlite:///labeer_dev.db"),
                              repr=False)
    admin_email: str = field(default_factory=lambda: os.getenv("LABEER_ADMIN_EMAIL", "admin@labeer.local").strip().lower())
    admin_password: str = field(default_factory=lambda: os.getenv("LABEER_ADMIN_PASSWORD", ""), repr=False)
    private_pilot_confirmed: bool = field(
        default_factory=lambda: os.getenv("LABEER_CLOUD_PRIVATE_ACK", "").strip().lower() == "true",
    )

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def is_cloud_pilot(self) -> bool:
        return self.environment == "cloud_pilot"

    def database_target(self) -> URL:
        if self.is_cloud_pilot:
            if not self.private_pilot_confirmed:
                raise DeploymentError("Confirme o piloto privado antes de conectar o banco da nuvem. Consulte o guia.")
            return postgres_url(self.database_url)
        if self.is_development:
            url = _parse_url(self.database_url)
            if url.get_backend_name() == "sqlite":
                return url
            raise DeploymentError("O modo local usa SQLite. Para PostgreSQL, configure o piloto privado.")
        raise DeploymentError("Selecione development ou cloud_pilot em LABEER_ENV.")

    def validate_app(self, server_address: str | None) -> None:
        if self.is_development and server_address not in {"127.0.0.1", "::1", "localhost"}:
            raise DeploymentError("Para uso local, inicie com: streamlit run app.py --server.address 127.0.0.1")
        self.database_target()


settings = Settings()

