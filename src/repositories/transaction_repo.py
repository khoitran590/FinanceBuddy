import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable, List, Optional

from src.services.security import read_backup

from src.domain.models import Budget, CategoryRule, SavingsGoal, Transaction


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

    def get_all(self) -> List[Transaction]:
        with self._get_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM transactions ORDER BY date DESC, id DESC"
            ).fetchall()

        return [
            Transaction(
                id=row["id"],
                date=date.fromisoformat(row["date"]),
                description=row["description"],
                amount=row["amount"],
                category=row["category"],
                account_name=row["account_name"],
                account_type=row["account_type"],
            )
            for row in rows
        ]

    def get_existing_ids(self) -> set[str]:
        with self._get_connection() as connection:
            rows = connection.execute("SELECT id FROM transactions").fetchall()
        return {row["id"] for row in rows}

    def insert_many(self, transactions: Iterable[Transaction]) -> int:
        payload = [
            (
                transaction.id,
                transaction.date.isoformat(),
                transaction.description,
                transaction.amount,
                transaction.category,
                transaction.account_name,
                transaction.account_type,
            )
            for transaction in transactions
        ]
        if not payload:
            return 0

        with self._get_connection() as connection:
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (id, date, description, amount, category, account_name, account_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return cursor.rowcount

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
            )
            for transaction in transactions
        ]
        if not payload:
            return 0

        with self._get_connection() as connection:
            connection.executemany(
                """
                INSERT INTO transactions
                    (id, date, description, amount, category, account_name, account_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    date = excluded.date,
                    description = excluded.description,
                    amount = excluded.amount,
                    category = CASE
                        WHEN transactions.category = 'Uncategorized' THEN excluded.category
                        ELSE transactions.category
                    END,
                    account_name = excluded.account_name,
                    account_type = excluded.account_type
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
        with self._get_connection() as connection:
            connection.execute(
                "DELETE FROM transactions WHERE account_name = ?", (account_name,)
            )
            if not payload:
                return 0
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (id, date, description, amount, category, account_name, account_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    )
                    for transaction in payload
                ],
            )
            return cursor.rowcount

    def update_category(self, transaction_id: str, category: str) -> None:
        with self._get_connection() as connection:
            connection.execute(
                "UPDATE transactions SET category = ? WHERE id = ?",
                (category, transaction_id),
            )

    def apply_category_rule(self, keyword: str, category: str) -> int:
        with self._get_connection() as connection:
            cursor = connection.execute(
                """
                UPDATE transactions
                SET category = ?
                WHERE lower(description) LIKE ?
                """,
                (category, f"%{keyword.strip().lower()}%"),
            )
            return cursor.rowcount

    def split_transaction(
        self,
        transaction_id: str,
        first_category: str,
        first_amount: float,
        second_category: str,
        second_amount: float,
    ) -> None:
        transactions = {transaction.id: transaction for transaction in self.get_all()}
        original = transactions.get(transaction_id)
        if original is None:
            raise ValueError("Transaction no longer exists.")
        if abs((first_amount + second_amount) - abs(original.amount)) > 0.01:
            raise ValueError("Split amounts must equal the original transaction amount.")
        sign = -1 if original.amount < 0 else 1
        split_rows = [
            original.model_copy(
                update={
                    "id": f"{original.id}-split-1",
                    "description": f"{original.description} (split 1)",
                    "amount": sign * first_amount,
                    "category": first_category,
                }
            ),
            original.model_copy(
                update={
                    "id": f"{original.id}-split-2",
                    "description": f"{original.description} (split 2)",
                    "amount": sign * second_amount,
                    "category": second_category,
                }
            ),
        ]
        with self._get_connection() as connection:
            connection.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,))
            connection.executemany(
                """
                INSERT INTO transactions
                    (id, date, description, amount, category, account_name, account_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    )
                    for item in split_rows
                ],
            )

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
            "version": 1,
            "transactions": [item.model_dump(mode="json") for item in self.get_all()],
            "budgets": [item.model_dump(mode="json") for item in self.get_budgets()],
            "goals": [item.model_dump(mode="json") for item in self.get_goals()],
            "category_rules": [
                item.model_dump(mode="json") for item in self.get_category_rules()
            ],
        }
        return json.dumps(payload, indent=2)

    def restore_backup(self, raw_json: bytes) -> dict:
        data = read_backup(raw_json)
        transactions = [Transaction.model_validate(item) for item in data.get("transactions", [])]
        budgets = [Budget.model_validate(item) for item in data.get("budgets", [])]
        goals = [SavingsGoal.model_validate(item) for item in data.get("goals", [])]
        rules = [CategoryRule.model_validate(item) for item in data.get("category_rules", [])]
        with self._get_connection() as connection:
            connection.execute("DELETE FROM transactions")
            connection.execute("DELETE FROM budgets")
            connection.execute("DELETE FROM savings_goals")
            connection.execute("DELETE FROM category_rules")
            connection.executemany(
                """
                INSERT INTO transactions
                    (id, date, description, amount, category, account_name, account_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
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
                    )
                    for item in transactions
                ],
            )
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
            )
            for transaction in transactions
        ]

        with self._get_connection() as connection:
            connection.execute("DELETE FROM transactions")
            if not payload:
                return 0
            cursor = connection.executemany(
                """
                INSERT OR IGNORE INTO transactions
                    (id, date, description, amount, category, account_name, account_type)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            return cursor.rowcount
