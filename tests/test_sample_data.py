from datetime import date
from decimal import Decimal

from src.domain.models import Budget, CategoryRule, Transaction
from src.repositories.transaction_repo import TransactionRepository
from src.services.analytics import AnalyticsService
from src.services.data_service import DataService
from src.services.sample_data import sample_statements

TODAY = date(2026, 10, 29)


def test_sample_statements_are_repeatable_and_end_today():
    assert sample_statements(TODAY) == sample_statements(TODAY)
    checking = sample_statements(TODAY)[0][2].decode().splitlines()
    assert checking[0] == "Date,Description,Amount" and checking[1].startswith("2026-05-01,")
    assert max(line.split(",")[0] for line in checking[1:]) <= TODAY.isoformat()


def test_load_then_clear_sample_data_leaves_real_data(tmp_path):
    repo = TransactionRepository(str(tmp_path / "sample.db"))
    service = DataService(repo)
    real = Transaction(id="real", date=date(2026, 10, 1), description="Real store", amount=-10,
                       category="Shopping", account_name="Checking")
    repo.insert_many([real])
    repo.upsert_budget(Budget(category="Dining", monthly_limit=75))

    marker = service.load_sample_data(TODAY)
    rows = repo.get_all()
    assert {row.account_name for row in rows} == {"Checking", "Sample Checking · demo", "Sample Card · demo"}
    assert all(isinstance(row.amount, Decimal) for row in rows)
    sample = [row for row in rows if row.account_name.endswith("· demo")]
    assert {item["merchant"] for item in AnalyticsService.recurring_expenses(sample) if item["active"]} >= {"STREAMFLIX", "IRONWORKS GYM"}
    assert AnalyticsService.income_summary(sample)["cadence"] == "Twice a month"
    assert any(row.category == "Uncategorized" for row in sample)
    # The person's own Dining budget is kept, not replaced.
    assert [item["category"] for item in marker["budgets"]] == ["Groceries", "Transportation"]
    assert Budget(category="Dining", monthly_limit=75) in repo.get_budgets()

    # Edits and splits while exploring must not block clearing.
    noodle = next(row for row in sample if "NOODLE" in row.description)
    repo.split_transaction(noodle.id, "Dining", 10, "Groceries", abs(noodle.amount) - 10)
    repo.upsert_category_rule(CategoryRule(keyword="streamflix", category="Subscriptions"))
    assert service.rules_added_since_sample() == ["streamflix"]
    assert service.clear_sample_data(remove_rules=True) == len(sample)

    assert [row.id for row in repo.get_all()] == ["real"]
    assert repo.get_budgets() == [Budget(category="Dining", monthly_limit=75)]
    assert repo.get_goals() == [] and repo.get_category_rules() == []
    assert service.sample_data() == {}
