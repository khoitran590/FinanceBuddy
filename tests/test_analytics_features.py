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
    # February's history ends on the 12th, so only complete January feeds the estimate.
    forecast = AnalyticsService.cash_flow_forecast(transactions)
    assert forecast["months"] == 1
    assert forecast["recurring"] == 15.0


def test_empty_account_filter_clears_dashboard_and_cash_flow_accumulates():
    transactions = [
        make_transaction("1", date(2026, 1, 1), 1000.0, "Payroll", "Salary/Income"),
        make_transaction("2", date(2026, 1, 2), -250.0, "Rent", "Housing"),
    ]

    filtered = AnalyticsService.filter_transactions(
        transactions,
        date(2026, 1, 1),
        date(2026, 1, 31),
        [],
        ["Salary/Income", "Housing"],
        ["Income / credit", "Expense / purchase"],
    )

    assert filtered == []
    assert AnalyticsService.cumulative_cash_flow(transactions) == [
        {"date": date(2026, 1, 1), "pnl": 1000.0, "daily_change": 1000.0},
        {"date": date(2026, 1, 2), "pnl": 750.0, "daily_change": -250.0},
    ]


def test_history_periods_cover_saved_dates_and_mark_partial_ends():
    months = AnalyticsService.history_periods(date(2026, 7, 10), date(2026, 9, 29), "Month")
    assert [(item["label"], item["partial"]) for item in months] == [
        ("Jul 2026", True), ("Aug 2026", False), ("Sep 2026", False)]
    assert months[1]["start"] == date(2026, 8, 1) and months[1]["end"] == date(2026, 8, 31)
    quarters = AnalyticsService.history_periods(date(2026, 2, 1), date(2026, 10, 2), "Quarter")
    assert [(item["label"], item["partial"]) for item in quarters] == [
        ("Q1 2026", True), ("Q2 2026", False), ("Q3 2026", False), ("Q4 2026", True)]


def test_budget_suggestions_use_complete_months_and_skip_transfers():
    from decimal import Decimal
    from src.domain.models import Budget

    def spend(day, amount, category):
        return Transaction(id=f"{day}{category}{amount}", date=day, description=category, amount=amount,
                           category=category, account_name="Card")

    rows = [spend(date(2026, 6, 1), "-1.00", "Dining"), spend(date(2026, 9, 30), "-1.00", "Shopping")]
    rows += [spend(date(2026, month, 10), amount, "Dining")
             for month, amount in ((6, "-100.00"), (7, "-50.00"), (8, "-25.01"), (9, "-40.00"))]
    rows += [spend(date(2026, 9, 12), "20.00", "Dining"), spend(date(2026, 9, 5), "-300.00", "Transfers"),
             spend(date(2026, 9, 6), "-12.50", "Shopping"), spend(date(2026, 10, 1), "-5.00", "Dining")]
    history = AnalyticsService.category_spend_history(rows, "Dining")
    # October is partial; July-September are the last three complete months.
    assert history == {"last_month": Decimal("40.00"), "last_label": "Sep 2026",
                       "average": Decimal("38.34"), "months": 3}
    assert AnalyticsService.category_spend_history([], "Dining") is None
    unbudgeted = AnalyticsService.unbudgeted_spending([Budget(category="Dining", monthly_limit=100)], rows, "2026-09")
    assert unbudgeted == [{"category": "Shopping", "spent": Decimal("13.50")}]


def test_dismissed_merchants_leave_recurring_forecast_and_timeline():
    from decimal import Decimal

    def row(day, description, amount, category):
        return Transaction(id=f"{description}{day}", date=day, description=description, amount=amount,
                           category=category, account_name="Checking")

    rows = []
    for month in (6, 7, 8, 9):
        rows += [row(date(2026, month, 3), "STREAMFLIX", "-15.49", "Entertainment"),
                 row(date(2026, month, 10), "CITY GYM", "-40.00", "Health"),
                 row(date(2026, month, 1), "ACME PAYROLL", "2000.00", "Salary/Income"),
                 row(date(2026, month, 15), "ACME PAYROLL", "2000.01", "Salary/Income")]
    assert {item["merchant"] for item in AnalyticsService.recurring_expenses(rows)} == {"STREAMFLIX", "CITY GYM"}
    kept = AnalyticsService.recurring_expenses(rows, excluded={"city gym"})
    assert [item["merchant"] for item in kept] == ["STREAMFLIX"]
    assert AnalyticsService.cash_flow_forecast(rows, excluded={"city gym"})["recurring"] == Decimal("15.49")

    income = AnalyticsService.income_summary(rows)
    timeline = AnalyticsService.cash_flow_timeline(kept, income, date(2026, 10, 1), days=10,
                                                   starting_balance=Decimal("100.00"))
    assert [(event["date"].day, event["label"], event["running"]) for event in timeline] == [
        (1, "Paycheck", Decimal("2100.01")), (3, "STREAMFLIX", Decimal("2084.52"))]


def test_duplicates_ignore_split_parts_other_accounts_and_dismissed_groups():
    from uuid import uuid4
    from src.domain.allocations import project_transactions
    from src.domain.models import TransactionAllocation

    def row(identifier, account_id=None, **extra):
        return Transaction(id=identifier, date=date(2026, 10, 1), description="Store", amount=-100,
                           account_name="Card", account_id=account_id, **extra)

    split = row("split", allocations=[TransactionAllocation(amount=-50, category="Dining"),
                                      TransactionAllocation(amount=-50, category="Dining")])
    same_label_a, same_label_b = uuid4(), uuid4()
    rows = project_transactions([split, row("a", same_label_a), row("b", same_label_b), row("c", same_label_a)])
    groups = AnalyticsService.duplicate_candidates(rows)
    assert [[item.id for item in group] for group in groups] == [["a", "c"]]
    assert AnalyticsService.duplicate_candidates(rows, dismissed={AnalyticsService.duplicate_group_key(groups[0])}) == []
