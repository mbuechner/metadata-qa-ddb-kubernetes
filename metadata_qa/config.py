"""Validated application configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigurationError(ValueError):
    """Raised when application configuration is invalid or ambiguous."""


_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_URL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
DEFAULT_URL_PREFIX = "/"


def _integer(
    environment: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw_value!r}") from exc
    if value < minimum or (maximum is not None and value > maximum):
        expected = f">= {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise ConfigurationError(f"{name} must be {expected}, got {value}")
    return value


def _dns_label(value: str, variable_name: str, *, maximum_length: int = 63) -> str:
    if len(value) > maximum_length or not _DNS_LABEL_RE.fullmatch(value):
        raise ConfigurationError(
            f"{variable_name} must be a valid Kubernetes DNS label (lowercase letters, "
            f"digits and hyphens; maximum {maximum_length} characters), got {value!r}"
        )
    return value


def _cors_origins(raw_value: str | None) -> tuple[str, ...] | str | None:
    # None lets Flask-SocketIO enforce its secure same-origin default.
    if raw_value is None or not raw_value.strip():
        return None
    if raw_value.strip() == "*":
        return "*"
    origins = tuple(origin.strip() for origin in raw_value.split(",") if origin.strip())
    if not origins:
        raise ConfigurationError("CORS_ALLOWED_ORIGINS must contain at least one origin")
    if any("\r" in origin or "\n" in origin for origin in origins):
        raise ConfigurationError("CORS_ALLOWED_ORIGINS must not contain control characters")
    return origins


def _url_prefix(raw_value: str | None) -> str:
    if raw_value is None or not raw_value.strip() or raw_value.strip() == "/":
        return ""
    value = raw_value.strip().rstrip("/")
    if not value.startswith("/") or "//" in value or "\\" in value:
        raise ConfigurationError(
            "URL_PREFIX must be empty or an absolute URL path such as /app/manager"
        )
    segments = value[1:].split("/")
    if any(
        segment in {"", ".", ".."} or _URL_SEGMENT_RE.fullmatch(segment) is None
        for segment in segments
    ):
        raise ConfigurationError(
            "URL_PREFIX contains an invalid path segment; use letters, digits, '.', '_', '~' or '-'"
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings with safe bounds for all externally controlled limits."""

    namespace: str = "ddbmetadata-qa"
    cronjob_name: str = "ddbmetadata-qa"
    secret_key: str = ""
    secret_key_is_ephemeral: bool = False
    cors_allowed_origins: tuple[str, ...] | str | None = None
    url_prefix: str = ""
    log_level: str = "INFO"
    port: int = 8080
    httpauth_username: str | None = None
    httpauth_password: str | None = None
    httpauth_realm: str = "metadata-qa"
    start_pod_timeout_seconds: int = 120
    log_stream_request_timeout_seconds: int = 10
    kubernetes_request_timeout_seconds: int = 10
    job_termination_timeout_seconds: int = 120
    default_log_tail_lines: int = 2_000
    max_log_tail_lines: int = 5_000

    @property
    def httpauth_enabled(self) -> bool:
        return self.httpauth_username is not None and self.httpauth_password is not None

    def is_managed_job_name_candidate(self, name: str) -> bool:
        """Reject invalid/unrelated names before they reach the Kubernetes API."""
        if not _DNS_LABEL_RE.fullmatch(name):
            return False
        return re.fullmatch(rf"{re.escape(self.cronjob_name)}-[0-9]{{10,13}}", name) is not None

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> Settings:
        """Build settings from an environment mapping and reject unsafe ambiguity."""
        env = os.environ if environment is None else environment
        username = env.get("HTTPAUTH_USERNAME") or None
        password = env.get("HTTPAUTH_PASSWORD") or None
        if (username is None) != (password is None):
            raise ConfigurationError(
                "HTTPAUTH_USERNAME and HTTPAUTH_PASSWORD must either both be set or both be unset"
            )

        realm = env.get("HTTPAUTH_REALM", "metadata-qa")
        if not realm or any(character in realm for character in ('"', "\r", "\n")):
            raise ConfigurationError(
                "HTTPAUTH_REALM must not be empty or contain quotes/control characters"
            )

        log_level = env.get("LOG_LEVEL", "INFO").upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ConfigurationError(
                "LOG_LEVEL must be one of CRITICAL, ERROR, WARNING, INFO or DEBUG"
            )

        max_tail_lines = _integer(env, "MAX_LOG_TAIL_LINES", 5_000, maximum=100_000)
        default_tail_lines = _integer(
            env,
            "DEFAULT_LOG_TAIL_LINES",
            min(2_000, max_tail_lines),
            maximum=max_tail_lines,
        )

        configured_secret_key = env.get("SECRET_KEY") or None
        return cls(
            namespace=_dns_label(env.get("NAMESPACE", "ddbmetadata-qa"), "NAMESPACE"),
            # Kubernetes limits CronJob names to 52 characters because generated Job names
            # need room for their suffix. Our timestamp suffix uses the same available space.
            cronjob_name=_dns_label(
                env.get("CRONJOB_NAME", "ddbmetadata-qa"),
                "CRONJOB_NAME",
                maximum_length=52,
            ),
            secret_key=configured_secret_key or os.urandom(32).hex(),
            secret_key_is_ephemeral=configured_secret_key is None,
            cors_allowed_origins=_cors_origins(env.get("CORS_ALLOWED_ORIGINS")),
            url_prefix=_url_prefix(env.get("URL_PREFIX", DEFAULT_URL_PREFIX)),
            log_level=log_level,
            port=_integer(env, "PORT", 8080, maximum=65_535),
            httpauth_username=username,
            httpauth_password=password,
            httpauth_realm=realm,
            start_pod_timeout_seconds=_integer(
                env, "START_POD_TIMEOUT_SECONDS", 120, maximum=3_600
            ),
            log_stream_request_timeout_seconds=_integer(
                env, "LOG_STREAM_REQUEST_TIMEOUT_SECONDS", 10, maximum=600
            ),
            kubernetes_request_timeout_seconds=_integer(
                env, "KUBERNETES_REQUEST_TIMEOUT_SECONDS", 10, maximum=600
            ),
            job_termination_timeout_seconds=_integer(
                env, "JOB_TERMINATION_TIMEOUT_SECONDS", 120, maximum=3_600
            ),
            default_log_tail_lines=default_tail_lines,
            max_log_tail_lines=max_tail_lines,
        )
