from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from src.domain.models import Transaction
from src.repositories.supabase_repo import SupabaseTransactionRepository
from src.services.supabase import (
    SupabaseAuth,
    SupabaseConfig,
    SupabaseDataClient,
    SupabaseError,
    has_verified_email,
    normalized_session,
)


def test_supabase_password_login_uses_publishable_key_and_normalizes_session():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "sb_publishable_test"
        assert request.url.path.endswith("/auth/v1/token")
        assert request.url.params["grant_type"] == "password"
        assert json.loads(request.content) == {
            "email": "person@example.com",
            "password": "long-test-password",
        }
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
            },
        )

    config = SupabaseConfig("https://project.supabase.co", "sb_publishable_test")
    auth = SupabaseAuth(config, transport=httpx.MockTransport(handler))
    session = normalized_session(auth.sign_in("person@example.com", "long-test-password"))

    assert session["access_token"] == "access"
    assert session["refresh_token"] == "refresh"
    assert session["expires_at"] > 0


@pytest.mark.parametrize("token_type", ["email", "recovery"])
def test_email_link_exchanges_token_hash_for_session(token_type):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth/v1/verify")
        assert json.loads(request.content) == {
            "token_hash": "hash-from-email",
            "type": token_type,
        }
        return httpx.Response(
            200,
            json={"access_token": "access", "refresh_token": "refresh"},
        )

    config = SupabaseConfig("https://project.supabase.co", "sb_publishable_test")
    auth = SupabaseAuth(config, transport=httpx.MockTransport(handler))
    assert auth.verify_email_link("hash-from-email", token_type)["access_token"] == "access"


def test_email_link_rejects_unexpected_type_without_request():
    config = SupabaseConfig("https://project.supabase.co", "sb_publishable_test")
    auth = SupabaseAuth(config)
    with pytest.raises(SupabaseError, match="invalid"):
        auth.verify_email_link("hash-from-email", "invite")


def test_email_verification_is_required_for_financial_access():
    assert has_verified_email({"email": "person@example.com", "email_confirmed_at": "2026-09-20T00:00:00Z"})
    assert not has_verified_email({"email": "person@example.com", "email_confirmed_at": None})
    assert not has_verified_email({"email": "person@example.com", "confirmed_at": "2026-09-20T00:00:00Z"})


def test_data_client_sends_user_jwt_for_rls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer user-jwt"
        assert request.headers["apikey"] == "sb_publishable_test"
        assert request.url.params["user_id"] == "eq.user-1"
        return httpx.Response(200, json=[])

    config = SupabaseConfig("https://project.supabase.co", "sb_publishable_test")
    client = SupabaseDataClient(config, "user-jwt", transport=httpx.MockTransport(handler))

    assert client.select("transactions", user_id="eq.user-1") == []


def test_data_client_pages_through_large_history():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["range"])
        size = 1000 if len(calls) == 1 else 1
        return httpx.Response(200, json=[{"id": str(index)} for index in range(size)])

    config = SupabaseConfig("https://project.supabase.co", "sb_publishable_test")
    client = SupabaseDataClient(config, "user-jwt", transport=httpx.MockTransport(handler))

    assert len(client.select("transactions", user_id="eq.user-1")) == 1001
    assert calls == ["0-999", "1000-1999"]


class RecordingDataClient:
    def __init__(self):
        self.inserted = []

    def select(self, table, **params):
        return []

    def insert(self, table, payload, *, ignore_duplicates=False):
        self.inserted.extend(payload)
        return payload


def test_repository_forces_authenticated_owner_on_insert():
    client = RecordingDataClient()
    repo = SupabaseTransactionRepository(client, "user-uuid")

    inserted = repo.insert_many(
        [
            Transaction(
                id="transaction-1",
                date=date(2026, 9, 20),
                description="Coffee",
                amount=-4.5,
                account_name="Checking",
            )
        ]
    )

    assert inserted == 1
    assert client.inserted[0]["user_id"] == "user-uuid"


def test_schema_enables_rls_for_every_financial_table():
    sql = (
        Path(__file__).parents[1]
        / "supabase/migrations/202609200001_financebuddy.sql"
    ).read_text()
    for table in (
        "transactions",
        "budgets",
        "savings_goals",
        "category_rules",
        "app_settings",
        "plaid_items",
        "plaid_accounts",
    ):
        assert f"alter table public.{table} enable row level security" in sql
    assert "(select auth.uid()) = user_id" in sql
    assert "revoke all on public.transactions from anon" in sql
