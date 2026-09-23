from datetime import date, timedelta

import pytest

from src.domain.models import Budget, SavingsGoal, Transaction
from src.services.analytics import AnalyticsService, clean_merchant


def tx(identifier, when, amount, description="Merchant", category="Shopping",
       account="Checking", account_type="Checking", **extra):
    return Transaction(
        id=identifier, date=when, description=description, amount=amount,
        category=category, account_name=account, account_type=account_type, **extra,
    )


def monthly(prefix, day, amount, description, category, months, start=(2026, 1), **extra):
    year, month = start
    rows = []
    for offset in range(months):
        index = year * 12 + month - 1 + offset
        rows.append(tx(f"{prefix}{offset}", date(index // 12, index % 12 + 1, day), amount,
                       description, category, **extra))
    return rows


def test_card_payments_and_transfers_are_not_counted_as_spending():
    transactions = [
        tx("pay", date(2026, 3, 1), 3000.0, "Payroll", "Salary/Income"),
        tx("card-buy", date(2026, 3, 5), -400.0, "Store", "Shopping", "Card", "Credit Card"),
        tx("card-pay-out", date(2026, 3, 20), -400.0, "Payment to Chase card", "Credit Card Payments"),
        tx("card-pay-in", date(2026, 3, 20), 400.0, "Payment Thank You", "Credit Card Payments", "Card", "Credit Card"),
        tx("savings", date(2026, 3, 21), -500.0, "Transfer to savings", "Transfers"),
    ]

    metrics = {item["label"]: item for item in AnalyticsService.dashboard_metrics(transactions)}

    assert metrics["Money in"]["value"] == 3000.0
    assert metrics["Spending"]["value"] == 400.0
    assert metrics["Net cash flow"]["value"] == 2600.0
    assert metrics["Moved between accounts"]["value"] == 900.0
    included = {
        item["label"]: item["value"]
        for item in AnalyticsService.dashboard_metrics(transactions, include_transfers=True)
    }
    assert included["Spending"] == 1300.0
    assert "Moved between accounts" not in included


def test_metrics_carry_change_from_previous_period_with_direction_colors():
    current = [tx("1", date(2026, 9, 1), 1000.0, "Pay", "Salary/Income"), tx("2", date(2026, 9, 2), -300.0)]
    previous = [tx("3", date(2026, 8, 1), 1000.0, "Pay", "Salary/Income"), tx("4", date(2026, 8, 2), -200.0)]

    metrics = {item["label"]: item for item in AnalyticsService.dashboard_metrics(current, previous)}

    assert metrics["Spending"]["delta"] == 100.0
    assert metrics["Spending"]["delta_color"] == "inverse"
    assert metrics["Net savings"]["delta"] == -100.0
    assert metrics["Net savings"]["delta_color"] == "normal"
    assert metrics["Savings rate"]["delta"] == pytest.approx(-10.0)


def test_previous_period_aligns_months_and_otherwise_uses_equal_length():
    assert AnalyticsService.previous_period(date(2026, 9, 1), date(2026, 9, 22)) == (date(2026, 8, 1), date(2026, 8, 22))
    assert AnalyticsService.previous_period(date(2026, 8, 1), date(2026, 8, 31)) == (date(2026, 7, 1), date(2026, 7, 31))
    assert AnalyticsService.previous_period(date(2026, 3, 1), date(2026, 3, 31)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert AnalyticsService.previous_period(date(2026, 9, 10), date(2026, 9, 19)) == (date(2026, 8, 31), date(2026, 9, 9))


@pytest.mark.parametrize(
    "preset, expected",
    [
        ("This month", (date(2026, 9, 1), date(2026, 9, 21))),
        ("Last month", (date(2026, 8, 1), date(2026, 8, 31))),
        ("Last 30 days", (date(2026, 8, 23), date(2026, 9, 21))),
        ("Last 3 months", (date(2026, 7, 1), date(2026, 9, 21))),
        ("Year to date", (date(2026, 3, 1), date(2026, 9, 21))),
        ("All time", (date(2026, 3, 1), date(2026, 9, 21))),
    ],
)
def test_quick_date_ranges_count_back_from_latest_activity(preset, expected):
    assert AnalyticsService.preset_range(preset, date(2026, 3, 1), date(2026, 9, 21)) == expected


def test_partial_month_is_compared_with_same_days_of_previous_month():
    transactions = [
        tx("aug-early", date(2026, 8, 3), -100.0, category="Dining"),
        tx("aug-late", date(2026, 8, 28), -900.0, category="Dining"),
        tx("sep", date(2026, 9, 4), -150.0, category="Dining"),
        tx("start", date(2026, 8, 1), 10.0, category="Salary/Income"),
    ]

    change = AnalyticsService.category_changes(transactions)[0]

    assert change["change"] == 50.0
    assert (change["current_month"], change["previous_month"]) == ("Sep 1–4", "Aug 1–4")


def test_merchant_names_are_cleaned_for_grouping():
    assert clean_merchant("SQ *BLUE BOTTLE 4821") == "BLUE BOTTLE"
    assert clean_merchant("AMZN Mktp US*2K3L45") == "Amazon"
    assert clean_merchant("TRADER JOE'S #552") == "TRADER JOE'S"
    assert clean_merchant("Coffee (split 1)") == "Coffee"

    summary = AnalyticsService.merchant_summary([
        tx("1", date(2026, 9, 1), -10.0, "SQ *BLUE BOTTLE 1111", "Dining"),
        tx("2", date(2026, 9, 2), -20.0, "SQ *BLUE BOTTLE 2222", "Dining"),
        tx("3", date(2026, 9, 3), -5.0, "Kiosk", "Shopping", merchant_name="Corner Kiosk"),
    ])
    assert summary[0]["merchant"] == "BLUE BOTTLE"
    assert (summary[0]["visits"], summary[0]["average"]) == (2, 15.0)
    assert summary[1]["merchant"] == "Corner Kiosk"


def test_recurring_detects_price_increase_and_variable_bills():
    netflix = [
        tx(f"n{index}", date(2026, 1 + index, 5), -(15.49 if index < 4 else 17.99), "NETFLIX.COM", "Subscriptions")
        for index in range(7)
    ]
    electric = [
        tx(f"e{index}", date(2026, 1 + index, 12), -(80.0 + index * 5), "Con Edison", "Utilities")
        for index in range(7)
    ]

    recurring = {item["merchant"]: item for item in AnalyticsService.recurring_expenses(netflix + electric)}

    assert recurring["Netflix"]["price_change"] == 2.5
    assert recurring["Netflix"]["previous_amount"] == 15.49
    assert recurring["Netflix"]["price_changed_on"] == date(2026, 5, 5)
    assert recurring["Netflix"]["next_date"] == date(2026, 8, 5)
    assert recurring["Netflix"]["annual_cost"] == pytest.approx(17.99 * 12)
    # A rising electric bill stays one series, but a varying bill has no single "price".
    assert recurring["Con Edison"]["frequency"] == 7
    assert recurring["Con Edison"]["price_change"] == 0.0


def test_recurring_ignores_habits_and_finds_fixed_membership_among_purchases():
    start = date(2026, 1, 5)
    coffee = [
        tx(f"c{week}", start + timedelta(days=7 * week), -round(4.0 + (week * 37 % 50) / 10, 2), "SQ *BLUE BOTTLE", "Dining")
        for week in range(20)
    ]
    amazon = [tx(f"a{index}", date(2026, 1, 3 + index * 5), -(20.0 + index * 13), "AMZN Mktp", "Shopping") for index in range(5)]
    prime = monthly("p", 14, -14.99, "Amazon Prime", "Subscriptions", 5)

    recurring = AnalyticsService.recurring_expenses(coffee + amazon + prime)

    assert [(item["merchant"], item["amount"]) for item in recurring] == [("Amazon", 14.99)]


def test_stopped_subscription_is_marked_inactive():
    old = monthly("old", 3, -9.99, "Old Streaming", "Subscriptions", 3)
    recent = [tx("latest", date(2026, 9, 1), -30.0, "Groceries", "Groceries")]

    item = AnalyticsService.recurring_expenses(old + recent)[0]

    assert item["active"] is False


def test_forecast_adds_expected_recurring_charges_to_variable_spending():
    transactions = (
        monthly("rent", 1, -1000.0, "Rent", "Housing", 3)
        + monthly("food-a", 10, -200.0, "Market", "Groceries", 3)
        + [tx("extra", date(2026, 2, 20), -100.0, "Market", "Groceries"), tx("end", date(2026, 3, 31), -1.0, "Gum")]
    )

    forecast = AnalyticsService.cash_flow_forecast(transactions)

    assert forecast["months"] == 3
    assert forecast["recurring"] == 1000.0 + 200.0
    assert forecast["outflow_low"] == pytest.approx(1200.0 + 0.0)
    assert forecast["outflow_high"] == pytest.approx(1200.0 + 100.0)


def test_income_summary_detects_twice_monthly_pay_and_next_payday():
    pay = monthly("a", 1, 2600.0, "ACME PAYROLL", "Salary/Income", 6) + monthly(
        "b", 15, 2600.0, "ACME PAYROLL", "Salary/Income", 6
    )
    pay.append(tx("end", date(2026, 6, 30), -5.0))

    income = AnalyticsService.income_summary(pay)

    assert income["cadence"] == "Twice a month"
    assert income["next_pay_date"] == date(2026, 7, 1)
    assert income["typical_paycheck"] == 2600.0
    assert income["stability"] == "Stable"
    assert income["average_monthly"] == 5200.0


def test_weekday_and_time_of_month_spending_are_averaged_per_day():
    # Two full weeks (Mon Sep 7 – Sun Sep 20): each weekday occurs twice.
    transactions = [
        tx("sat", date(2026, 9, 12), -100.0),
        tx("mon", date(2026, 9, 7), -20.0),
        tx("mon2", date(2026, 9, 14), -20.0),
    ]
    weekday = AnalyticsService.weekday_spending(transactions, date(2026, 9, 7), date(2026, 9, 20))
    by_day = {row["day"]: row for row in weekday}

    assert by_day["Saturday"]["average_per_day"] == 50.0
    assert by_day["Monday"]["average_per_day"] == 20.0
    split = AnalyticsService.weekend_vs_weekday(weekday)
    assert split["weekend"] == 25.0
    assert split["weekday"] == 4.0

    buckets = AnalyticsService.week_of_month_spending(transactions, date(2026, 9, 1), date(2026, 9, 30))
    assert [bucket["total"] for bucket in buckets] == [20.0, 120.0, 0.0, 0.0]
    assert buckets[3]["average_per_day"] == 0.0


def test_daily_spending_is_zero_filled_with_rolling_average():
    transactions = [tx("1", date(2026, 9, 1), -70.0), tx("2", date(2026, 9, 8), -7.0)]

    daily = AnalyticsService.daily_spending(transactions)
    pace = AnalyticsService.spending_pace(transactions)

    assert len(daily) == 8
    assert daily[6]["rolling_7"] == 10.0
    assert daily[7]["rolling_7"] == 1.0
    assert pace["average_daily"] == pytest.approx(77.0 / 8)


def test_fixed_and_flexible_spending_split():
    split = AnalyticsService.fixed_vs_flexible([
        tx("rent", date(2026, 9, 1), -1500.0, category="Housing"),
        tx("food", date(2026, 9, 2), -500.0, category="Dining"),
    ])

    assert (split["fixed"], split["flexible"], split["fixed_share"]) == (1500.0, 500.0, 75.0)


def test_budget_status_projects_pace_but_not_one_time_fixed_costs():
    transactions = [
        tx("dining", date(2026, 9, 5), -150.0, category="Dining"),
        tx("rent", date(2026, 9, 1), -1500.0, category="Housing"),
        tx("fun", date(2026, 9, 9), -120.0, category="Entertainment"),
    ]
    budgets = [
        Budget(category="Dining", monthly_limit=300),
        Budget(category="Housing", monthly_limit=1500),
        Budget(category="Entertainment", monthly_limit=100),
    ]

    rows = {row["category"]: row for row in AnalyticsService.budget_status(budgets, transactions, "2026-09", date(2026, 9, 10))}

    assert rows["Dining"]["projected"] == 450.0
    assert rows["Dining"]["status"] == "Projected to go over"
    assert rows["Dining"]["daily_allowance"] == pytest.approx(150.0 / 20)
    assert rows["Housing"]["projected"] == 1500.0
    assert rows["Housing"]["status"] == "On track"
    assert rows["Entertainment"]["status"] == "Over budget"
    past = AnalyticsService.budget_status(budgets, transactions, "2026-09", date(2026, 10, 3))
    assert past[0]["status"] == "Under budget"


def test_budget_history_counts_months_on_budget():
    transactions = [
        tx("a", date(2026, 7, 3), -80.0, category="Dining"),
        tx("b", date(2026, 8, 3), -130.0, category="Dining"),
    ]

    history = AnalyticsService.budget_history([Budget(category="Dining", monthly_limit=100)], transactions, ["2026-07", "2026-08"])

    summary = history["summary"][0]
    assert (summary["months_on_budget"], summary["highest_month"]) == (1, "2026-08")
    assert [cell["ratio"] for cell in history["cells"]] == [80.0, 130.0]


def test_goal_plan_reports_required_monthly_savings_and_projection():
    goal = SavingsGoal(name="Trip", target_amount=3000, current_amount=1000, target_date=date(2027, 1, 1))

    plan = AnalyticsService.goal_plan(goal, date(2026, 9, 1), average_monthly_savings=400.0)

    assert plan["remaining"] == 2000.0
    assert plan["required_monthly"] == pytest.approx(2000.0 / (122 / 30.44))
    assert plan["on_track"] is False
    assert plan["projected_date"] == date(2026, 9, 1) + timedelta(days=round(5 * 30.44))
    assert AnalyticsService.goal_plan(
        SavingsGoal(name="Done", target_amount=10, current_amount=10), date(2026, 9, 1), None
    )["complete"] is True


def test_average_monthly_savings_uses_complete_months_without_transfers():
    transactions = (
        monthly("pay", 1, 3000.0, "Payroll", "Salary/Income", 2)
        + monthly("rent", 2, -1000.0, "Rent", "Housing", 2)
        + monthly("save", 3, -1500.0, "Transfer to savings", "Transfers", 2)
        + [tx("end", date(2026, 2, 28), -0.01)]
    )

    assert AnalyticsService.average_monthly_savings(transactions) == pytest.approx(1999.995)


def test_balance_overview_net_worth_utilization_and_history():
    accounts = [
        {"type": "depository", "current_balance": 5000.0, "available_balance": 4500.0},
        {"type": "investment", "current_balance": 10000.0},
        {"type": "credit", "name": "Card", "current_balance": 600.0, "credit_limit": 2000.0},
        {"type": "loan", "current_balance": 8000.0},
        {"type": "depository", "current_balance": None},
    ]

    overview = AnalyticsService.balance_overview(accounts)

    assert overview["net_worth"] == 15000.0 - 8600.0
    assert overview["cash_available"] == 4500.0
    assert overview["credit_utilization"] == 30.0
    assert overview["cards"][0]["rating"] == "High"
    assert overview["accounts"] == 4

    history = AnalyticsService.net_worth_history([
        {"account_id": "c", "as_of": "2026-09-01", "type": "depository", "current_balance": 100.0},
        {"account_id": "k", "as_of": "2026-09-01", "type": "credit", "current_balance": 40.0},
        {"account_id": "c", "as_of": "2026-09-02", "type": "depository", "current_balance": 150.0},
    ])
    assert [point["net_worth"] for point in history] == [60.0, 110.0]


def test_safe_to_spend_subtracts_recurring_charges_before_payday():
    recurring = [
        {"merchant": "Rent", "amount": 1000.0, "next_date": date(2026, 9, 25), "cadence": "Monthly", "interval_days": 30, "active": True},
        {"merchant": "Gym", "amount": 10.0, "next_date": date(2026, 9, 12), "cadence": "Weekly", "interval_days": 7, "active": True},
        {"merchant": "Old", "amount": 99.0, "next_date": date(2026, 9, 21), "cadence": "Monthly", "interval_days": 30, "active": False},
    ]

    safe = AnalyticsService.safe_to_spend(2000.0, recurring, date(2026, 9, 30), date(2026, 9, 20))

    assert [charge["merchant"] for charge in safe["charges"]] == ["Rent", "Gym"]
    assert safe["committed"] == 1010.0
    assert safe["safe_to_spend"] == 990.0
    assert safe["until_payday"] is True


def test_insights_describe_price_increases_growth_and_subscriptions():
    transactions = [
        tx(f"n{index}", date(2026, 1 + index, 5), -(15.49 if index < 4 else 17.99), "NETFLIX.COM", "Subscriptions")
        for index in range(8)
    ] + [
        tx("aug", date(2026, 7, 10), -100.0, "Market", "Groceries"),
        tx("sep", date(2026, 8, 10), -300.0, "Market", "Groceries"),
        tx("end", date(2026, 8, 31), -1.0, "Gum"),
    ]

    insights = AnalyticsService.insights(transactions)

    assert insights[0] == "Netflix went up from $15.49 to $17.99 (monthly)."
    assert insights[1] == "Groceries spending is up $200 (Aug 2026 vs Jul 2026)."
    assert insights[2] == "1 subscription or membership costs about $216 a year."


def test_provider_detail_summaries():
    transactions = [
        tx("1", date(2026, 9, 1), -30.0, category="Dining", subcategory="Coffee", payment_channel="in store", location="Oakland, CA"),
        tx("2", date(2026, 9, 2), -70.0, category="Dining", subcategory="Restaurant", payment_channel="online"),
        tx("3", date(2026, 9, 3), -5.0),
    ]

    assert AnalyticsService.subcategory_summary(transactions)[0] == {"subcategory": "Restaurant", "amount": 70.0, "transactions": 1}
    channels = {row["channel"]: row for row in AnalyticsService.payment_channel_summary(transactions)}
    assert channels["Online"]["share"] == 70.0
    assert AnalyticsService.location_summary(transactions) == [{"location": "Oakland, CA", "amount": 30.0, "transactions": 1}]
