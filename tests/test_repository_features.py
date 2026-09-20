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
