from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx

from src.domain.models import Transaction
from src.repositories.supabase_repo import SupabaseTransactionRepository
from src.services.supabase import (
    SupabaseAuth,
    SupabaseConfig,
    SupabaseDataClient,
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


def test_data_client_sends_user_jwt_for_rls():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer user-jwt"
        assert request.headers["apikey"] == "sb_publishable_test"
        assert request.url.params["user_id"] == "eq.user-1"
        return httpx.Response(200, json=[])

    config = SupabaseConfig("https://project.supabase.co", "sb_publishable_test")
    client = SupabaseDataClient(config, "user-jwt", transport=httpx.MockTransport(handler))

    assert client.select("transactions", user_id="eq.user-1") == []


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
