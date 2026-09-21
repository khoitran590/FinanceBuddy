from __future__ import annotations

import json
from datetime import date
from typing import Iterable, Optional

from src.domain.models import Budget, CategoryRule, SavingsGoal, Transaction
from src.repositories.plaid_repo import PlaidItem
from src.services.supabase import SupabaseDataClient


class SupabaseTransactionRepository:
    def __init__(self, client: SupabaseDataClient, user_id: str) -> None:
        self.client = client
        self.user_id = user_id

    @property
    def _owner(self) -> str:
        return f"eq.{self.user_id}"

    def _transaction_payload(self, item: Transaction) -> dict:
        return {
            "user_id": self.user_id,
            "id": item.id,
            "date": item.date.isoformat(),
            "description": item.description,
            "amount": item.amount,
            "category": item.category,
            "account_name": item.account_name,
            "account_type": item.account_type,
        }

    @staticmethod
    def _transaction(row: dict) -> Transaction:
        return Transaction(
            id=row["id"],
            date=date.fromisoformat(str(row["date"])),
            description=row["description"],
            amount=float(row["amount"]),
            category=row["category"],
            account_name=row["account_name"],
            account_type=row["account_type"],
        )

    def get_all(self) -> list[Transaction]:
        rows = self.client.select(
            "transactions", user_id=self._owner, order="date.desc,id.desc"
        )
        return [self._transaction(row) for row in rows]

    def get_existing_ids(self) -> set[str]:
        return {item.id for item in self.get_all()}

    def insert_many(self, transactions: Iterable[Transaction]) -> int:
        payload = [self._transaction_payload(item) for item in transactions]
        if not payload:
            return 0
        rows = self.client.insert("transactions", payload, ignore_duplicates=True)
        return len(rows)

    def upsert_many(self, transactions: Iterable[Transaction]) -> int:
        items = list(transactions)
        if not items:
            return 0
        existing_categories = {item.id: item.category for item in self.get_all()}
        payload = []
        for item in items:
            row = self._transaction_payload(item)
            existing = existing_categories.get(item.id)
            if existing and existing != "Uncategorized":
                row["category"] = existing
            payload.append(row)
        self.client.upsert("transactions", payload, "user_id,id")
        return len(payload)

    def delete_many(self, transaction_ids: Iterable[str]) -> int:
        deleted = 0
        for transaction_id in transaction_ids:
            deleted += len(
                self.client.delete(
                    "transactions", user_id=self._owner, id=f"eq.{transaction_id}"
                )
            )
        return deleted

    def count_existing_ids(self, transactions: Iterable[Transaction]) -> int:
        existing = self.get_existing_ids()
        return sum(item.id in existing for item in transactions)

    def replace_account(self, account_name: str, transactions: Iterable[Transaction]) -> int:
        self.client.delete(
            "transactions", user_id=self._owner, account_name=f"eq.{account_name}"
        )
        return self.insert_many(transactions)

    def update_category(self, transaction_id: str, category: str) -> None:
        self.client.update(
            "transactions",
            {"category": category},
            user_id=self._owner,
            id=f"eq.{transaction_id}",
        )

    def apply_category_rule(self, keyword: str, category: str) -> int:
        rows = self.client.update(
            "transactions",
            {"category": category},
            user_id=self._owner,
            description=f"ilike.*{keyword.strip()}*",
        )
        return len(rows)

    def split_transaction(
        self,
        transaction_id: str,
        first_category: str,
        first_amount: float,
        second_category: str,
        second_amount: float,
    ) -> None:
        original = next((item for item in self.get_all() if item.id == transaction_id), None)
        if original is None:
            raise ValueError("Transaction no longer exists.")
        if abs((first_amount + second_amount) - abs(original.amount)) > 0.01:
            raise ValueError("Split amounts must equal the original transaction amount.")
        sign = -1 if original.amount < 0 else 1
        splits = [
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
        self.insert_many(splits)
        self.delete_many([transaction_id])

    def get_budgets(self) -> list[Budget]:
        rows = self.client.select("budgets", user_id=self._owner, order="category.asc")
        return [Budget(category=row["category"], monthly_limit=float(row["monthly_limit"])) for row in rows]

    def upsert_budget(self, budget: Budget) -> None:
        self.client.upsert(
            "budgets",
            [{"user_id": self.user_id, **budget.model_dump()}],
            "user_id,category",
        )

    def delete_budget(self, category: str) -> None:
        self.client.delete("budgets", user_id=self._owner, category=f"eq.{category}")

    def get_goals(self) -> list[SavingsGoal]:
        rows = self.client.select(
            "savings_goals", user_id=self._owner, order="target_date.asc.nullslast,id.asc"
        )
        return [
            SavingsGoal(
                id=int(row["id"]),
                name=row["name"],
                target_amount=float(row["target_amount"]),
                current_amount=float(row["current_amount"]),
                target_date=date.fromisoformat(str(row["target_date"])) if row.get("target_date") else None,
            )
            for row in rows
        ]

    def save_goal(self, goal: SavingsGoal) -> int:
        payload = {
            "user_id": self.user_id,
            "name": goal.name,
            "target_amount": goal.target_amount,
            "current_amount": goal.current_amount,
            "target_date": goal.target_date.isoformat() if goal.target_date else None,
        }
        if goal.id is None:
            return int(self.client.insert("savings_goals", [payload])[0]["id"])
        self.client.update(
            "savings_goals", payload, user_id=self._owner, id=f"eq.{goal.id}"
        )
        return goal.id

    def delete_goal(self, goal_id: int) -> None:
        self.client.delete("savings_goals", user_id=self._owner, id=f"eq.{goal_id}")

    def get_category_rules(self) -> list[CategoryRule]:
        rows = self.client.select(
            "category_rules", user_id=self._owner, order="keyword.asc"
        )
        return [CategoryRule(keyword=row["keyword"], category=row["category"]) for row in rows]

    def upsert_category_rule(self, rule: CategoryRule) -> None:
        self.client.upsert(
            "category_rules",
            [{"user_id": self.user_id, "keyword": rule.keyword.strip().lower(), "category": rule.category}],
            "user_id,keyword",
        )

    def delete_category_rule(self, keyword: str) -> None:
        self.client.delete("category_rules", user_id=self._owner, keyword=f"eq.{keyword}")

    def delete_account(self, account_name: str) -> int:
        return len(
            self.client.delete(
                "transactions", user_id=self._owner, account_name=f"eq.{account_name}"
            )
        )

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self.client.select("app_settings", user_id=self._owner, key=f"eq.{key}")
        return str(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self.client.upsert(
            "app_settings",
            [{"user_id": self.user_id, "key": key, "value": value}],
            "user_id,key",
        )

    def export_backup(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "transactions": [item.model_dump(mode="json") for item in self.get_all()],
                "budgets": [item.model_dump(mode="json") for item in self.get_budgets()],
                "goals": [item.model_dump(mode="json") for item in self.get_goals()],
                "category_rules": [item.model_dump(mode="json") for item in self.get_category_rules()],
            },
            indent=2,
        )

    def restore_backup(self, raw_json: bytes) -> dict:
        data = json.loads(raw_json.decode("utf-8"))
        if data.get("version") != 1:
            raise ValueError("Unsupported FinanceBuddy backup version.")
        transactions = [Transaction.model_validate(item) for item in data.get("transactions", [])]
        budgets = [Budget.model_validate(item) for item in data.get("budgets", [])]
        goals = [SavingsGoal.model_validate(item) for item in data.get("goals", [])]
        rules = [CategoryRule.model_validate(item) for item in data.get("category_rules", [])]
        for table in ("transactions", "budgets", "savings_goals", "category_rules"):
            self.client.delete(table, user_id=self._owner)
        self.insert_many(transactions)
        for budget in budgets:
            self.upsert_budget(budget)
        for goal in goals:
            self.save_goal(goal.model_copy(update={"id": None}))
        for rule in rules:
            self.upsert_category_rule(rule)
        return {
            "transactions": len(transactions),
            "budgets": len(budgets),
            "goals": len(goals),
            "category_rules": len(rules),
        }

    def replace_all(self, transactions: Iterable[Transaction]) -> int:
        self.client.delete("transactions", user_id=self._owner)
        return self.insert_many(transactions)


class SupabasePlaidRepository:
    def __init__(self, client: SupabaseDataClient, user_id: str) -> None:
        self.client = client
        self.user_id = user_id

    @property
    def _owner(self) -> str:
        return f"eq.{self.user_id}"

    def save_item(self, item_id: str, encrypted_access_token: str, institution_name: str) -> None:
        self.client.upsert(
            "plaid_items",
            [{
                "user_id": self.user_id,
                "item_id": item_id,
                "encrypted_access_token": encrypted_access_token,
                "institution_name": institution_name,
                "status": "connected",
                "error_message": None,
            }],
            "user_id,item_id",
        )

    def save_accounts(self, item_id: str, accounts: list[dict]) -> None:
        self.client.delete("plaid_accounts", user_id=self._owner, item_id=f"eq.{item_id}")
        payload = [
            {
                "user_id": self.user_id,
                "account_id": account["account_id"],
                "item_id": item_id,
                "name": account.get("name") or "Connected account",
                "official_name": account.get("official_name"),
                "type": str(account.get("type") or "depository"),
                "subtype": str(account.get("subtype") or ""),
                "mask": account.get("mask"),
            }
            for account in accounts
        ]
        if payload:
            self.client.insert("plaid_accounts", payload)

    def get_items(self) -> list[PlaidItem]:
        rows = self.client.select(
            "plaid_items", user_id=self._owner, order="institution_name.asc,item_id.asc"
        )
        return [
            PlaidItem(
                item_id=row["item_id"],
                access_token=row["encrypted_access_token"],
                institution_name=row["institution_name"],
                cursor=row.get("cursor"),
                status=row["status"],
                last_synced_at=row.get("last_synced_at"),
                error_message=row.get("error_message"),
            )
            for row in rows
        ]

    def get_item(self, item_id: str) -> Optional[PlaidItem]:
        return next((item for item in self.get_items() if item.item_id == item_id), None)

    def get_accounts(self, item_id: Optional[str] = None) -> list[dict]:
        params = {"user_id": self._owner, "order": "name.asc"}
        if item_id:
            params["item_id"] = f"eq.{item_id}"
        return self.client.select("plaid_accounts", **params)

    def update_sync(self, item_id: str, cursor: str, synced_at: str) -> None:
        self.client.update(
            "plaid_items",
            {"cursor": cursor, "last_synced_at": synced_at, "status": "connected", "error_message": None},
            user_id=self._owner,
            item_id=f"eq.{item_id}",
        )

    def set_error(self, item_id: str, message: str) -> None:
        self.client.update(
            "plaid_items",
            {"status": "error", "error_message": message},
            user_id=self._owner,
            item_id=f"eq.{item_id}",
        )

    def delete_item(self, item_id: str) -> None:
        self.client.delete("plaid_items", user_id=self._owner, item_id=f"eq.{item_id}")
