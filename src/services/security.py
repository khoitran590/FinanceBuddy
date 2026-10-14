"""Small, shared boundaries for private session data and untrusted backups."""
import json
from collections.abc import MutableMapping
from typing import Any

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_BACKUP_RECORDS = 50_000


def clear_private_session(state: MutableMapping[str, Any]) -> None:
    # Include upload widgets, undo snapshots, passwords and bank-link tokens.
    for key in list(state):
        del state[key]


def bind_session_owner(state: MutableMapping[str, Any], user_id: str) -> None:
    if state.get("session_owner") != user_id:
        session = state.get("supabase_session")
        clear_private_session(state)
        if session:
            state["supabase_session"] = session
        state["session_owner"] = user_id


def read_backup(raw: bytes) -> dict:
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("Backup exceeds the 10 MB limit.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise ValueError("Backup must be valid UTF-8 JSON.") from error
    if not isinstance(data, dict) or data.get("version") not in (1, 2, 3):
        raise ValueError("Unsupported FinanceBuddy backup version.")
    keys = ["transactions", "budgets", "goals", "category_rules"]
    if "statement_import_rows" in data:
        keys.append("statement_import_rows")
    if data.get("version") == 3:
        keys.extend(["accounts", "settings", "balance_snapshots"])
    for key in keys:
        rows = data.get(key)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError(f"Backup must contain a {key} list of records.")
        if len(rows) > MAX_BACKUP_RECORDS:
            raise ValueError(f"Backup contains too many {key} records.")
    return data
