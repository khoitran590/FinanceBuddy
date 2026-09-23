from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class PlaidItem:
    item_id: str
    access_token: str
    institution_name: str
    cursor: Optional[str]
    status: str
    last_synced_at: Optional[str]
    error_message: Optional[str]


def account_balances(account: dict) -> dict:
    """Flatten Plaid's nested balances into the stored account columns."""
    balances = account.get("balances") or {}

    def number(key: str) -> Optional[float]:
        value = balances.get(key)
        return float(value) if value is not None else None

    return {
        "current_balance": number("current"),
        "available_balance": number("available"),
        "credit_limit": number("limit"),
        "iso_currency_code": balances.get("iso_currency_code"),
    }


class PlaidRepository:
    """Persistence for Plaid Items and account metadata.

    Access tokens passed to this repository must already be encrypted.
    """

    def __init__(self, db_path: str = "data/finance.db") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with self._get_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plaid_items (
                    item_id TEXT PRIMARY KEY,
                    encrypted_access_token TEXT NOT NULL,
                    institution_name TEXT NOT NULL DEFAULT 'Connected institution',
                    cursor TEXT,
                    status TEXT NOT NULL DEFAULT 'connected',
                    last_synced_at TEXT,
                    error_message TEXT
                );

                CREATE TABLE IF NOT EXISTS plaid_accounts (
                    account_id TEXT PRIMARY KEY,
                    item_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    official_name TEXT,
                    type TEXT NOT NULL,
                    subtype TEXT,
                    mask TEXT,
                    FOREIGN KEY(item_id) REFERENCES plaid_items(item_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS account_balances (
                    account_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    as_of TEXT NOT NULL,
                    type TEXT NOT NULL,
                    current_balance REAL NOT NULL,
                    available_balance REAL,
                    credit_limit REAL,
                    PRIMARY KEY(account_id, as_of),
                    FOREIGN KEY(item_id) REFERENCES plaid_items(item_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(plaid_accounts)").fetchall()
            }
            for column, kind in (
                ("current_balance", "REAL"),
                ("available_balance", "REAL"),
                ("credit_limit", "REAL"),
                ("iso_currency_code", "TEXT"),
                ("balances_updated_at", "TEXT"),
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE plaid_accounts ADD COLUMN {column} {kind}")

    def save_item(
        self, item_id: str, encrypted_access_token: str, institution_name: str
    ) -> None:
        with self._get_connection() as connection:
            connection.execute(
                """
                INSERT INTO plaid_items
                    (item_id, encrypted_access_token, institution_name, status, error_message)
                VALUES (?, ?, ?, 'connected', NULL)
                ON CONFLICT(item_id) DO UPDATE SET
                    encrypted_access_token = excluded.encrypted_access_token,
                    institution_name = excluded.institution_name,
                    status = 'connected',
                    error_message = NULL
                """,
                (item_id, encrypted_access_token, institution_name),
            )

    def save_accounts(self, item_id: str, accounts: list[dict]) -> None:
        now = datetime.now(timezone.utc)
        rows = [(account, account_balances(account)) for account in accounts]
        with self._get_connection() as connection:
            connection.execute("DELETE FROM plaid_accounts WHERE item_id = ?", (item_id,))
            connection.executemany(
                """
                INSERT INTO plaid_accounts
                    (account_id, item_id, name, official_name, type, subtype, mask,
                     current_balance, available_balance, credit_limit, iso_currency_code,
                     balances_updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        account["account_id"],
                        item_id,
                        account.get("name") or "Connected account",
                        account.get("official_name"),
                        str(account.get("type") or "depository"),
                        str(account.get("subtype") or ""),
                        account.get("mask"),
                        balances["current_balance"],
                        balances["available_balance"],
                        balances["credit_limit"],
                        balances["iso_currency_code"],
                        now.isoformat(timespec="seconds") if balances["current_balance"] is not None else None,
                    )
                    for account, balances in rows
                ],
            )
            connection.executemany(
                """
                INSERT INTO account_balances
                    (account_id, item_id, as_of, type, current_balance, available_balance, credit_limit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, as_of) DO UPDATE SET
                    type = excluded.type,
                    current_balance = excluded.current_balance,
                    available_balance = excluded.available_balance,
                    credit_limit = excluded.credit_limit
                """,
                [
                    (
                        account["account_id"],
                        item_id,
                        now.date().isoformat(),
                        str(account.get("type") or "depository"),
                        balances["current_balance"],
                        balances["available_balance"],
                        balances["credit_limit"],
                    )
                    for account, balances in rows
                    if balances["current_balance"] is not None
                ],
            )

    def get_items(self) -> list[PlaidItem]:
        with self._get_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM plaid_items ORDER BY institution_name, item_id"
            ).fetchall()
        return [
            PlaidItem(
                item_id=row["item_id"],
                access_token=row["encrypted_access_token"],
                institution_name=row["institution_name"],
                cursor=row["cursor"],
                status=row["status"],
                last_synced_at=row["last_synced_at"],
                error_message=row["error_message"],
            )
            for row in rows
        ]

    def get_item(self, item_id: str) -> Optional[PlaidItem]:
        return next((item for item in self.get_items() if item.item_id == item_id), None)

    def get_accounts(self, item_id: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM plaid_accounts"
        params: tuple[str, ...] = ()
        if item_id:
            query += " WHERE item_id = ?"
            params = (item_id,)
        query += " ORDER BY name"
        with self._get_connection() as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def get_balance_history(self) -> list[dict]:
        with self._get_connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM account_balances ORDER BY as_of, account_id"
                ).fetchall()
            ]

    def update_sync(self, item_id: str, cursor: str, synced_at: str) -> None:
        with self._get_connection() as connection:
            connection.execute(
                """
                UPDATE plaid_items
                SET cursor = ?, last_synced_at = ?, status = 'connected', error_message = NULL
                WHERE item_id = ?
                """,
                (cursor, synced_at, item_id),
            )

    def set_error(self, item_id: str, message: str) -> None:
        with self._get_connection() as connection:
            connection.execute(
                "UPDATE plaid_items SET status = 'error', error_message = ? WHERE item_id = ?",
                (message, item_id),
            )

    def delete_item(self, item_id: str) -> None:
        with self._get_connection() as connection:
            connection.execute("DELETE FROM plaid_items WHERE item_id = ?", (item_id,))
