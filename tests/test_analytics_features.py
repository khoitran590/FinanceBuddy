from datetime import date

from src.domain.models import Transaction
from src.services.analytics import AnalyticsService


def make_transaction(
    identifier: str,
    when: date,
    amount: float,
    description: str = "Merchant",
    category: str = "Shopping",
    account_type: str = "Checking",
) -> Transaction:
    return Transaction(
        id=identifier,
        date=when,
        description=description,
        amount=amount,
        category=category,
        account_name="Primary",
        account_type=account_type,
    )


def test_credit_card_dashboard_uses_card_language():
    transactions = [
        make_transaction("1", date(2026, 1, 1), 200.0, account_type="Credit Card"),
        make_transaction("2", date(2026, 1, 2), -500.0, account_type="Credit Card"),
    ]

    metrics = AnalyticsService.dashboard_metrics(transactions)

    assert [item["label"] for item in metrics] == [
        "Payments & credits",
        "Purchases & fees",
        "Balance change",
        "Transactions",
    ]
    assert metrics[2]["value"] == 300.0


def test_filters_recurring_anomalies_and_forecast():
    transactions = [
        make_transaction("1", date(2026, 1, 1), 2000.0, "Payroll", "Salary/Income"),
        make_transaction("2", date(2026, 1, 5), -15.0, "Streaming"),
        make_transaction("3", date(2026, 2, 1), 2000.0, "Payroll", "Salary/Income"),
        make_transaction("4", date(2026, 2, 5), -15.0, "Streaming"),
        make_transaction("5", date(2026, 2, 10), -900.0, "Large purchase"),
        make_transaction("6", date(2026, 2, 12), -25.0, "Lunch", "Dining"),
    ]

    filtered = AnalyticsService.filter_transactions(
        transactions,
        date(2026, 2, 1),
        date(2026, 2, 28),
        ["Primary"],
        ["Dining"],
        ["Expense / purchase"],
        "lunch",
        20.0,
    )

    assert [item.id for item in filtered] == ["6"]
    assert AnalyticsService.recurring_expenses(transactions)[0]["merchant"] == "Streaming"
    assert AnalyticsService.unusual_expenses(transactions)[0].description == "Large purchase"
    assert AnalyticsService.cash_flow_forecast(transactions)["months"] == 2
