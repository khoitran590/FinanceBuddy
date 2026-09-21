from __future__ import annotations

import json
import os
from pathlib import Path


def _quoted(value: str) -> str:
    return json.dumps(value)


def write_runtime_secrets() -> None:
    required = {
        "AUTH_REDIRECT_URI": os.getenv("AUTH_REDIRECT_URI", ""),
        "AUTH_COOKIE_SECRET": os.getenv("AUTH_COOKIE_SECRET", ""),
        "AUTH_CLIENT_ID": os.getenv("AUTH_CLIENT_ID", ""),
        "AUTH_CLIENT_SECRET": os.getenv("AUTH_CLIENT_SECRET", ""),
        "AUTH_SERVER_METADATA_URL": os.getenv("AUTH_SERVER_METADATA_URL", ""),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    plaid = {
        "client_id": os.getenv("PLAID_CLIENT_ID", ""),
        "secret": os.getenv("PLAID_SECRET", ""),
        "environment": os.getenv("PLAID_ENV", "production"),
        "country_codes": os.getenv("PLAID_COUNTRY_CODES", "US"),
        "redirect_uri": os.getenv("PLAID_REDIRECT_URI", ""),
        "webhook_url": os.getenv("PLAID_WEBHOOK_URL", ""),
        "token_encryption_key": os.getenv("PLAID_TOKEN_ENCRYPTION_KEY", ""),
    }
    lines = [
        "[auth]",
        f"redirect_uri = {_quoted(required['AUTH_REDIRECT_URI'])}",
        f"cookie_secret = {_quoted(required['AUTH_COOKIE_SECRET'])}",
        f"client_id = {_quoted(required['AUTH_CLIENT_ID'])}",
        f"client_secret = {_quoted(required['AUTH_CLIENT_SECRET'])}",
        f"server_metadata_url = {_quoted(required['AUTH_SERVER_METADATA_URL'])}",
        "",
        "[plaid]",
    ]
    for key, value in plaid.items():
        if value:
            lines.append(f"{key} = {_quoted(value)}")

    target = Path(".streamlit/secrets.toml")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)


if __name__ == "__main__":
    write_runtime_secrets()
    port = os.getenv("PORT", "8501")
    os.execvp(
        "streamlit",
        ["streamlit", "run", "app.py", "--server.address=0.0.0.0", f"--server.port={port}"],
    )
