"""Local configuration loading and Canvas URL security checks."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit


class ConfigurationError(ValueError):
    """Raised when required local configuration is missing or unsafe."""


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without replacing existing environment values."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def validate_canvas_base_url(value: str) -> str:
    """Return a normalized Canvas base URL or raise for an unsafe URL."""
    candidate = value.strip().rstrip("/")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError(
            "CANVAS_BASE_URL must be a plain HTTPS URL, such as "
            "https://school.instructure.com"
        )
    try:
        port = parsed.port
    except ValueError as error:
        raise ConfigurationError("CANVAS_BASE_URL contains an invalid port.") from error

    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    normalized = SplitResult("https", netloc, parsed.path.rstrip("/"), "", "")
    return urlunsplit(normalized)


def url_has_same_origin(candidate: str, trusted_base: str) -> bool:
    """Return whether two HTTPS URLs have the same host and effective port."""
    candidate_url = urlsplit(candidate)
    base_url = urlsplit(trusted_base)
    if candidate_url.scheme.lower() != "https" or base_url.scheme.lower() != "https":
        return False
    try:
        candidate_port = candidate_url.port or 443
        base_port = base_url.port or 443
    except ValueError:
        return False
    return (
        candidate_url.hostname is not None
        and base_url.hostname is not None
        and candidate_url.hostname.lower() == base_url.hostname.lower()
        and candidate_port == base_port
        and candidate_url.username is None
        and candidate_url.password is None
        and not candidate_url.fragment
    )


@dataclass(frozen=True)
class AppConfig:
    canvas_base_url: str
    api_key: str = field(repr=False)

    @classmethod
    def from_project(cls, project_root: Path) -> "AppConfig":
        load_env_file(project_root / ".env")
        token = os.getenv("API_KEY", "").strip()
        base_url = os.getenv("CANVAS_BASE_URL", "")
        if not token:
            raise ConfigurationError("API_KEY is missing from .env or the environment.")
        if not base_url.strip():
            raise ConfigurationError(
                "CANVAS_BASE_URL is missing from .env or the environment."
            )
        return cls(validate_canvas_base_url(base_url), token)
