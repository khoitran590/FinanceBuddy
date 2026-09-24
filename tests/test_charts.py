from datetime import date

from src.ui.charts import make_category_donut_chart, make_profit_loss_line_chart


def test_donut_chart_has_center_total_and_interactive_slice_details():
    figure = make_category_donut_chart({"Housing": 1200.0, "Dining": 300.0})

    assert figure.data[0].hole == 0.5
    assert figure.layout.height == 560
    assert figure.layout.legend.font.size == 14
    assert "$1,500" in figure.layout.annotations[0].text
    assert "%{percent}" in figure.data[0].hovertemplate


def test_profit_loss_chart_connects_sign_changes_at_zero():
    figure = make_profit_loss_line_chart(
        [
            {"date": date(2026, 1, 1), "pnl": 100.0, "daily_change": 100.0},
            {"date": date(2026, 1, 2), "pnl": -50.0, "daily_change": -150.0},
        ]
    )

    assert [trace.name for trace in figure.data] == ["Gain", "Loss"]
    assert 0.0 in list(figure.data[0].y)
    assert 0.0 in list(figure.data[1].y)
    assert figure.layout.shapes[0].y0 == 0


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
    labels = list(saved.data[0].node.label)
    assert "Saved" in labels
    assert saved.data[0].link.value[labels.index("Saved") - 1] == 300.0

    deficit = make_cash_flow_sankey({"Salary/Income": 100.0}, {"Housing": 600.0})
    assert "Drawn from savings" in list(deficit.data[0].node.label)


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
