from __future__ import annotations

import os
from typing import Any, Mapping
from urllib.parse import urlparse


def _section(values: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = values.get(name, {}) if hasattr(values, "get") else {}
    return section if hasattr(section, "get") else {}


def _value(section: Mapping[str, Any], key: str, env_name: str) -> str:
    return str(os.getenv(env_name) or section.get(key) or "").strip()


def validate_production_configuration(secrets: Mapping[str, Any]) -> list[str]:
    """Return safe, field-level errors without exposing any configured secret."""
    if os.getenv("FINANCEBUDDY_ENV", "development").lower() != "production":
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

    for field, value in (
        ("supabase.url", _value(supabase, "url", "SUPABASE_URL")),
        (
            "supabase.public_app_url",
            _value(supabase, "public_app_url", "PUBLIC_APP_URL"),
        ),
    ):
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname in {"localhost", "127.0.0.1"}:
            errors.append(f"{field} must be a public HTTPS URL in production.")

    plaid_environment = _value(plaid, "environment", "PLAID_ENV").lower()
    if plaid_environment != "production":
        errors.append("plaid.environment must be production.")

    for field, value in (
        ("plaid.redirect_uri", _value(plaid, "redirect_uri", "PLAID_REDIRECT_URI")),
        ("plaid.webhook_url", _value(plaid, "webhook_url", "PLAID_WEBHOOK_URL")),
    ):
        if value and urlparse(value).scheme != "https":
            errors.append(f"{field} must use HTTPS when configured.")

    return errors
