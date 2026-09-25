from __future__ import annotations

import os
import base64
import binascii
import json
from ipaddress import ip_address
from typing import Any, Mapping
from urllib.parse import urlsplit


def _section(values: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = values.get(name, {}) if hasattr(values, "get") else {}
    return section if hasattr(section, "get") else {}


def _value(section: Mapping[str, Any], key: str, env_name: str) -> str:
    return str(os.getenv(env_name) or section.get(key) or "").strip()


def _is_public_https_url(value: str, *, origin_only: bool = False) -> bool:
    """Reject local, malformed, and credential-bearing production URLs."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or (origin_only and parsed.path not in ("", "/"))
            or (origin_only and parsed.query)
            or parsed.fragment
            or host == "localhost"
            or host.endswith((".localhost", ".local"))
        ):
            return False
        # urlsplit does not validate a malformed port until .port is accessed.
        parsed.port
        try:
            return ip_address(host).is_global
        except ValueError:
            return "." in host
    except ValueError:
        return False


def _is_privileged_supabase_key(value: str) -> bool:
    """Fail closed for known key formats that would bypass user RLS."""
    if value.startswith("sb_secret_"):
        return True
    parts = value.split(".")
    if len(parts) != 3:
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return isinstance(payload, dict) and payload.get("role") in {"service_role", "supabase_admin"}


def validate_production_configuration(secrets: Mapping[str, Any]) -> list[str]:
    """Return safe, field-level errors without exposing any configured secret."""
    # Hosts like Streamlit Community Cloud only offer secrets, so accept the flag there too.
    environment = os.getenv("FINANCEBUDDY_ENV") or (
        secrets.get("FINANCEBUDDY_ENV") if hasattr(secrets, "get") else None
    )
    if str(environment or "development").strip().lower() != "production":
        return []

    supabase = _section(secrets, "supabase")
    plaid = _section(secrets, "plaid")
    errors: list[str] = []

    required = (
        ("supabase.url", _value(supabase, "url", "SUPABASE_URL")),
        (
            "supabase.publishable_key",
            _value(supabase, "publishable_key", "SUPABASE_PUBLISHABLE_KEY"),
        ),
        ("plaid.client_id", _value(plaid, "client_id", "PLAID_CLIENT_ID")),
        ("plaid.secret", _value(plaid, "secret", "PLAID_SECRET")),
        ("plaid.token_encryption_key", _value(plaid, "token_encryption_key", "PLAID_TOKEN_ENCRYPTION_KEY")),
    )
    for field, value in required:
        if not value:
            errors.append(f"Missing required production setting: {field}.")

    if _is_privileged_supabase_key(_value(supabase, "publishable_key", "SUPABASE_PUBLISHABLE_KEY")):
        errors.append("supabase.publishable_key must be a publishable or anon key, never a privileged key.")

    for field, value in (
        ("supabase.url", _value(supabase, "url", "SUPABASE_URL")),
        (
            "supabase.public_app_url",
            _value(supabase, "public_app_url", "PUBLIC_APP_URL"),
        ),
    ):
        if not _is_public_https_url(value, origin_only=True):
            errors.append(f"{field} must be a public HTTPS origin with no path, query, or fragment in production.")

    plaid_environment = _value(plaid, "environment", "PLAID_ENV").lower()
    if plaid_environment != "production":
        errors.append("plaid.environment must be production.")

    if _value(plaid, "redirect_uri", "PLAID_REDIRECT_URI"):
        errors.append(
            "plaid.redirect_uri must be unset: the embedded Link UI does not resume OAuth redirects yet."
        )

    for field, value in (
        ("plaid.webhook_url", _value(plaid, "webhook_url", "PLAID_WEBHOOK_URL")),
    ):
        if value and not _is_public_https_url(value):
            errors.append(f"{field} must be a public HTTPS URL when configured.")

    return errors
