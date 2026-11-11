from datetime import date, datetime
from decimal import Decimal

import pytest

from src.ui.charts import make_category_donut_chart, make_profit_loss_line_chart


def test_donut_chart_has_center_total_and_interactive_slice_details():
    figure = make_category_donut_chart({"Housing": 1200.0, "Dining": 300.0})

    assert figure.data[0].hole == 0.5
    assert figure.layout.height == 560
    assert figure.layout.legend.font.size == 14
    assert "$1,500" in figure.layout.annotations[0].text
    assert "%{percent}" in figure.data[0].hovertemplate


@pytest.mark.parametrize("number", [float, Decimal])
@pytest.mark.parametrize("direction", [1, -1])
def test_profit_loss_chart_connects_sign_changes_at_zero(number, direction):
    figure = make_profit_loss_line_chart(
        [
            {"date": date(2026, 1, 1), "pnl": number("100.00") * direction,
             "daily_change": number("100.00") * direction},
            {"date": date(2026, 1, 2), "pnl": number("-50.00") * direction,
             "daily_change": number("-150.00") * direction},
        ]
    )

    assert [trace.name for trace in figure.data] == ["Ahead", "Behind"]
    assert 0.0 in list(figure.data[0].y)
    assert 0.0 in list(figure.data[1].y)
    assert figure.data[0].x[1] == datetime(2026, 1, 1, 16)
    assert figure.data[1].x[1] == datetime(2026, 1, 1, 16)
    assert figure.layout.shapes[0].y0 == 0


def test_profit_loss_chart_renders_exact_money_analytics_without_changing_amounts():
    from src.domain.models import Transaction
    from src.services.analytics import AnalyticsService

    transactions = [
        Transaction(id="first", date=date(2026, 1, 1), description="Refund", amount="0.10", account_name="Checking"),
        Transaction(id="second", date=date(2026, 1, 4), description="Purchase", amount="-0.30", account_name="Checking"),
        Transaction(id="third", date=date(2026, 1, 9), description="Refund", amount="0.50", account_name="Checking"),
    ]
    points = AnalyticsService.cumulative_cash_flow(transactions)
    original = [point.copy() for point in points]

    figure = make_profit_loss_line_chart(points)

    assert [point["pnl"] for point in points] == [Decimal("0.10"), Decimal("-0.20"), Decimal("0.30")]
    assert points == original
    for trace in figure.data:
        assert [day for day, amount in zip(trace.x, trace.y) if amount == 0] == [
            datetime(2026, 1, 2), datetime(2026, 1, 6),
        ]
    assert figure.to_json()


def test_new_charts_render_with_data_and_when_empty():
    from src.ui.charts import (
        make_budget_history_chart,
        make_category_bar_chart,
        make_daily_spending_chart,
        make_fixed_flexible_chart,
        make_income_chart,
        make_merchant_bar_chart,
        make_net_worth_chart,
        make_savings_rate_chart,
        make_week_of_month_chart,
        make_weekday_chart,
    )

    samples = [
        (make_savings_rate_chart, [{"month": "2026-08", "inflow": 100.0, "outflow": 50.0, "net": 50.0, "savings_rate": 50.0}]),
        (make_merchant_bar_chart, [{"merchant": "Cafe", "amount": 10.0, "visits": 2, "average": 5.0, "share": 100.0, "category": "Dining"}]),
        (make_weekday_chart, [{"day": "Monday", "total": 10.0, "transactions": 1, "days": 1, "average_per_day": 10.0}]),
        (make_week_of_month_chart, [{"bucket": "Days 1–7", "total": 10.0, "share": 100.0, "average_per_day": 1.4}]),
        (make_daily_spending_chart, [{"date": date(2026, 9, 1), "spend": 5.0, "rolling_7": 5.0}]),
        (make_fixed_flexible_chart, [{"month": "2026-09", "fixed": 10.0, "flexible": 5.0}]),
        (make_budget_history_chart, [{"category": "Dining", "month": "2026-09", "spent": 50.0, "limit": 100.0, "ratio": 50.0}]),
        (make_net_worth_chart, [{"date": date(2026, 9, 1), "assets": 10.0, "liabilities": 2.0, "net_worth": 8.0}]),
        (make_income_chart, [{"month": "2026-09", "income": 100.0}]),
        (make_category_bar_chart, [{"category": "Dining", "amount": 10.0, "transactions": 1, "share": 100.0, "average": 10.0}]),
    ]
    for builder, data in samples:
        assert builder(data).data
        assert builder([]).layout.title.text


