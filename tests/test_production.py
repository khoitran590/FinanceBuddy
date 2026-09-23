from __future__ import annotations

import pytest

from src.services.production import validate_production_configuration


def _valid_secrets():
    return {
        "supabase": {
            "url": "https://project.supabase.co",
            "publishable_key": "sb_publishable_test",
            "public_app_url": "https://finance.example",
        },
        "plaid": {
            "client_id": "plaid-client",
            "secret": "plaid-secret",
            "environment": "production",
            "token_encryption_key": "b" * 64,
            "webhook_url": "https://api.finance.example/plaid/webhook",
        },
    }


def test_development_does_not_require_production_configuration(monkeypatch):
    monkeypatch.delenv("FINANCEBUDDY_ENV", raising=False)
    assert validate_production_configuration({}) == []


def test_valid_production_configuration(monkeypatch):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    assert validate_production_configuration(_valid_secrets()) == []


def test_production_rejects_local_app_url_and_sandbox_plaid(monkeypatch):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    secrets = _valid_secrets()
    secrets["supabase"]["public_app_url"] = "http://localhost:8501"
    secrets["plaid"]["environment"] = "sandbox"

    errors = validate_production_configuration(secrets)

    assert any("supabase.public_app_url" in error for error in errors)
    assert any("must be production" in error for error in errors)


def test_production_requires_explicit_encryption_and_supabase_key(monkeypatch):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    secrets = _valid_secrets()
    secrets["plaid"]["token_encryption_key"] = ""
    secrets["supabase"]["publishable_key"] = ""

    errors = validate_production_configuration(secrets)

    assert any("plaid.token_encryption_key" in error for error in errors)
    assert any("supabase.publishable_key" in error for error in errors)


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://",
        "https://localhost:8501",
        "https://127.0.0.1",
        "https://192.168.1.2",
        "https://internal.local",
        "https://user:password@finance.example",
        "https://finance.example/callback",
        "https://finance.example?next=/dashboard",
        "https://finance.example#dashboard",
        "https://finance.example:invalid",
    ],
)
def test_production_requires_public_root_app_url(monkeypatch, bad_url):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    secrets = _valid_secrets()
    secrets["supabase"]["public_app_url"] = bad_url

    errors = validate_production_configuration(secrets)

    assert any("supabase.public_app_url" in error for error in errors)


def test_production_rejects_supabase_url_with_path_or_credentials(monkeypatch):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    secrets = _valid_secrets()
    secrets["supabase"]["url"] = "https://secret@project.supabase.co/auth/v1"

    errors = validate_production_configuration(secrets)

    assert any("supabase.url" in error for error in errors)


def test_production_rejects_unimplemented_plaid_oauth_redirect(monkeypatch):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    secrets = _valid_secrets()
    secrets["plaid"]["redirect_uri"] = "https://localhost:8501/plaid/callback"

    errors = validate_production_configuration(secrets)

    assert any("plaid.redirect_uri" in error for error in errors)


def test_production_rejects_privileged_supabase_keys(monkeypatch):
    import base64
    import json
    from src.services.supabase import SupabaseConfig

    monkeypatch.setenv('FINANCEBUDDY_ENV', 'production')
    for key in ('sb_secret_example', '.'.join([
        'header', base64.urlsafe_b64encode(json.dumps({'role': 'service_role'}).encode()).decode().rstrip('='), 'signature'
    ])):
        secrets = _valid_secrets()
        secrets['supabase']['publishable_key'] = key
        assert any('privileged' in error for error in validate_production_configuration(secrets))
        assert not SupabaseConfig('https://project.supabase.co', key).is_configured
