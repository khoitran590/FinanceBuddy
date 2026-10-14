import json
import math
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from src.services.security import read_backup

from src.domain.models import Budget, CategoryRule, SavingsGoal, Transaction, TransactionAllocation, StatementImportRow
from src.services.imports import plan_statement_import, normalize_text
from src.domain.allocations import project_transactions, recover_legacy_splits


class TransactionRepository:
    """SQLite repository with no dependency on the presentation layer."""

    def __init__(self, db_path: str = "data/finance.db") -> None:
        self.db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.create_function("fb_import_normalize", 1, normalize_text, deterministic=True)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_db(self) -> None:
        with self._get_connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY,
                    date TEXT NOT NULL,
                    description TEXT NOT NULL,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL DEFAULT 'Uncategorized',
                    account_name TEXT NOT NULL,
                    account_type TEXT NOT NULL DEFAULT 'Checking'
                );

                CREATE TABLE IF NOT EXISTS budgets (
                    category TEXT PRIMARY KEY,
                    monthly_limit REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS savings_goals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    target_amount REAL NOT NULL,
                    current_amount REAL NOT NULL DEFAULT 0,
                    target_date TEXT
                );

                CREATE TABLE IF NOT EXISTS category_rules (
                    keyword TEXT PRIMARY KEY COLLATE NOCASE,
                    category TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(transactions)").fetchall()
            }
            if "account_type" not in columns:
                connection.execute(
                    "ALTER TABLE transactions ADD COLUMN account_type TEXT NOT NULL DEFAULT 'Checking'"
                )

            for column in ("plaid_item_id", "plaid_account_id", "statement_file_id", "statement_reference"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE transactions ADD COLUMN {column} TEXT")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS transaction_allocations (
                    id TEXT PRIMARY KEY,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    amount REAL NOT NULL,
                    category TEXT NOT NULL
                );
            """)
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='plaid_items'").fetchone():
                item_columns = {row[1] for row in connection.execute("PRAGMA table_info(plaid_items)")}
                for column, kind in (("sync_generation", "INTEGER NOT NULL DEFAULT 0"),
                                     ("needs_reconciliation", "INTEGER NOT NULL DEFAULT 0")):
                    if column not in item_columns:
                        connection.execute(f"ALTER TABLE plaid_items ADD COLUMN {column} {kind}")
            if "statement_match_bank" not in columns:
                connection.execute("ALTER TABLE transactions ADD COLUMN statement_match_bank INTEGER NOT NULL DEFAULT 0")
            if "statement_row" not in columns:
                connection.execute("ALTER TABLE transactions ADD COLUMN statement_row INTEGER")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS statement_import_rows (
                    file_id TEXT NOT NULL, row_number INTEGER NOT NULL,
                    transaction_id TEXT NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
                    PRIMARY KEY(file_id,row_number)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS transactions_file_row_idx
                    ON transactions(statement_file_id,statement_row) WHERE statement_file_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS transactions_reference_idx
                    ON transactions(fb_import_normalize(account_name),fb_import_normalize(account_type),statement_reference)
                    WHERE statement_reference IS NOT NULL;
            """)
            self._recover_legacy_splits(connection)

    @staticmethod
    def _recover_legacy_splits(connection):
        rows = connection.execute("SELECT * FROM transactions").fetchall()
        allocations = {}
        for row in connection.execute("SELECT * FROM transaction_allocations"):
            allocations.setdefault(row["transaction_id"], []).append(dict(row))
        original = [Transaction(**dict(row), allocations=allocations.get(row["id"], [])) for row in rows]
        recovered = recover_legacy_splits(original)
        consumed = {item.id for item in original} - {item.id for item in recovered}
        if not consumed:
            return
        # Recover before export, so a legacy SQLite file produces a canonical v2
        # backup. Only complete pairs are consumed; partial records stay intact.
        for item in recovered:
            connection.execute("""INSERT OR IGNORE INTO transactions
                (id,date,description,amount,category,account_name,account_type,plaid_item_id,plaid_account_id)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (item.id,item.date.isoformat(),item.description,item.amount,item.category,
                 item.account_name,item.account_type,item.plaid_item_id,item.plaid_account_id))
        for identifier in consumed:
            connection.execute("DELETE FROM transactions WHERE id=?", (identifier,))
        for item in recovered:
            if item.allocations:
                connection.execute("DELETE FROM transaction_allocations WHERE transaction_id=?", (item.id,))
                connection.executemany("INSERT INTO transaction_allocations VALUES (?,?,?,?)", [
                    (str(allocation.id),item.id,allocation.amount,allocation.category) for allocation in item.allocations
                ])

    def get_canonical_all(self) -> List[Transaction]:
        with self._get_connection() as connection:
            rows = connection.execute("SELECT * FROM transactions ORDER BY date DESC, id DESC").fetchall()
            allocations = connection.execute("SELECT * FROM transaction_allocations ORDER BY id").fetchall()
        by_id = {}
        for allocation in allocations:
            by_id.setdefault(allocation["transaction_id"], []).append({
                "id": allocation["id"], "amount": allocation["amount"], "category": allocation["category"],
            })
        return [Transaction(**dict(row), allocations=by_id.get(row["id"], [])) for row in rows]

    def get_all(self) -> List[Transaction]:
        return project_transactions(self.get_canonical_all())

    def get_existing_ids(self) -> set[str]:
        with self._get_connection() as connection:
            rows = connection.execute("SELECT id FROM transactions").fetchall()
        return {row["id"] for row in rows}

    def insert_many(self, transactions: Iterable[Transaction]) -> int:
        transactions = list(transactions)
        if transactions and all(item.statement_file_id for item in transactions):
            return self.append_statement(transactions)["inserted"]
        payload = [
            (
                transaction.id,
                transaction.date.isoformat(),
                transaction.description,
                transaction.amount,
                transaction.category,
                transaction.account_name,
                transaction.account_type,
                transaction.plaid_item_id,
                transaction.plaid_account_id,
                transaction.statement_file_id, transaction.statement_row, transaction.statement_reference, transaction.statement_match_bank,
            )
            for transaction in transactions
        ]
        if not payload:
            return 0

        with self._get_connection() as connection:
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (id, date, description, amount, category, account_name, account_type, plaid_item_id, plaid_account_id, statement_file_id, statement_row, statement_reference, statement_match_bank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return cursor.rowcount

    def get_import_rows(self, file_id=None):
        with self._get_connection() as connection:
            if file_id:
                rows = connection.execute("SELECT * FROM statement_import_rows WHERE file_id=?", (file_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM statement_import_rows").fetchall()
        return [dict(row) for row in rows]

    def preview_statement_import(self, transactions, *, keep_matching=False):
        return plan_statement_import(transactions, self.get_canonical_all(), self.get_import_rows(),
                                     keep_matching=keep_matching)

    def is_connected_account(self, account_name):
        with self._get_connection() as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='plaid_items'").fetchone():
                return False
            return bool(connection.execute("""SELECT 1 FROM plaid_items i WHERE
                EXISTS(SELECT 1 FROM transactions t WHERE t.plaid_item_id=i.item_id AND fb_import_normalize(t.account_name)=fb_import_normalize(?))
                OR EXISTS(SELECT 1 FROM plaid_accounts a WHERE a.item_id=i.item_id AND
                    fb_import_normalize(coalesce(nullif(a.official_name,''),a.name) || CASE WHEN a.mask IS NULL OR a.mask='' THEN '' ELSE ' ••••'||a.mask END)=fb_import_normalize(?))
                LIMIT 1""", (account_name, account_name)).fetchone())

    def append_statement(self, transactions, *, keep_matching=False):
        items = list(transactions)
        with self._get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Read the planning snapshot and commit its exact inserted IDs under
            # one writer lock, so concurrent imports cannot create extra copies.
            existing = [Transaction(**dict(row)) for row in connection.execute("SELECT * FROM transactions")]
            mappings = [dict(row) for row in connection.execute("SELECT * FROM statement_import_rows")]
            plan = plan_statement_import(items, existing, mappings, keep_matching=keep_matching)
            plan["inserted_rows"] = [item.model_copy(update={"statement_match_bank": not keep_matching})
                                     for item in plan["inserted_rows"]]
            for item in plan["inserted_rows"]:
                connection.execute("""INSERT INTO transactions
                    (id,date,description,amount,category,account_name,account_type,plaid_item_id,plaid_account_id,
                     statement_file_id,statement_row,statement_reference,statement_match_bank) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.id,item.date.isoformat(),item.description,item.amount,item.category,item.account_name,
                     item.account_type,item.plaid_item_id,item.plaid_account_id,item.statement_file_id,
                     item.statement_row,item.statement_reference,item.statement_match_bank))
            for identifier, reference in plan["references_added"]:
                connection.execute("UPDATE transactions SET statement_reference=? WHERE id=?", (reference, identifier))
            for identifier, file_id, row in plan["provenance_added"]:
                connection.execute("UPDATE transactions SET statement_file_id=?,statement_row=?,statement_match_bank=1 WHERE id=?",
                                   (file_id, row, identifier))
            connection.executemany("INSERT OR REPLACE INTO statement_import_rows VALUES (?,?,?)", [
                (row["file_id"],row["row_number"],row["transaction_id"]) for row in plan["mappings"]
            ])
            return plan

    def undo_append(self, transactions):
        items = list(transactions)
        with self._get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in items:
                actual = connection.execute("SELECT * FROM transactions WHERE id=?", (item.id,)).fetchone()
                split = connection.execute("SELECT 1 FROM transaction_allocations WHERE transaction_id=?", (item.id,)).fetchone()
                if actual is None or split:
                    raise ValueError("Imported transactions changed; cannot undo.")
                saved = Transaction(**dict(actual))
                if saved.model_dump(mode="json") != item.model_dump(mode="json"):
                    raise ValueError("Imported transactions changed; cannot undo.")
            connection.executemany("DELETE FROM transactions WHERE id=?", [(item.id,) for item in items])
        return len(items)

    def upsert_many(self, transactions: Iterable[Transaction]) -> int:
        """Insert synced rows and refresh mutable fields without losing user categories."""
        payload = [
            (
                transaction.id,
                transaction.date.isoformat(),
                transaction.description,
                transaction.amount,
                transaction.category,
                transaction.account_name,
                transaction.account_type,
                transaction.plaid_item_id,
                transaction.plaid_account_id,
                transaction.statement_file_id, transaction.statement_row, transaction.statement_reference, transaction.statement_match_bank,
            )
            for transaction in transactions
        ]
        if not payload:
            return 0

        with self._get_connection() as connection:
            connection.executemany(
                """
                INSERT INTO transactions
                    (id, date, description, amount, category, account_name, account_type, plaid_item_id, plaid_account_id, statement_file_id, statement_row, statement_reference, statement_match_bank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    date = excluded.date,
                    description = excluded.description,
                    amount = excluded.amount,
                    category = CASE
                        WHEN transactions.category = 'Uncategorized' THEN excluded.category
                        ELSE transactions.category
                    END,
                    account_name = excluded.account_name,
                    account_type = excluded.account_type,
                    plaid_item_id = excluded.plaid_item_id,
                    plaid_account_id = excluded.plaid_account_id
                """,
                payload,
            )
        return len(payload)

    def delete_many(self, transaction_ids: Iterable[str]) -> int:
        ids = list(transaction_ids)
        if not ids:
            return 0
        with self._get_connection() as connection:
            cursor = connection.executemany(
                "DELETE FROM transactions WHERE id = ?", [(item,) for item in ids]
            )
            return cursor.rowcount

    def count_existing_ids(self, transactions: Iterable[Transaction]) -> int:
        existing = self.get_existing_ids()
        return sum(transaction.id in existing for transaction in transactions)

    def replace_account(self, account_name: str, transactions: Iterable[Transaction]) -> int:
        """Replace one account without disturbing the user's other accounts."""
        payload = list(transactions)
        if any(item.account_name != account_name for item in payload):
            raise ValueError("Statement account does not match the selected account.")
        if payload and any(not item.id.startswith("plaid:") for item in payload) and self.is_connected_account(account_name):
            raise ValueError("Append statements for a connected bank, or disconnect it before replacing history.")
        with self._get_connection() as connection:
            self._invalidate_sync(connection, account_name)
            connection.execute(
                "DELETE FROM transactions WHERE account_name = ?", (account_name,)
            )
            if not payload:
                return 0
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (id, date, description, amount, category, account_name, account_type, plaid_item_id, plaid_account_id, statement_file_id, statement_row, statement_reference, statement_match_bank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        transaction.id,
                        transaction.date.isoformat(),
                        transaction.description,
                        transaction.amount,
                        transaction.category,
                        transaction.account_name,
                        transaction.account_type,
                        transaction.plaid_item_id,
                        transaction.plaid_account_id,
                        transaction.statement_file_id, transaction.statement_row, transaction.statement_reference, transaction.statement_match_bank,
                    )
                    for transaction in payload
                ],
            )
            return cursor.rowcount

    def update_category(self, transaction_id: str, category: str) -> None:
        if transaction_id.startswith("allocation:"):
            with self._get_connection() as connection:
                connection.execute("UPDATE transaction_allocations SET category=? WHERE id=?",
                                   (category, transaction_id.removeprefix("allocation:")))
            return
        with self._get_connection() as connection:
            connection.execute(
                "UPDATE transactions SET category = ? WHERE id = ?",
                (category, transaction_id),
            )

    def apply_category_rule(self, keyword: str, category: str) -> int:
        with self._get_connection() as connection:
            cursor = connection.execute("UPDATE transactions SET category=? WHERE instr(lower(description),?)>0",
                                        (category, keyword.strip().lower()))
            connection.execute("""UPDATE transaction_allocations SET category=? WHERE transaction_id IN (
                SELECT id FROM transactions WHERE instr(lower(description),?)>0)""",
                (category, keyword.strip().lower()))
            return cursor.rowcount

    def split_transaction(
        self, transaction_id: str, first_category: str, first_amount: float,
        second_category: str, second_amount: float,
    ) -> None:
        if not all(math.isfinite(value) and value > 0 for value in (first_amount, second_amount)):
            raise ValueError("Split amounts must be positive and finite.")
        with self._get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM transactions WHERE id=?", (transaction_id,)).fetchone()
            if row is None:
                raise ValueError("Transaction no longer exists.")
            if abs(first_amount + second_amount - abs(row["amount"])) > 0.005:
                raise ValueError("Split amounts must equal the original transaction amount.")
            sign = -1 if row["amount"] < 0 else 1
            allocations = [TransactionAllocation(category=category, amount=sign * amount)
                           for category, amount in ((first_category, first_amount), (second_category, second_amount))]
            connection.execute("DELETE FROM transaction_allocations WHERE transaction_id=?", (transaction_id,))
            connection.executemany("INSERT INTO transaction_allocations VALUES (?,?,?,?)", [
                (str(item.id), transaction_id, item.amount, item.category) for item in allocations
            ])

    def get_budgets(self) -> List[Budget]:
        with self._get_connection() as connection:
            rows = connection.execute(
                "SELECT category, monthly_limit FROM budgets ORDER BY category"
            ).fetchall()
        return [Budget(category=row["category"], monthly_limit=row["monthly_limit"]) for row in rows]

    def upsert_budget(self, budget: Budget) -> None:
        with self._get_connection() as connection:
            connection.execute(
                """
                INSERT INTO budgets (category, monthly_limit) VALUES (?, ?)
                ON CONFLICT(category) DO UPDATE SET monthly_limit = excluded.monthly_limit
                """,
                (budget.category, budget.monthly_limit),
            )

    def delete_budget(self, category: str) -> None:
        with self._get_connection() as connection:
            connection.execute("DELETE FROM budgets WHERE category = ?", (category,))

    def get_goals(self) -> List[SavingsGoal]:
        with self._get_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM savings_goals ORDER BY target_date IS NULL, target_date, id"
            ).fetchall()
        return [
            SavingsGoal(
                id=row["id"],
                name=row["name"],
                target_amount=row["target_amount"],
                current_amount=row["current_amount"],
                target_date=date.fromisoformat(row["target_date"]) if row["target_date"] else None,
            )
            for row in rows
        ]

    def save_goal(self, goal: SavingsGoal) -> int:
        with self._get_connection() as connection:
            if goal.id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO savings_goals (name, target_amount, current_amount, target_date)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        goal.name,
                        goal.target_amount,
                        goal.current_amount,
                        goal.target_date.isoformat() if goal.target_date else None,
                    ),
                )
                return int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE savings_goals
                SET name = ?, target_amount = ?, current_amount = ?, target_date = ?
                WHERE id = ?
                """,
                (
                    goal.name,
                    goal.target_amount,
                    goal.current_amount,
                    goal.target_date.isoformat() if goal.target_date else None,
                    goal.id,
                ),
            )
            return goal.id

    def delete_goal(self, goal_id: int) -> None:
        with self._get_connection() as connection:
            connection.execute("DELETE FROM savings_goals WHERE id = ?", (goal_id,))

    def get_category_rules(self) -> List[CategoryRule]:
        with self._get_connection() as connection:
            rows = connection.execute(
                "SELECT keyword, category FROM category_rules ORDER BY keyword"
            ).fetchall()
        return [CategoryRule(keyword=row["keyword"], category=row["category"]) for row in rows]

    def upsert_category_rule(self, rule: CategoryRule) -> None:
        with self._get_connection() as connection:
            connection.execute(
                """
                INSERT INTO category_rules (keyword, category) VALUES (?, ?)
                ON CONFLICT(keyword) DO UPDATE SET category = excluded.category
                """,
                (rule.keyword.strip().lower(), rule.category),
            )

    def delete_category_rule(self, keyword: str) -> None:
        with self._get_connection() as connection:
            connection.execute("DELETE FROM category_rules WHERE keyword = ?", (keyword,))

    def delete_account(self, account_name: str) -> int:
        with self._get_connection() as connection:
            self._invalidate_sync(connection, account_name)
            cursor = connection.execute(
                "DELETE FROM transactions WHERE account_name = ?", (account_name,)
            )
            return cursor.rowcount

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._get_connection() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._get_connection() as connection:
            connection.execute(
                """
                INSERT INTO app_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def export_backup(self) -> str:
        payload = {
            "version": 2,
            "transactions": [item.model_dump(mode="json") for item in self.get_canonical_all()],
            "budgets": [item.model_dump(mode="json") for item in self.get_budgets()],
            "goals": [item.model_dump(mode="json") for item in self.get_goals()],
            "category_rules": [
                item.model_dump(mode="json") for item in self.get_category_rules()
            ],
            "statement_import_rows": self.get_import_rows(),
        }
        return json.dumps(payload, indent=2)

    def restore_backup(self, raw_json: bytes) -> dict:
        data = read_backup(raw_json)
        transactions = [Transaction.model_validate(item) for item in data.get("transactions", [])]
        if data["version"] == 1:
            transactions = recover_legacy_splits(transactions)
        budgets = [Budget.model_validate(item) for item in data.get("budgets", [])]
        goals = [SavingsGoal.model_validate(item) for item in data.get("goals", [])]
        rules = [CategoryRule.model_validate(item) for item in data.get("category_rules", [])]
        import_rows = [StatementImportRow.model_validate(item) for item in data.get("statement_import_rows", [])]
        with self._get_connection() as connection:
            transactions = self._prepare_transactions(connection, transactions)
            connection.execute("DELETE FROM transactions")
            connection.execute("DELETE FROM budgets")
            connection.execute("DELETE FROM savings_goals")
            connection.execute("DELETE FROM category_rules")
            connection.executemany(
                """
                INSERT INTO transactions
                    (id, date, description, amount, category, account_name, account_type, plaid_item_id, plaid_account_id, statement_file_id, statement_row, statement_reference, statement_match_bank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.date.isoformat(),
                        item.description,
                        item.amount,
                        item.category,
                        item.account_name,
                        item.account_type,
                        item.plaid_item_id,
                        item.plaid_account_id,
                        item.statement_file_id, item.statement_row, item.statement_reference, item.statement_match_bank,
                    )
                    for item in transactions
                ],
            )
            connection.executemany("INSERT INTO transaction_allocations VALUES (?,?,?,?)", [
                (str(allocation.id), item.id, allocation.amount, allocation.category)
                for item in transactions for allocation in item.allocations
            ])
            connection.executemany("INSERT INTO statement_import_rows VALUES (?,?,?)", [
                (row.file_id,row.row_number,row.transaction_id) for row in import_rows
            ])
            self._invalidate_sync(connection)
            connection.executemany(
                "INSERT INTO budgets (category, monthly_limit) VALUES (?, ?)",
                [(item.category, item.monthly_limit) for item in budgets],
            )
            connection.executemany(
                """
                INSERT INTO savings_goals (id, name, target_amount, current_amount, target_date)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.name,
                        item.target_amount,
                        item.current_amount,
                        item.target_date.isoformat() if item.target_date else None,
                    )
                    for item in goals
                ],
            )
            connection.executemany(
                "INSERT INTO category_rules (keyword, category) VALUES (?, ?)",
                [(item.keyword, item.category) for item in rules],
            )
        return {
            "transactions": len(transactions),
            "budgets": len(budgets),
            "goals": len(goals),
            "category_rules": len(rules),
        }

    def replace_all(self, transactions: Iterable[Transaction]) -> int:
        """Replace the complete transaction history in one SQLite transaction."""
        payload = [
            (
                transaction.id,
                transaction.date.isoformat(),
                transaction.description,
                transaction.amount,
                transaction.category,
                transaction.account_name,
                transaction.account_type,
                transaction.plaid_item_id,
                transaction.plaid_account_id,
                transaction.statement_file_id, transaction.statement_row, transaction.statement_reference, transaction.statement_match_bank,
            )
            for transaction in transactions
        ]

        with self._get_connection() as connection:
            self._invalidate_sync(connection, None)
            connection.execute("DELETE FROM transactions")
            if not payload:
                return 0
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (id, date, description, amount, category, account_name, account_type, plaid_item_id, plaid_account_id, statement_file_id, statement_row, statement_reference, statement_match_bank)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return cursor.rowcount

    @staticmethod
    def _invalidate_sync(connection, account_name=None):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plaid_items'").fetchone()
        if not exists:
            return
        if account_name is None:
            connection.execute("UPDATE plaid_items SET cursor=NULL, sync_generation=sync_generation+1, needs_reconciliation=1")
        else:
            connection.execute("""UPDATE plaid_items SET cursor=NULL,
                sync_generation=sync_generation+1, needs_reconciliation=1
                WHERE item_id IN (SELECT plaid_item_id FROM transactions WHERE account_name=?)
                OR item_id IN (SELECT item_id FROM plaid_accounts WHERE coalesce(nullif(official_name,''),name) ||
                    CASE WHEN mask IS NULL OR mask='' THEN '' ELSE ' ••••'||mask END=?)""",
                (account_name, account_name))

    def apply_plaid_sync(self, item_id, expected_cursor, next_cursor, upserts, removed_ids, synced_at,
                         *, expected_generation=0, reconcile=False):
        items = list(upserts)
        removed_ids = set(removed_ids)
        with self._get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute("SELECT * FROM plaid_items WHERE item_id=?", (item_id,)).fetchone()
            if (state is None or state["cursor"] != expected_cursor
                    or state["sync_generation"] != expected_generation):
                raise ValueError("Bank cursor changed; retry sync.")
            if bool(state["needs_reconciliation"]) != reconcile:
                raise ValueError("Bank reconciliation state changed; retry sync.")
            reconciled = 0
            for item in items:
                imported = None
                if item.id not in removed_ids and not connection.execute("SELECT 1 FROM transactions WHERE id=?", (item.id,)).fetchone():
                    imported = connection.execute("""SELECT * FROM transactions
                        WHERE id NOT LIKE 'plaid:%' AND plaid_item_id IS NULL AND statement_match_bank=1 AND date=? AND amount=?
                        AND fb_import_normalize(description)=? AND fb_import_normalize(account_name)=? AND fb_import_normalize(account_type)=?
                        ORDER BY id LIMIT 1""", (item.date.isoformat(),item.amount,normalize_text(item.description),
                                              normalize_text(item.account_name),normalize_text(item.account_type))).fetchone()
                connection.execute("""INSERT INTO transactions
                    (id,date,description,amount,category,account_name,account_type,plaid_item_id,plaid_account_id)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    date=excluded.date,description=excluded.description,amount=excluded.amount,
                    category=CASE WHEN transactions.category='Uncategorized' THEN excluded.category ELSE transactions.category END,
                    account_name=excluded.account_name,account_type=excluded.account_type,
                    plaid_item_id=excluded.plaid_item_id,plaid_account_id=excluded.plaid_account_id""",
                    (item.id,item.date.isoformat(),item.description,item.amount,item.category,
                     item.account_name,item.account_type,item_id,item.plaid_account_id))
                if imported:
                    connection.execute("UPDATE transactions SET statement_file_id=NULL,statement_row=NULL,statement_reference=NULL WHERE id=?", (imported["id"],))
                    connection.execute("""UPDATE transactions SET
                        category=CASE WHEN ?='Uncategorized' THEN category ELSE ? END,
                        statement_file_id=?,statement_row=?,statement_reference=?,statement_match_bank=1 WHERE id=?""",
                        (imported["category"],imported["category"],imported["statement_file_id"],imported["statement_row"],
                         imported["statement_reference"],item.id))
                    connection.execute("UPDATE transaction_allocations SET transaction_id=? WHERE transaction_id=?", (item.id,imported["id"]))
                    connection.execute("UPDATE statement_import_rows SET transaction_id=? WHERE transaction_id=?", (item.id,imported["id"]))
                    connection.execute("DELETE FROM transactions WHERE id=?", (imported["id"],))
                    reconciled += 1
            removed = 0
            for identifier in set(removed_ids):
                removed += connection.execute("DELETE FROM transactions WHERE id=? AND plaid_item_id=?",
                                              (identifier,item_id)).rowcount
            if reconcile:
                ids = {item.id for item in items} - set(removed_ids)
                stale = connection.execute("SELECT id FROM transactions WHERE plaid_item_id=?", (item_id,)).fetchall()
                for row in stale:
                    if row["id"] not in ids:
                        removed += connection.execute("DELETE FROM transactions WHERE id=?", (row["id"],)).rowcount
            connection.execute("""UPDATE plaid_items SET cursor=?,last_synced_at=?,status='connected',
                error_message=NULL,needs_reconciliation=0 WHERE item_id=?""", (next_cursor,synced_at,item_id))
            return {"upserted": len(items), "removed": removed, "reconciled_imports": reconciled}

    @staticmethod
    def _prepare_transactions(connection, transactions):
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='plaid_items'").fetchone():
            return transactions
        has_items = connection.execute("SELECT 1 FROM plaid_items LIMIT 1").fetchone()
        result = []
        for item in transactions:
            if item.id.startswith('plaid:') and item.plaid_item_id is None:
                existing = connection.execute("SELECT plaid_item_id,plaid_account_id FROM transactions WHERE id=?",
                                              (item.id,)).fetchone()
                source = existing if existing and existing['plaid_item_id'] else None
                if source is None:
                    matches = connection.execute("""SELECT item_id AS plaid_item_id,account_id AS plaid_account_id
                        FROM plaid_accounts WHERE coalesce(nullif(official_name,''),name) ||
                        CASE WHEN mask IS NULL OR mask='' THEN '' ELSE ' ••••'||mask END=?""", (item.account_name,)).fetchall()
                    source = matches[0] if len(matches) == 1 else None
                if source:
                    item = item.model_copy(update=dict(source))
                elif has_items:
                    raise ValueError("Bank history must be matched before restore; sync connections and retry.")
            result.append(item)
        return result