def test_sankey_balances_income_spending_and_savings():
    from src.ui.charts import make_cash_flow_sankey

    saved = make_cash_flow_sankey({"Salary/Income": 1000.0}, {"Housing": 600.0, "Dining": 100.0})
    labels = list(saved.data[0].node.customdata)
    assert "Saved" in labels
    # Amounts are written on the nodes, so reading them doesn't depend on hovering.
    assert "Saved · $300" in list(saved.data[0].node.label)
    assert saved.data[0].link.value[labels.index("Saved") - 1] == 300.0

    deficit = make_cash_flow_sankey({"Salary/Income": 100.0}, {"Housing": 600.0})
    assert "Drawn from savings" in list(deficit.data[0].node.customdata)


def test_alternative_chart_styles_render_with_data_and_when_empty():
    from src.ui.charts import (
        make_category_treemap,
        make_category_trend_lines,
        make_fixed_flexible_chart,
        make_monthly_line_chart,
        make_monthly_net_chart,
    )

    monthly = [
        {"month": "2026-07", "inflow": 100.0, "outflow": 150.0},
        {"month": "2026-08", "inflow": 200.0, "outflow": 50.0},
    ]
    lines = make_monthly_line_chart(monthly)
    assert [trace.name for trace in lines.data] == ["Money in", "Money out"]
    net = make_monthly_net_chart(monthly)
    assert list(net.data[0].y) == [-50.0, 150.0]
    assert net.data[0].marker.color[0] != net.data[0].marker.color[1]

    treemap = make_category_treemap([
        {"category": "Housing", "amount": 900.0},
        {"category": "Dining", "amount": 100.0},
        {"category": "Refunds", "amount": 0.0},
    ])
    assert list(treemap.data[0].labels) == ["Housing", "Dining"]
    trend = make_category_trend_lines([
        {"month": "2026-07", "category": "Dining", "amount": 40.0},
        {"month": "2026-08", "category": "Dining", "amount": 60.0},
    ])
    assert trend.data[0].name == "Dining"
    assert make_fixed_flexible_chart(
        [{"month": "2026-08", "fixed": 10.0, "flexible": 5.0}], stacked=False
    ).layout.barmode == "group"

    for builder in (make_monthly_line_chart, make_monthly_net_chart, make_category_treemap, make_category_trend_lines):
        assert builder([]).layout.title.text


def test_category_labels_meet_normal_text_contrast_for_every_palette_fill():
    from src.ui.charts import CATEGORY_COLORS, make_category_treemap

    def luminance(color):
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    rows = [{'category': f'Category {index}', 'amount': 100} for index in range(len(CATEGORY_COLORS))]
    figures = [make_category_donut_chart({row['category']: row['amount'] for row in rows}),
               make_category_treemap(rows)]
    for figure in figures:
        trace = figure.data[0]
        for fill, label in zip(trace.marker.colors, trace.textfont.color):
            lighter, darker = sorted([luminance(fill), luminance(label)], reverse=True)
            assert (lighter + 0.05) / (darker + 0.05) >= 4.5


def test_cash_flow_timeline_chart_separates_paydays_and_bills():
    from datetime import date
    from decimal import Decimal
    from src.ui.charts import make_cash_flow_timeline

    figure = make_cash_flow_timeline([
        {"date": date(2026, 10, 1), "label": "Paycheck", "amount": Decimal("2000.00"), "kind": "Payday", "running": Decimal("2000.00")},
        {"date": date(2026, 10, 3), "label": "Rent", "amount": Decimal("-1500.00"), "kind": "Bill", "running": Decimal("500.00")},
    ])
    assert [trace.name for trace in figure.data] == ["Payday", "Bill", "Running balance"]
    assert list(figure.data[2].y) == [2000.0, 500.0]
    assert make_cash_flow_timeline([]).layout.title.text == "No bills or paydays expected"


def test_month_charts_use_one_axis_and_readable_month_labels():
    from src.ui.charts import make_monthly_bar_chart, make_savings_rate_chart

    monthly = [
        {"month": "2026-08", "inflow": 100.0, "outflow": 50.0, "net": 50.0, "savings_rate": 50.0},
        {"month": "2026-09", "inflow": 100.0, "outflow": 150.0, "net": -50.0, "savings_rate": -50.0},
    ]
    for figure in (make_monthly_bar_chart(monthly), make_savings_rate_chart(monthly)):
        # Months stay categories (not a date axis) and keep the raw key for click-to-filter.
        assert figure.layout.xaxis.type == "category"
        assert list(figure.layout.xaxis.ticktext) == ["Aug 2026", "Sep 2026"]
        assert list(figure.data[0].x) == ["2026-08", "2026-09"]
        assert "yaxis2" not in figure.layout
    rate = make_savings_rate_chart(monthly)
    assert list(rate.data[0].text) == ["50%", "-50%"]
    assert rate.data[0].marker.color[0] != rate.data[0].marker.color[1]


