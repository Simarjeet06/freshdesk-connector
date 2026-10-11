"""Connector configuration, loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError

_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader. Variables that are already set in the environment take precedence."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def normalize_domain(raw: str) -> str:
    """Accept 'acme', 'acme.freshdesk.com' or 'https://acme.freshdesk.com/' and return 'acme'."""
    d = raw.strip().lower()
    d = re.sub(r"^https?://", "", d).rstrip("/")
    d = d.removesuffix(".freshdesk.com")
    if not _DOMAIN_RE.match(d):
        raise ConfigError(
            f"FRESHDESK_DOMAIN {raw!r} is not a valid Freshdesk subdomain (expected e.g. 'acme')"
        )
    return d


@dataclass(frozen=True)
class Settings:
    domain: str
    api_key: str
    mock: bool = False
    rate_limit_per_min: int = 50
    max_retry_wait_seconds: int = 30
    max_retries: int = 3
    timeout_seconds: float = 20.0
    include_private_notes: bool = False
    redact_pii: bool = False
    connector_auth_token: str | None = None

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}.freshdesk.com/api/v2"

    @property
    def portal_url(self) -> str:
        return f"https://{self.domain}.freshdesk.com"

    @classmethod
    def from_env(cls) -> "Settings":
        mock = _flag("FRESHDESK_MOCK")
        domain = os.environ.get("FRESHDESK_DOMAIN", "").strip()
        api_key = os.environ.get("FRESHDESK_API_KEY", "").strip()

        if mock:
            domain = domain if domain and domain != "your-subdomain" else "demo-merchant"
            api_key = "mock-api-key"
        else:
            if not domain or domain == "your-subdomain":
                raise ConfigError("FRESHDESK_DOMAIN is not set (or set FRESHDESK_MOCK=1 for demo data)")
            if not api_key or api_key == "replace-me":
                raise ConfigError("FRESHDESK_API_KEY is not set (or set FRESHDESK_MOCK=1 for demo data)")

        rate = _int("FRESHDESK_RATE_LIMIT_PER_MIN", 50)
        if rate < 1:
            raise ConfigError("FRESHDESK_RATE_LIMIT_PER_MIN must be >= 1")

        token = os.environ.get("CONNECTOR_AUTH_TOKEN", "").strip() or None
        if token == "generate-a-long-random-string":
            token = None

        return cls(
            domain=normalize_domain(domain),
            api_key=api_key,
            mock=mock,
            rate_limit_per_min=rate,
            max_retry_wait_seconds=_int("FRESHDESK_MAX_RETRY_WAIT_SECONDS", 30),
            max_retries=_int("FRESHDESK_MAX_RETRIES", 3),
            include_private_notes=_flag("FRESHDESK_INCLUDE_PRIVATE_NOTES"),
            redact_pii=_flag("FRESHDESK_REDACT_PII"),
            connector_auth_token=token,
        )
