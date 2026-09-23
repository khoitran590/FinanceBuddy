from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx


class SupabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    publishable_key: str
    public_app_url: str = "http://localhost:8501"

    @classmethod
    def from_sources(cls, secrets: Optional[Mapping[str, Any]] = None) -> "SupabaseConfig":
        nested = secrets.get("supabase", {}) if secrets and hasattr(secrets, "get") else {}
        return cls(
            url=str(os.getenv("SUPABASE_URL") or nested.get("url") or "").rstrip("/"),
            publishable_key=str(
                os.getenv("SUPABASE_PUBLISHABLE_KEY")
                or nested.get("publishable_key")
                or ""
            ),
            public_app_url=str(
                os.getenv("PUBLIC_APP_URL")
                or nested.get("public_app_url")
                or "http://localhost:8501"
            ).rstrip("/"),
        )

    @property
    def is_configured(self) -> bool:
        from src.services.production import _is_privileged_supabase_key

        return (self.url.startswith("https://") and bool(self.publishable_key)
                and not _is_privileged_supabase_key(self.publishable_key))


class SupabaseAuth:
    def __init__(self, config: SupabaseConfig, transport: httpx.BaseTransport | None = None):
        self.config = config
        self.client = httpx.Client(timeout=20, transport=transport)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict] = None,
        params: Optional[dict] = None,
        access_token: Optional[str] = None,
    ) -> dict:
        headers = {"apikey": self.config.publishable_key}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        try:
            response = self.client.request(
                method,
                f"{self.config.url}/auth/v1/{path}",
                headers=headers,
                params=params,
                json=json,
            )
        except httpx.RequestError as error:
            raise SupabaseError("Supabase authentication is temporarily unavailable.") from error
        if response.is_error:
            try:
                payload = response.json()
                code = payload.get("code") or payload.get("error_code") if isinstance(payload, dict) else None
            except ValueError:
                code = None
            messages = {
                "invalid_credentials": "Email or password is incorrect.",
                "email_not_confirmed": "Verify your email before logging in.",
                "otp_expired": "This code or link has expired. Request a new one.",
                "weak_password": "Choose a stronger password with at least 12 characters.",
            }
            message = messages.get(code, "Unable to complete sign-in or account update. Check your details and try again.")
            if response.status_code == 429:
                message = "Too many attempts. Wait a few minutes before trying again."
            raise SupabaseError(message)
        return response.json() if response.content else {}

    def sign_in(self, email: str, password: str) -> dict:
        return self._request(
            "POST", "token?grant_type=password", json={"email": email, "password": password}
        )

    def sign_up(self, email: str, password: str) -> dict:
        return self._request(
            "POST",
            "signup",
            params={"redirect_to": self.config.public_app_url},
            json={"email": email, "password": password},
        )

    def resend_signup_email(self, email: str) -> None:
        self._request(
            "POST",
            "resend",
            params={"redirect_to": self.config.public_app_url},
            json={"type": "signup", "email": email},
        )

    def verify_signup_otp(self, email: str, token: str) -> dict:
        return self._request(
            "POST", "verify", json={"email": email, "token": token, "type": "email"}
        )

    def verify_email_link(self, token_hash: str, token_type: str) -> dict:
        if token_type not in {"email", "recovery"} or not token_hash:
            raise SupabaseError("This email link is invalid. Request a new one.")
        return self._request(
            "POST", "verify", json={"token_hash": token_hash, "type": token_type}
        )

    def send_recovery_email(self, email: str) -> None:
        self._request(
            "POST",
            "recover",
            params={"redirect_to": self.config.public_app_url},
            json={"email": email},
        )

    def verify_recovery_otp(self, email: str, token: str) -> dict:
        return self._request(
            "POST", "verify", json={"email": email, "token": token, "type": "recovery"}
        )

    def update_password(self, access_token: str, password: str) -> dict:
        return self._request("PUT", "user", json={"password": password}, access_token=access_token)

    def refresh(self, refresh_token: str) -> dict:
        return self._request(
            "POST", "token?grant_type=refresh_token", json={"refresh_token": refresh_token}
        )

    def get_user(self, access_token: str) -> dict:
        payload = self._request("GET", "user", access_token=access_token)
        return payload.get("user", payload)

    def sign_out(self, access_token: str) -> None:
        self._request("POST", "logout", access_token=access_token)


def normalized_session(payload: Mapping[str, Any]) -> dict[str, Any]:
    expires_in = int(payload.get("expires_in") or 3600)
    return {
        "access_token": str(payload.get("access_token") or ""),
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": int(payload.get("expires_at") or (time.time() + expires_in)),
    }


def has_verified_email(user: Mapping[str, Any]) -> bool:
    """Require a confirmed email even if the provider's sign-in policy changes."""
    return bool(user.get("email") and user.get("email_confirmed_at"))


class SupabaseDataClient:
    """Small PostgREST client. The user's JWT makes PostgreSQL RLS the authority."""

    def __init__(
        self,
        config: SupabaseConfig,
        access_token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = f"{config.url}/rest/v1"
        self.client = httpx.Client(timeout=30, transport=transport)
        self.headers = {
            "apikey": config.publishable_key,
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        table: str,
        *,
        params: Optional[dict[str, str]] = None,
        payload: Any = None,
        prefer: Optional[str] = None,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
    ) -> list[dict]:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        if range_start is not None and range_end is not None:
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{range_start}-{range_end}"
        try:
            response = self.client.request(
                method,
                f"{self.base_url}/{table}",
                headers=headers,
                params=params,
                json=payload,
            )
        except httpx.RequestError as error:
            raise SupabaseError("Supabase data storage is temporarily unavailable.") from error
        if response.is_error:
            raise SupabaseError("Unable to complete the database request. Please try again.")
        if not response.content:
            return []
        data = response.json()
        return data if isinstance(data, list) else [data]

    def select(self, table: str, **params: str) -> list[dict]:
        page_size = 1000
        rows: list[dict] = []
        while True:
            page = self.request(
                "GET",
                table,
                params={"select": "*", **params},
                range_start=len(rows),
                range_end=len(rows) + page_size - 1,
            )
            rows.extend(page)
            if len(page) < page_size:
                return rows

    def insert(self, table: str, payload: list[dict], *, ignore_duplicates: bool = False) -> list[dict]:
        prefer = "return=representation"
        if ignore_duplicates:
            prefer += ",resolution=ignore-duplicates"
        return self.request("POST", table, payload=payload, prefer=prefer)

    def upsert(self, table: str, payload: list[dict], on_conflict: str) -> list[dict]:
        return self.request(
            "POST",
            table,
            params={"on_conflict": on_conflict},
            payload=payload,
            prefer="return=representation,resolution=merge-duplicates",
        )

    def update(self, table: str, payload: dict, **filters: str) -> list[dict]:
        return self.request(
            "PATCH", table, params=filters, payload=payload, prefer="return=representation"
        )

    def delete(self, table: str, **filters: str) -> list[dict]:
        return self.request("DELETE", table, params=filters, prefer="return=representation")

    def rpc(self, name: str, payload: dict) -> Any:
        """Call a database function once so all of its writes share a transaction."""
        if not name.startswith("fb_") or not name.replace("_", "").isalnum():
            raise ValueError("Invalid database operation.")
        result = self.request("POST", f"rpc/{name}", payload=payload)
        return result[0] if result else None