def test_daily_chart_keeps_ordinary_days_readable_next_to_a_rent_payment():
    from datetime import timedelta
    from src.ui.charts import make_daily_spending_chart

    days = [{"date": date(2026, 9, 1) + timedelta(days=index), "spend": 20.0 + index, "rolling_7": 25.0}
            for index in range(20)]
    assert make_daily_spending_chart(days).layout.yaxis.range is None
    days[3]["spend"] = 1650.0
    clipped = make_daily_spending_chart(days)
    assert clipped.layout.yaxis.range[1] < 200
    assert [note.text for note in clipped.layout.annotations] == ["▲ $1,650"]


def test_donut_folds_small_categories_into_other():
    figure = make_category_donut_chart({f"Category {index}": 100.0 - index for index in range(9)})
    assert len(figure.data[0].labels) == 6
    assert figure.data[0].labels[-1] == "Other"
    assert sum(figure.data[0].values) == sum(100.0 - index for index in range(9))


def test_heatmaps_label_cells_and_follow_the_theme():
    from src.ui.charts import make_budget_history_chart, make_category_heatmap

    rows = [{"month": "2026-08", "category": "Dining", "amount": 40.0},
            {"month": "2026-09", "category": "Housing", "amount": 900.0}]
    light, dark = make_category_heatmap(rows), make_category_heatmap(rows, dark=True)
    assert list(light.data[0].y) == ["Housing", "Dining"]
    assert [note.text for note in light.layout.annotations] == ["–", "$900", "$40", "–"]
    assert light.data[0].colorscale != dark.data[0].colorscale
    # Labels flip between black and white with the fill behind them.
    assert {note.font.color for note in light.layout.annotations} == {"#000000", "#FFFFFF"}
    budget = make_budget_history_chart(
        [{"category": "Dining", "month": "2026-09", "spent": 150.0, "limit": 100.0, "ratio": 150.0}])
    assert budget.layout.annotations[0].text == "150%"


def test_takeaways_say_what_each_chart_shows():
    from src.ui import charts

    monthly = [
        {"month": "2026-08", "inflow": 1000.0, "outflow": 400.0, "net": 600.0, "savings_rate": 60.0},
        {"month": "2026-09", "inflow": 1000.0, "outflow": 1200.0, "net": -200.0, "savings_rate": -20.0},
    ]
    assert charts.cash_flow_takeaway(monthly) == (
        "More came in than went out in 1 of 2 months. Best month: Aug 2026 (+$600).")
    assert charts.cash_flow_takeaway(monthly[1:]) == "In Sep 2026 you spent $200 more than came in."
    assert charts.savings_rate_takeaway(monthly) == (
        "In Sep 2026 you spent 20% more than you earned. Your average across 2 months is 20%.")
    assert charts.category_takeaway([{"category": "Housing", "amount": 750.0}, {"category": "Dining", "amount": 250.0}]) == (
        "Housing is your biggest category: $750, 75% of spending.")
    assert charts.running_total_takeaway([{"date": date(2026, 9, 1), "pnl": Decimal("-30")}]) == (
        "Across these dates, $30 more went out than came in.")
    assert charts.merchant_takeaway([{"merchant": "Cafe", "amount": 10.0, "share": 40.0}]).startswith("Cafe is where")
    assert charts.week_of_month_takeaway([
        {"bucket": "Days 1–7", "average_per_day": 90.0}, {"bucket": "Days 8–14", "average_per_day": 20.0},
    ]) == "Days 1–7 is your most expensive stretch, at $90 a day."
    assert charts.fixed_flexible_takeaway([{"month": "2026-09", "fixed": 75.0, "flexible": 25.0}]).startswith("75% of")
    comparisons = [{"label": "Aug 2026", "categories": {"Dining": 100.0, "Fuel": 50.0}},
                   {"label": "Sep 2026", "categories": {"Dining": 140.0, "Fuel": 20.0}}]
    assert charts.comparison_takeaway(comparisons) == "Biggest increase: Dining (+$40). Biggest drop: Fuel (-$30)."
    for empty in (charts.cash_flow_takeaway, charts.category_takeaway, charts.savings_rate_takeaway,
                  charts.running_total_takeaway, charts.merchant_takeaway, charts.week_of_month_takeaway,
                  charts.fixed_flexible_takeaway, charts.comparison_takeaway):
        assert empty([]) == ""
