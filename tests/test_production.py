from __future__ import annotations

from src.services.production import validate_production_configuration


def _valid_secrets():
    return {
        "auth": {
            "redirect_uri": "https://finance.example/oauth2callback",
            "cookie_secret": "a" * 64,
            "client_id": "client-id",
            "client_secret": "client-secret",
            "server_metadata_url": "https://identity.example/.well-known/openid-configuration",
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


def test_valid_production_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    monkeypatch.setenv("FINANCEBUDDY_DATA_DIR", str(tmp_path))
    assert validate_production_configuration(_valid_secrets()) == []


def test_production_rejects_local_auth_and_sandbox_plaid(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    monkeypatch.setenv("FINANCEBUDDY_DATA_DIR", str(tmp_path))
    secrets = _valid_secrets()
    secrets["auth"]["redirect_uri"] = "http://localhost:8501/oauth2callback"
    secrets["plaid"]["environment"] = "sandbox"

    errors = validate_production_configuration(secrets)

    assert any("auth.redirect_uri" in error for error in errors)
    assert any("must be production" in error for error in errors)


def test_production_requires_explicit_encryption_and_persistent_path(monkeypatch):
    monkeypatch.setenv("FINANCEBUDDY_ENV", "production")
    monkeypatch.delenv("FINANCEBUDDY_DATA_DIR", raising=False)
    secrets = _valid_secrets()
    secrets["plaid"]["token_encryption_key"] = ""

    errors = validate_production_configuration(secrets)

    assert any("plaid.token_encryption_key" in error for error in errors)
    assert any("FINANCEBUDDY_DATA_DIR" in error for error in errors)
