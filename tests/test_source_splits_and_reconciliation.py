import json
from datetime import date
from unittest.mock import Mock

import pytest

from src.domain.allocations import recover_legacy_splits
from src.domain.models import Transaction
from src.repositories.plaid_repo import PlaidRepository
from src.repositories.supabase_repo import SupabaseTransactionRepository
from src.repositories.transaction_repo import TransactionRepository
from src.services.analytics import AnalyticsService
from src.services.plaid_service import PlaidConfig, PlaidService, TokenCipher


def transaction(identifier="plaid:purchase", amount=-100, **fields):
    return Transaction(id=identifier, date=date(2026, 9, 1), description="Store",
                       amount=amount, category="Shopping", account_name="Card", **fields)


def test_split_keeps_source_and_survives_sync_and_statement_reimport(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    source = transaction()
    repo.insert_many([source])
    repo.split_transaction(source.id, "Groceries", 60, "Shopping", 40)
    assert repo.get_existing_ids() == {source.id}
    assert len(repo.get_canonical_all()) == 1
    assert len(repo.get_canonical_all()[0].allocations) == 2
    assert repo.insert_many([source]) == 0
    repo.upsert_many([source.model_copy(update={"description": "Updated store"})])
    assert len(repo.get_all()) == 2
    assert AnalyticsService.calculate_summary(repo.get_all())["outflow"] == 100
    assert all(row.description == "Updated store" for row in repo.get_all())
    repo.delete_many([source.id])
    assert repo.get_all() == []
    with repo._get_connection() as connection:
        assert connection.execute("SELECT count(*) FROM transaction_allocations").fetchone()[0] == 0


def test_provider_amount_change_counts_current_source_once_until_resplit(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.insert_many([transaction()])
    repo.split_transaction("plaid:purchase", "Groceries", 60, "Shopping", 40)
    repo.upsert_many([transaction(amount=-110)])
    assert len(repo.get_all()) == 1
    assert repo.get_all()[0].split_needs_review
    assert AnalyticsService.calculate_summary(repo.get_all())["outflow"] == 110
    assert len(repo.get_canonical_all()[0].allocations) == 2
    repo.split_transaction("plaid:purchase", "Groceries", 70, "Shopping", 40)
    assert len(repo.get_all()) == 2
    assert not any(row.split_needs_review for row in repo.get_all())


def test_allocation_category_edit_does_not_mutate_bank_category(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.insert_many([transaction()])
    repo.split_transaction("plaid:purchase", "Groceries", 60, "Shopping", 40)
    selected = repo.get_all()[0]
    repo.update_category(selected.id, "Travel")
    assert "Travel" in {row.category for row in repo.get_all()}
    assert repo.get_canonical_all()[0].category == "Shopping"


@pytest.mark.parametrize("amounts", [(0, 100), (-1, 101), (float("nan"), 100), (60, 30)])
def test_invalid_resplit_preserves_existing_allocations(tmp_path, amounts):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.insert_many([transaction()])
    repo.split_transaction("plaid:purchase", "Groceries", 60, "Shopping", 40)
    before = repo.get_canonical_all()[0].model_dump(mode="json")
    with pytest.raises(ValueError):
        repo.split_transaction("plaid:purchase", "Groceries", amounts[0], "Shopping", amounts[1])
    assert repo.get_canonical_all()[0].model_dump(mode="json") == before


def test_v2_backup_round_trips_source_allocations_and_provider_identity(tmp_path):
    repo = TransactionRepository(str(tmp_path / "source.db"))
    repo.insert_many([transaction(plaid_item_id="item", plaid_account_id="account")])
    repo.split_transaction("plaid:purchase", "Groceries", 60, "Shopping", 40)
    backup = repo.export_backup()
    data = json.loads(backup)
    assert data["version"] == 2 and len(data["transactions"]) == 1
    assert "source_transaction_id" not in data["transactions"][0]
    restored = TransactionRepository(str(tmp_path / "restored.db"))
    restored.restore_backup(backup.encode())
    assert restored.get_canonical_all() == repo.get_canonical_all()
    assert sum(row.amount for row in restored.get_all()) == -100


def test_legacy_complete_and_nested_splits_recover_but_partial_pairs_do_not():
    def part(identifier, description, amount):
        return transaction(identifier, amount).model_copy(update={"description": description})
    rows = [part("source-split-1-split-1", "Store (split 1) (split 1)", -20),
            part("source-split-1-split-2", "Store (split 1) (split 2)", -40),
            part("source-split-2", "Store (split 2)", -40)]
    recovered = recover_legacy_splits(rows)
    assert len(recovered) == 1 and recovered[0].id == "source"
    assert recovered[0].amount == -100
    assert len(recovered[0].allocations) == 3
    assert recover_legacy_splits(rows[:1]) == rows[:1]
    # An original already re-created by a bank update remains authoritative.
    recovered = recover_legacy_splits(rows + [transaction("source", -110)])
    assert len(recovered) == 1 and recovered[0].amount == -110


class SnapshotClient:
    def __init__(self, status="HISTORICAL_UPDATE_COMPLETE"):
        self.status = status
        self.cursors = []

    def transactions_sync(self, request):
        self.cursors.append(request.to_dict().get("cursor"))
        return {"added": [{"transaction_id": "purchase", "account_id": "account", "date": "2026-09-01",
                           "name": "Store", "amount": 100},
                          {"transaction_id": "new", "account_id": "account", "date": "2026-10-01",
                           "name": "New", "amount": 5}],
                "modified": [], "removed": [], "next_cursor": "latest", "has_more": False,
                "transactions_update_status": self.status}

    def accounts_get(self, request):
        return {"accounts": [{"account_id": "account", "name": "Card", "type": "credit"}]}


def connected_repositories(tmp_path):
    path = str(tmp_path / "finance.db")
    bank, repo = PlaidRepository(path), TransactionRepository(path)
    config = PlaidConfig("client", "secret", token_encryption_key="key")
    bank.save_item("item", TokenCipher(config).encrypt("access"), "Bank")
    bank.save_accounts("item", [{"account_id": "account", "name": "Card", "type": "credit"}])
    repo.insert_many([transaction(plaid_item_id="item", plaid_account_id="account")])
    repo.split_transaction("plaid:purchase", "Groceries", 60, "Shopping", 40)
    bank.update_sync("item", "october-cursor", "2026-10-02T00:00:00Z")
    return bank, repo, config


def test_older_restore_resets_cursor_rebuilds_missing_rows_and_prunes_stale(tmp_path):
    bank, repo, config = connected_repositories(tmp_path)
    data = json.loads(repo.export_backup())
    data["transactions"].append(transaction("plaid:stale", -25, plaid_item_id="item").model_dump(mode="json"))
    data["transactions"].append(transaction("statement", -10).model_dump(mode="json"))
    repo.restore_backup(json.dumps(data).encode())
    restored_item = bank.get_item("item")
    assert restored_item.cursor is None and restored_item.needs_reconciliation
    assert restored_item.sync_generation == 1
    client = SnapshotClient()
    PlaidService(config, bank, repo, client).sync_item("item")
    assert client.cursors == [None]
    assert repo.get_existing_ids() == {"plaid:purchase", "plaid:new", "statement"}
    assert len(next(row for row in repo.get_canonical_all() if row.id == "plaid:purchase").allocations) == 2
    assert bank.get_item("item").cursor == "latest"
    assert not bank.get_item("item").needs_reconciliation


@pytest.mark.parametrize("status", ["NOT_READY", "INITIAL_UPDATE_COMPLETE", None])
def test_incomplete_history_does_not_advance_reset_cursor_or_prune(tmp_path, status):
    bank, repo, config = connected_repositories(tmp_path)
    repo.restore_backup(repo.export_backup().encode())
    before = repo.get_canonical_all()
    result = PlaidService(config, bank, repo, SnapshotClient(status)).sync_item("item")
    assert result["reconciliation_pending"]
    assert repo.get_canonical_all() == before
    assert bank.get_item("item").cursor is None


def test_real_sdk_response_serializes_readiness_for_reconciliation(tmp_path):
    from plaid.model.transactions_sync_response import TransactionsSyncResponse
    from plaid.model.transactions_update_status import TransactionsUpdateStatus

    class SDKClient(SnapshotClient):
        def transactions_sync(self, request):
            return TransactionsSyncResponse(
                added=[], modified=[], removed=[], accounts=[], next_cursor="latest",
                has_more=False, request_id="test",
                transactions_update_status=TransactionsUpdateStatus("HISTORICAL_UPDATE_COMPLETE"),
            )

    bank, repo, config = connected_repositories(tmp_path)
    repo.restore_backup(repo.export_backup().encode())
    result = PlaidService(config, bank, repo, SDKClient()).sync_item("item")
    assert not result.get("reconciliation_pending")
    assert not bank.get_item("item").needs_reconciliation
    assert bank.get_item("item").cursor == "latest"


def test_generation_rejects_sync_started_before_restore_even_with_same_null_cursor(tmp_path):
    bank, repo, _ = connected_repositories(tmp_path)
    repo.restore_backup(repo.export_backup().encode())
    old = bank.get_item("item")
    repo.restore_backup(repo.export_backup().encode())
    before = repo.get_canonical_all()
    with pytest.raises(ValueError, match="cursor changed"):
        repo.apply_plaid_sync("item", None, "stale", [], [], "2026-10-13T00:00:00Z",
                              expected_generation=old.sync_generation, reconcile=True)
    assert repo.get_canonical_all() == before
    assert bank.get_item("item").needs_reconciliation


def test_reconciliation_is_scoped_to_item(tmp_path):
    bank, repo, _ = connected_repositories(tmp_path)
    bank.save_item("other", "encrypted", "Other bank")
    repo.insert_many([transaction("plaid:other", -15, plaid_item_id="other")])
    repo.restore_backup(repo.export_backup().encode())
    repo.apply_plaid_sync("item", None, "latest", [], [], "2026-10-13T00:00:00Z",
                          expected_generation=1, reconcile=True)
    assert repo.get_existing_ids() == {"plaid:other"}
    assert bank.get_item("other").needs_reconciliation


def test_failed_restore_rolls_back_cursor_invalidation_and_data(tmp_path):
    bank, repo, _ = connected_repositories(tmp_path)
    data = json.loads(repo.export_backup())
    data["transactions"].append(data["transactions"][0])
    with pytest.raises(Exception):
        repo.restore_backup(json.dumps(data).encode())
    assert bank.get_item("item").cursor == "october-cursor"
    assert repo.get_existing_ids() == {"plaid:purchase"}


def test_account_replacement_and_deletion_require_full_sync(tmp_path):
    bank, repo, _ = connected_repositories(tmp_path)
    repo.replace_account("Card", [])
    assert bank.get_item("item").needs_reconciliation
    assert bank.get_item("item").cursor is None


def test_account_deletion_invalidates_cursor_and_cascades_allocations(tmp_path):
    bank, repo, _ = connected_repositories(tmp_path)
    assert repo.delete_account("Card") == 1
    assert bank.get_item("item").needs_reconciliation
    assert repo.get_all() == []


def test_v1_backup_matches_existing_provider_identity_before_replacement(tmp_path):
    bank, repo, config = connected_repositories(tmp_path)
    backup = json.loads(repo.export_backup())
    backup["version"] = 1
    for row in backup["transactions"]:
        row.pop("plaid_item_id")
        row.pop("plaid_account_id")
    repo.restore_backup(json.dumps(backup).encode())
    assert repo.get_canonical_all()[0].plaid_item_id == "item"
    PlaidService(config, bank, repo, SnapshotClient()).sync_item("item")
    assert repo.get_existing_ids() == {"plaid:purchase", "plaid:new"}


def test_ambiguous_legacy_bank_restore_is_rejected_without_deleting_data(tmp_path):
    bank, repo, _ = connected_repositories(tmp_path)
    bank.save_item("second", "encrypted", "Other bank")
    bank.save_accounts("second", [{"account_id": "second-account", "name": "Card", "type": "credit"}])
    backup = {"version": 1, "transactions": [transaction("plaid:unknown").model_dump(mode="json")],
              "budgets": [], "goals": [], "category_rules": []}
    with pytest.raises(ValueError, match="matched"):
        repo.restore_backup(json.dumps(backup).encode())
    assert repo.get_existing_ids() == {"plaid:purchase"}
    assert bank.get_item("item").cursor == "october-cursor"


def test_legacy_sqlite_export_recovers_complete_splits(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = TransactionRepository(path)
    legacy.insert_many([
        transaction("old-split-1", -60).model_copy(update={"description": "Store (split 1)"}),
        transaction("old-split-2", -40).model_copy(update={"description": "Store (split 2)"}),
    ])
    upgraded = TransactionRepository(path)
    backup = json.loads(upgraded.export_backup())
    assert len(backup["transactions"]) == 1
    assert backup["transactions"][0]["id"] == "old"
    assert len(backup["transactions"][0]["allocations"]) == 2


def test_supabase_reads_canonical_records_but_projects_allocations_for_ui(tmp_path):
    local = TransactionRepository(str(tmp_path / "source.db"))
    local.insert_many([transaction()])
    local.split_transaction("plaid:purchase", "Groceries", 60, "Shopping", 40)
    client = Mock()
    client.select.side_effect = lambda table, **params: (
        [local.get_canonical_all()[0].model_dump(mode="json")] if table == "transactions" else []
    )
    repo = SupabaseTransactionRepository(client, "alice")
    assert len(repo.get_all()) == 2
    assert repo.get_existing_ids() == {"plaid:purchase"}
    client.rpc.return_value = json.loads(local.export_backup())
    assert json.loads(repo.export_backup())["transactions"][0]["id"] == "plaid:purchase"
    assert client.rpc.call_args.args[0] == "fb_export_backup"
    assert "allocations:transaction_allocations" in client.select.call_args_list[0].kwargs["select"]
