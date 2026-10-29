from datetime import date

from src.domain.models import Budget, CategoryRule, SavingsGoal, Transaction
from src.repositories.transaction_repo import TransactionRepository


def transaction(identifier: str, account: str, amount: float = -100.0) -> Transaction:
    return Transaction(
        id=identifier,
        date=date(2026, 8, 1),
        description=f"Merchant {identifier}",
        amount=amount,
        category="Shopping",
        account_name=account,
        account_type="Checking",
    )


def test_repository_supports_multi_account_replacement_and_split(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.insert_many([transaction("a", "Checking"), transaction("b", "Card")])

    repo.replace_account("Checking", [transaction("c", "Checking", -80.0)])

    assert {item.id for item in repo.get_all()} == {"b", "c"}
    repo.split_transaction("c", "Groceries", 30.0, "Dining", 50.0)
    split = [item for item in repo.get_all() if item.account_name == "Checking"]
    assert {item.category for item in split} == {"Groceries", "Dining"}
    assert sum(item.amount for item in split) == -80.0


def test_repository_round_trips_budgets_goals_rules_and_backup(tmp_path):
    repo = TransactionRepository(str(tmp_path / "source.db"))
    repo.insert_many([transaction("a", "Checking")])
    repo.upsert_budget(Budget(category="Shopping", monthly_limit=400.0))
    repo.save_goal(
        SavingsGoal(
            name="Emergency fund",
            target_amount=5000.0,
            current_amount=750.0,
            target_date=date(2027, 1, 1),
        )
    )
    repo.upsert_category_rule(CategoryRule(keyword="merchant", category="Shopping"))

    restored = TransactionRepository(str(tmp_path / "restored.db"))
    result = restored.restore_backup(repo.export_backup().encode())

    assert result == {
        "transactions": 1,
        "budgets": 1,
        "goals": 1,
        "category_rules": 1,
    }
    assert restored.get_budgets()[0].monthly_limit == 400.0
    assert restored.get_goals()[0].name == "Emergency fund"
    assert restored.get_category_rules()[0].keyword == "merchant"


def test_repository_persists_onboarding_setting(tmp_path):
    repo = TransactionRepository(str(tmp_path / "settings.db"))

    assert repo.get_setting("onboarding_complete", "false") == "false"
    repo.set_setting("onboarding_complete", "true")
    assert repo.get_setting("onboarding_complete") == "true"


def test_triage_uses_the_rule_where_it_applies_and_leaves_other_rows(tmp_path):
    repo = TransactionRepository(str(tmp_path / "triage.db"))
    rows = [transaction(identifier, "Card") for identifier in ("a", "b", "c", "d")]
    rows[0].description, rows[1].description = "BLUE BOTTLE COFFEE OAKLAND", "BLUE BOTTLE COFFEE SF"
    rows[2].description, rows[3].description = "CORNER STORE", "BLUE BOTTLE COFFEE NYC"
    repo.insert_many(rows)
    repo.update_category("b", "Travel")

    assert repo.triage_category(["a", "b", "c"], "Dining", " Blue Bottle Coffee ") == 3

    saved = {item.id: item for item in repo.get_all()}
    assert [(saved[key].category, saved[key].category_origin) for key in "abc"] == [
        ("Dining", "rule"), ("Dining", "rule"), ("Dining", "manual")]
    assert saved["d"].category == "Shopping"
    assert CategoryRule(keyword="blue bottle coffee", category="Dining") in repo.get_category_rules()


def test_merge_duplicate_keeps_lineage_and_refuses_bank_or_split_rows(tmp_path):
    import pytest
    repo = TransactionRepository(str(tmp_path / "merge.db"))
    keep, remove = transaction("keep", "Card"), transaction("remove", "Card")
    remove.description = "  merchant KEEP "
    bank = transaction("plaid:bank", "Card").model_copy(update={"description": "Merchant keep"})
    repo.insert_many([keep, remove, bank])
    with pytest.raises(ValueError, match="Bank"):
        repo.merge_duplicate("keep", "plaid:bank")

    assert repo.merge_duplicate("keep", "remove") == 1
    assert {item.id for item in repo.get_all()} == {"keep", "plaid:bank"}
    with pytest.raises(ValueError, match="not found"):
        repo.merge_duplicate("keep", "remove")


def test_clear_sample_removes_only_reserved_accounts_and_unchanged_plans(tmp_path):
    repo = TransactionRepository(str(tmp_path / "sample.db"))
    repo.insert_many([transaction("s", "Sample Checking · demo"), transaction("r", "Checking")])
    repo.split_transaction("s", "Groceries", 40.0, "Dining", 60.0)
    repo.upsert_budget(Budget(category="Groceries", monthly_limit=400))
    repo.upsert_budget(Budget(category="Dining", monthly_limit=300))
    repo.save_goal(SavingsGoal(name="Sample trip", target_amount=1500))
    repo.set_setting("sample_data", "{}")

    removed = repo.clear_sample(["Sample Checking · demo"],
                                budgets=[{"category": "Groceries", "monthly_limit": "400.00"},
                                         {"category": "Dining", "monthly_limit": "250.00"}],
                                goals=[{"name": "Sample trip", "target_amount": "1500.00"}])

    assert removed == 1
    assert [item.id for item in repo.get_all()] == ["r"]
    assert [item.category for item in repo.get_budgets()] == ["Dining"]
    assert repo.get_goals() == [] and repo.get_setting("sample_data") is None


def test_json_settings_round_trip_and_fall_back_on_bad_values(tmp_path):
    import pytest
    from src.services.data_service import DataService
    repo = TransactionRepository(str(tmp_path / "settings.db"))
    service = DataService(repo)
    assert service.get_json_setting("dismissed", []) == []
    service.set_json_setting("dismissed", [["a", "b"]])
    assert service.get_json_setting("dismissed", []) == [["a", "b"]]
    repo.set_setting("dismissed", "{not json")
    assert service.get_json_setting("dismissed", []) == []
    assert service.get_json_setting("dismissed", {}) == {}
    with pytest.raises(ValueError):
        service.set_json_setting("big", "x" * 20_000)


def test_notes_tags_and_manual_entries_round_trip_through_backup(tmp_path):
    import pytest
    repo = TransactionRepository(str(tmp_path / "notes.db"))
    repo.insert_many([transaction("a", "Card")])
    repo.split_transaction("a", "Travel", 60, "Dining", 40)
    split_part = repo.get_all()[0].id
    repo.annotate(split_part, " Conference ", [" Work ", "work", "Trip"])
    manual = Transaction(id="manual:cash1", date=date(2026, 8, 2), description="Market", amount="-12.50",
                         category="Groceries", account_name="Cash")
    repo.add_manual_transaction(manual, tags=["market"])

    saved = {item.id: item for item in repo.get_canonical_all()}
    assert (saved["a"].notes, saved["a"].tags) == ("Conference", ["trip", "work"])
    assert saved["manual:cash1"].category_origin == "manual" and saved["manual:cash1"].tags == ["market"]
    assert {item.id for item in repo.get_filtered({"tags": ["trip"]})} == {item.id for item in repo.get_all() if item.source_transaction_id == "a"}
    assert "notes" not in saved["a"].model_dump()

    restored = TransactionRepository(str(tmp_path / "restored.db"))
    restored.restore_backup(repo.export_backup().encode())
    assert {item.id: item.tags for item in restored.get_canonical_all()} == {"a": ["trip", "work"], "manual:cash1": ["market"]}

    with pytest.raises(ValueError):
        repo.annotate("a", "", ["x" * 41])
    with pytest.raises(ValueError):
        repo.delete_manual_transaction("a")
    repo.delete_manual_transaction("manual:cash1")
    repo.annotate("a", "", [])
    assert [(item.id, item.notes, item.tags) for item in repo.get_canonical_all()] == [("a", None, [])]
