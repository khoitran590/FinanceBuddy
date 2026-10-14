from __future__ import annotations

import json
from datetime import date
from typing import Iterable, Optional

from src.services.security import read_backup
from src.services.supabase import SupabaseDataClient, SupabaseError

from src.domain.models import Budget, CategoryRule, SavingsGoal, Transaction, StatementImportRow
from src.services.imports import plan_statement_import
from src.domain.allocations import project_transactions, recover_legacy_splits
from src.domain.banking import PlaidItem, account_balances
from src.domain.money import money
from src.services.operations import OperationService
from src.services.backups import validate_backup, serialize_backup

OPTIONAL_TRANSACTION_FIELDS = ("merchant_name", "subcategory", "payment_channel", "location", "pending", "plaid_item_id", "plaid_account_id", "statement_file_id", "statement_row", "statement_reference", "statement_match_bank", "currency", "account_id", "category_origin", "provider_category", "builtin_category", "category_override", "category_rule", "category_rule_version", "pending_transaction_id")


class SupabaseTransactionRepository:
    def __init__(self, client: SupabaseDataClient, user_id: str) -> None:
        self.client = client
        self.user_id = user_id
        self.operations = OperationService(client, getattr(client, "pending_operation_ids", None))
        self._catalog = None

    @property
    def _owner(self) -> str:
        return f"eq.{self.user_id}"

    def _transaction_payload(self, item: Transaction) -> dict:
        payload = {
            "user_id": self.user_id,
            "id": item.id,
            "date": item.date.isoformat(),
            "description": item.description,
            "amount": str(item.amount),
            "category": item.category,
            "account_name": item.account_name,
            "account_type": item.account_type,
        }
        # Provider detail is sent only when present, so statement imports keep
        # working against a database that predates these optional columns.
        for field in OPTIONAL_TRANSACTION_FIELDS:
            value = getattr(item, field)
            if value:
                payload[field] = str(value) if field == "account_id" else value
        return payload

    def _transaction_rows(self, items: Iterable[Transaction]) -> list[dict]:
        """Payloads with one shared key set, as PostgREST bulk writes require."""
        rows = [self._transaction_payload(item) for item in items]
        present = {field for row in rows for field in OPTIONAL_TRANSACTION_FIELDS if field in row}
        for row in rows:
            for field in present:
                row.setdefault(field, False if field in ("pending", "statement_match_bank", "currency", "account_id", "category_origin", "provider_category", "builtin_category", "category_override", "category_rule", "category_rule_version", "pending_transaction_id") else None)
        return rows

    @staticmethod
    def _transaction(row: dict) -> Transaction:
        return Transaction.model_validate(row)

    def get_canonical_all(self) -> list[Transaction]:
        rows = self.client.select(
            "transactions", user_id=self._owner, order="date.desc,id.desc",
            select="*,allocations:transaction_allocations(id,amount,category)",
        )
        return [self._transaction(row) for row in rows]

    def get_all(self) -> list[Transaction]:
        return project_transactions(self.get_canonical_all())

    def get_existing_ids(self) -> set[str]:
        return {row["id"] for row in self.client.select("transactions", user_id=self._owner, select="id")}

    def insert_many(self, transactions: Iterable[Transaction]) -> int:
        items = list(transactions)
        if items and all(item.statement_file_id for item in items):
            return self.append_statement(items)["inserted"]
        payload = self._transaction_rows(items)
        if not payload:
            return 0
        rows = self.client.insert("transactions", payload, ignore_duplicates=True)
        self._catalog = None
        return len(rows)

    def get_import_rows(self, file_id=None) -> list[dict]:
        params = {"user_id": self._owner, "order": "file_id.asc,row_number.asc"}
        if file_id:
            params["file_id"] = f"eq.{file_id}"
        return self.client.select("statement_import_rows", **params)

    def preview_statement_import(self, transactions, *, keep_matching=False) -> dict:
        items = self._prepare_statement(list(transactions))
        if not items:
            return plan_statement_import([], [])
        candidates = self.client.rpc("fb_statement_candidates", {"p_rows": [item.model_dump(mode="json") for item in items]})
        return plan_statement_import(items, [self._transaction(row) for row in candidates["transactions"]],
                                     candidates["mappings"], keep_matching=keep_matching)

    def _prepare_statement(self, items):
        if not items or items[0].account_id:
            return items
        matches = [row for row in self.get_catalog()["accounts"]
                   if row["name"].strip().lower() == items[0].account_name.strip().lower()
                   and row["account_type"].lower() == items[0].account_type.lower()]
        if len(matches) > 1:
            raise ValueError("Select a saved account; this name belongs to several accounts.")
        if matches:
            from uuid import UUID
            return [item.model_copy(update={"account_id": UUID(matches[0]["id"])}) for item in items]
        return items

    def is_connected_account(self, account_name: str, account_id=None) -> bool:
        if account_id:
            return any(row["id"] == str(account_id) and row["connected"] for row in self.get_catalog()["accounts"])
        return bool(self.client.rpc("fb_statement_account_is_connected", {"p_account": account_name}))

    def append_statement(self, transactions, *, keep_matching=False, operation_id=None) -> dict:
        items = list(transactions)
        if not items:
            return {"inserted": 0, "matched": 0, "inserted_rows": []}
        result = self.operations.execute("append", {
            "rows": [item.model_dump(mode="json") for item in items],
            "keep_matching": keep_matching,
        }, operation_id=operation_id)
        self._catalog = None
        return {**result, "inserted_rows": [self._transaction(row) for row in result["inserted_rows"]]}

    def upsert_many(self, transactions: Iterable[Transaction]) -> int:
        items = list(transactions)
        if not items:
            return 0
        existing_categories = {row["id"]: row["category"] for row in self.client.select("transactions", user_id=self._owner, select="id,category")}
        payload = self._transaction_rows(items)
        for item, row in zip(items, payload):
            existing = existing_categories.get(item.id)
            if existing and existing != "Uncategorized":
                row["category"] = existing
        self.client.upsert("transactions", payload, "user_id,id")
        self._catalog = None
        return len(payload)

    def delete_many(self, transaction_ids: Iterable[str]) -> int:
        deleted = 0
        for transaction_id in transaction_ids:
            deleted += len(
                self.client.delete(
                    "transactions", user_id=self._owner, id=f"eq.{transaction_id}"
                )
            )
        self._catalog = None
        return deleted

    def count_existing_ids(self, transactions: Iterable[Transaction]) -> int:
        items = list(transactions)
        existing = set(self.client.rpc("fb_existing_transaction_ids", {"p_ids": [item.id for item in items]}) or [])
        return sum(item.id in existing for item in items)

    def replace_account(self, account_name: str, transactions: Iterable[Transaction]) -> int:
        items = list(transactions)
        if any(item.account_name != account_name for item in items):
            raise ValueError("Statement account does not match the selected account.")
        result = self.operations.execute("replace_account", {
            "account_id": str(items[0].account_id) if items and items[0].account_id else None,
            "account_name": account_name, "rows": [item.model_dump(mode="json") for item in items],
        })
        self._catalog = None
        return int(result)

    def update_category(self, transaction_id: str, category: str) -> None:
        self.correct_category(transaction_id, category)

    def correct_category(self, transaction_id, category, keyword='', apply_similar=False):
        result = self.operations.execute("category", {"id": transaction_id, "category": category,
                                                    "keyword": keyword, "apply_similar": apply_similar})
        self._catalog = None
        return result

    def correct_categories(self, transaction_ids, category):
        result = self.operations.execute('bulk_category', {'ids': list(transaction_ids), 'category': category})
        self._catalog = None
        return int(result)

    def apply_category_rule(self, keyword: str, category: str) -> int:
        return int(self.client.rpc("fb_apply_category_rule", {
            "p_keyword": keyword.strip(), "p_category": category,
        }))

    def split_transaction(
        self,
        transaction_id: str,
        first_category: str,
        first_amount: float,
        second_category: str,
        second_amount: float,
    ) -> None:
        self.operations.execute("split", {"id": transaction_id, "first_category": first_category,
            "first_amount": str(money(first_amount)), "second_category": second_category,
            "second_amount": str(money(second_amount))})
        self._catalog = None

    def get_budgets(self) -> list[Budget]:
        rows = self.client.select("budgets", user_id=self._owner, order="category.asc")
        return [Budget(category=row["category"], monthly_limit=row["monthly_limit"]) for row in rows]

    def upsert_budget(self, budget: Budget) -> None:
        self.operations.execute("budget", budget.model_dump(mode="json"))

    def delete_budget(self, category: str) -> None:
        self.operations.execute("delete_budget", {"category": category})

    def get_goals(self) -> list[SavingsGoal]:
        rows = self.client.select(
            "savings_goals", user_id=self._owner, order="target_date.asc.nullslast,id.asc"
        )
        return [
            SavingsGoal(
                id=int(row["id"]),
                name=row["name"],
                target_amount=row["target_amount"],
                current_amount=row["current_amount"],
                target_date=date.fromisoformat(str(row["target_date"])) if row.get("target_date") else None,
            )
            for row in rows
        ]

    def save_goal(self, goal: SavingsGoal) -> int:
        return int(self.operations.execute("goal", goal.model_dump(mode="json")))

    def delete_goal(self, goal_id: int) -> None:
        self.operations.execute("delete_goal", {"id": goal_id})

    def get_category_rules(self) -> list[CategoryRule]:
        rows = self.client.select(
            "category_rules", user_id=self._owner, order="keyword.asc"
        )
        return [CategoryRule.model_validate(row) for row in rows]

    def upsert_category_rule(self, rule: CategoryRule) -> None:
        self.operations.execute("rule", rule.model_dump(mode="json"))

    def delete_category_rule(self, keyword: str) -> None:
        self.operations.execute("delete_rule", {"keyword": keyword})

    def delete_account(self, account_id: str) -> int:
        from uuid import UUID
        result = self.operations.execute("delete_account", {"account_id": str(UUID(account_id))})
        self._catalog = None
        return int(result)

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        rows = self.client.select("app_settings", user_id=self._owner, key=f"eq.{key}")
        return str(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self.operations.execute("setting", {"key": key, "value": value})

    def export_backup(self) -> str:
        return serialize_backup(self.client.rpc("fb_export_backup", {}))

    def restore_backup(self, raw_json: bytes) -> dict:
        result = self.operations.execute("restore", {"backup": validate_backup(raw_json)})
        self._catalog = None
        return result

    def replace_all(self, transactions: Iterable[Transaction]) -> int:
        result = self.operations.execute("replace_all", {"rows": [item.model_dump(mode="json") for item in transactions]})
        self._catalog = None
        return int(result)

    def undo_import_batch(self, batch_id: str) -> int:
        result = self.operations.execute("undo_batch", {"batch_id": batch_id})
        self._catalog = None
        return int(result)

    def get_import_batches(self):
        return self.client.rpc("fb_import_batches", {}) or []

    def get_catalog(self):
        if self._catalog is None:
            self._catalog = self.client.rpc("fb_transaction_catalog", {})
        return self._catalog

    def get_page(self, filters=None, *, limit=200, after=None):
        result = self.client.rpc("fb_query_transactions", {"p_filters": filters or {}, "p_limit": limit, "p_after": after})
        return {**result, "rows": [self._transaction(row) for row in result["rows"]]}

    def get_filtered(self, filters=None):
        result, cursor = [], None
        while True:
            page = self.get_page({**(filters or {}), "skip_count": True}, limit=1000, after=cursor)
            result.extend(page["rows"])
            cursor = page["next_cursor"]
            if cursor is None:
                return result

    def get_summary(self, filters=None):
        return self.client.rpc("fb_transaction_summary", {"p_filters": filters or {}})

    def undo_append(self, transactions: Iterable[Transaction]) -> int:
        return int(self.client.rpc("fb_undo_append", {
            "p_rows": [self._transaction_payload(item) for item in transactions],
        }))

    def apply_plaid_sync(
        self, item_id: str, expected_cursor: str | None, next_cursor: str,
        upserts: Iterable[Transaction], removed_ids: Iterable[str], synced_at: str,
        *, expected_generation: int = 0, reconcile: bool = False,
    ) -> dict:
        result = self.operations.execute("sync", {
            "p_item_id": item_id,
            "p_expected_cursor": expected_cursor,
            "p_next_cursor": next_cursor,
            "p_synced_at": synced_at,
            "p_upserts": [self._transaction_payload(item) for item in upserts],
            "p_removed_ids": list(removed_ids),
            "p_expected_generation": expected_generation,
            "p_reconcile": reconcile,
        })

        self._catalog = None
        return result


class SupabasePlaidRepository:
    def __init__(self, client: SupabaseDataClient, user_id: str) -> None:
        self.client = client
        self.user_id = user_id
        self.operations = OperationService(client, getattr(client, "pending_operation_ids", None))
        self._catalog = None

    @property
    def _owner(self) -> str:
        return f"eq.{self.user_id}"

    def save_item(self, item_id: str, encrypted_access_token: str, institution_name: str) -> None:
        self.client.rpc("fb_save_plaid_item", {
            "p_item_id": item_id,
            "p_encrypted_access_token": encrypted_access_token,
            "p_institution_name": institution_name,
        })

    def save_accounts(self, item_id: str, accounts: list[dict]) -> None:
        payload = [
            {
                "account_id": account["account_id"],
                "name": account.get("name") or "Connected account",
                "official_name": account.get("official_name"),
                "type": str(account.get("type") or "depository"),
                "subtype": str(account.get("subtype") or ""),
                "mask": account.get("mask"),
                **account_balances(account),
            }
            for account in accounts
        ]
        self.client.rpc("fb_replace_plaid_accounts", {"p_item_id": item_id, "p_rows": payload})

    def get_items(self) -> list[PlaidItem]:
        rows = self.client.select(
            "plaid_items", user_id=self._owner, order="institution_name.asc,item_id.asc"
        )
        return [self._item(row) for row in rows]

    @staticmethod
    def _item(row, token=''):
        return PlaidItem(item_id=row["item_id"], access_token=token,
                         institution_name=row["institution_name"], cursor=row.get("cursor"),
                         status=row["status"], last_synced_at=row.get("last_synced_at"),
                         error_message=row.get("error_message"), sync_generation=int(row.get("sync_generation") or 0),
                         needs_reconciliation=bool(row.get("needs_reconciliation", False)),
                         disconnect_remove_transactions=bool(row.get("disconnect_remove_transactions", False)))

    def get_item(self, item_id: str) -> Optional[PlaidItem]:
        rows = self.client.select("plaid_items", user_id=self._owner, item_id=f"eq.{item_id}", limit="1")
        if not rows:
            return None
        token = self.client.rpc("fb_plaid_token", {"p_item_id": item_id})
        if not token:
            raise SupabaseError("This bank connection needs to be reconnected before it can sync.")
        return self._item(rows[0], token)

    def begin_disconnect(self, item_id, remove_transactions):
        return bool(self.operations.execute("disconnect_begin", {"item_id": item_id, "remove_transactions": remove_transactions}))

    def finish_disconnect(self, item_id):
        return int(self.operations.execute("disconnect_finish", {"item_id": item_id}))

    def get_accounts(self, item_id: Optional[str] = None) -> list[dict]:
        params = {"user_id": self._owner, "order": "name.asc"}
        if item_id:
            params["item_id"] = f"eq.{item_id}"
        return self.client.select("plaid_accounts", **params)

    def get_balance_history(self, since=None) -> list[dict]:
        from datetime import timedelta
        since = since or date.today() - timedelta(days=366)
        return self.client.rpc("fb_balance_history", {"p_since": since.isoformat()}) or []

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
