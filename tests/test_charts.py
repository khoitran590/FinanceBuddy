from datetime import date

from src.ui.charts import make_category_donut_chart, make_profit_loss_line_chart


def test_donut_chart_has_center_total_and_interactive_slice_details():
    figure = make_category_donut_chart({"Housing": 1200.0, "Dining": 300.0})

    assert figure.data[0].hole == 0.58
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
