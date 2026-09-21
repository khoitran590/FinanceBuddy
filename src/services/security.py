from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Mapping


class AuthenticationError(ValueError):
    pass


def authenticated_user_id(claims: Mapping[str, Any]) -> str:
    """Derive a stable, non-PII storage identifier from a verified OIDC identity."""
    subject = str(claims.get("sub") or "").strip()
    issuer = str(claims.get("iss") or "").strip()
    if not subject:
        raise AuthenticationError("The identity provider did not return a subject identifier.")
    return hashlib.sha256(f"{issuer}\x00{subject}".encode("utf-8")).hexdigest()


def user_database_path(claims: Mapping[str, Any], root: str | None = None) -> str:
    """Return a server-derived database path that a browser user cannot choose."""
    directory = Path(root) if root else Path(os.getenv("FINANCEBUDDY_DATA_DIR", "data")) / "users"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return str(directory / f"{authenticated_user_id(claims)}.db")


def protect_database_file(path: str) -> None:
    """Restrict a user's database to the operating-system account running the app."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def safe_display_name(claims: Mapping[str, Any]) -> str:
    return str(claims.get("name") or claims.get("preferred_username") or "Your account")
