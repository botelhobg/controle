"""Códigos de diagnóstico estáticos: nunca devolvem mensagens ou credenciais do driver."""

from __future__ import annotations


def database_error_code(error: Exception) -> str:
    original = getattr(error, "orig", error)
    state = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    message = str(original).casefold()

    if state == "28P01" or "password authentication failed" in message:
        return "DB_AUTHENTICATION"
    if "channel binding" in message:
        return "DB_TLS_CHANNEL_BINDING"
    if any(term in message for term in (
        "certificate verify failed", "root certificate", "sslrootcert", "certificate chain",
    )):
        return "DB_TLS_CERTIFICATE"
    if "certificate" in message and any(term in message for term in ("host name", "hostname")):
        return "DB_TLS_HOSTNAME"
    if "does not support ssl" in message or "ssl connection is required" in message:
        return "DB_TLS_REQUIRED"
    if state == "42501" or "permission denied" in message:
        return "DB_PERMISSION"
    if state == "3D000":
        return "DB_DATABASE"
    if state == "3F000":
        return "DB_SCHEMA"
    if state == "53000" or ("quota" in message and "exceed" in message):
        return "DB_QUOTA"
    if "could not translate host name" in message or "name or service not known" in message:
        return "DB_DNS"
    if any(term in message for term in ("timeout", "timed out")):
        return "DB_TIMEOUT"
    if "invalid connection option" in message or "unsupported startup parameter" in message:
        return "DB_CONNECTION_OPTIONS"
    if any(term in message for term in (
        "connection refused", "network is unreachable", "connection reset", "server closed the connection",
    )):
        return "DB_NETWORK"
    return "DB_UNAVAILABLE"
