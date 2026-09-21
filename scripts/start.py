from __future__ import annotations

import json
import os
from pathlib import Path


def _quoted(value: str) -> str:
    return json.dumps(value)


def write_runtime_secrets() -> None:
    required = {
        "SUPABASE_URL": os.getenv("SUPABASE_URL", ""),
        "SUPABASE_PUBLISHABLE_KEY": os.getenv("SUPABASE_PUBLISHABLE_KEY", ""),
        "PUBLIC_APP_URL": os.getenv("PUBLIC_APP_URL", ""),
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
        "[supabase]",
        f"url = {_quoted(required['SUPABASE_URL'])}",
        f"publishable_key = {_quoted(required['SUPABASE_PUBLISHABLE_KEY'])}",
        f"public_app_url = {_quoted(required['PUBLIC_APP_URL'])}",
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
