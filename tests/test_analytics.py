from datetime import date

from src.domain.models import Transaction
from src.services.analytics import AnalyticsService


def test_analytics_summary_and_breakdowns():
    transactions = [
        Transaction(id="1", date=date(2026, 1, 1), description="Pay", amount=2500, account_name="Checking"),
        Transaction(id="2", date=date(2026, 1, 2), description="Rent", amount=-1200, category="Housing", account_name="Checking"),
        Transaction(id="3", date=date(2026, 2, 2), description="Food", amount=-100, category="Dining", account_name="Checking"),
    ]

    summary = AnalyticsService.calculate_summary(transactions)

    assert summary == {
        "inflow": 2500,
        "outflow": 1300,
        "net_savings": 1200,
        "savings_rate": 48.0,
    }
    assert AnalyticsService.category_expenses(transactions) == {"Housing": 1200, "Dining": 100}
    assert AnalyticsService.monthly_breakdown(transactions) == [
        {"month": "2026-01", "inflow": 2500.0, "outflow": 1200.0},
        {"month": "2026-02", "inflow": 0.0, "outflow": 100.0},
    ]


def test_category_summary_and_period_comparison():
    first = [
        Transaction(id="1", date=date(2026, 1, 1), description="Pay", amount=1000, account_name="Checking"),
        Transaction(id="2", date=date(2026, 1, 2), description="Rent", amount=-600, category="Housing", account_name="Checking"),
        Transaction(id="3", date=date(2026, 1, 3), description="Food", amount=-200, category="Dining", account_name="Checking"),
    ]
    second = [
        Transaction(id="4", date=date(2026, 2, 1), description="Pay", amount=1000, account_name="Checking"),
        Transaction(id="5", date=date(2026, 2, 2), description="Rent", amount=-600, category="Housing", account_name="Checking"),
        Transaction(id="6", date=date(2026, 2, 3), description="Food", amount=-100, category="Dining", account_name="Checking"),
    ]

    categories = AnalyticsService.category_summary(first)
    comparisons = AnalyticsService.compare_periods(
        [
            {"label": "January", "transactions": first},
            {"label": "February", "transactions": second},
        ]
    )

    assert categories[0]["category"] == "Housing"
    assert categories[0]["share"] == 75.0
    assert comparisons[0]["status"] == "Baseline"
    assert comparisons[1]["status"] == "Improved"
    assert comparisons[1]["outflow_delta"] == -100
