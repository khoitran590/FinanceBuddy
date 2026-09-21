from __future__ import annotations

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
