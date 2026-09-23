from collections import defaultdict
from typing import Any, Dict, List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go


FINANCIAL_COLORS = {
    "income": "#38BDF8",
    "expense": "#FB923C",
    "savings": "#A78BFA",
    "category": "#60A5FA",
    "higher_spending": "#FB7185",
    "lower_spending": "#2DD4BF",
}
STATEMENT_COLORS = ["#60A5FA", "#2DD4BF", "#A78BFA"]
CATEGORY_COLORS = [
    "#60A5FA",
    "#2DD4BF",
    "#A78BFA",
    "#FB923C",
    "#FB7185",
    "#94A3B8",
    "#FACC15",
    "#34D399",
]


def _style_figure(figure):
    """Apply an accessible chart theme that blends into Streamlit's dark UI."""
    figure.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#E5E7EB", "size": 13},
        title={"font": {"color": "#F9FAFB", "size": 18}},
        legend={"font": {"color": "#E5E7EB", "size": 12}, "title_text": ""},
        margin={"l": 24, "r": 24, "t": 64, "b": 48},
        hoverlabel={"bgcolor": "#111827", "font_color": "#F9FAFB"},
    )
    figure.update_xaxes(
        gridcolor="#374151",
        zerolinecolor="#6B7280",
        tickfont={"color": "#D1D5DB"},
        title_font={"color": "#D1D5DB"},
    )
    figure.update_yaxes(
        gridcolor="#374151",
        zerolinecolor="#6B7280",
        tickfont={"color": "#D1D5DB"},
        title_font={"color": "#D1D5DB"},
    )
    return figure


def make_monthly_bar_chart(monthly_data: List[Dict[str, Any]]):
    if not monthly_data:
        return _style_figure(px.bar(title="No Monthly Data Available"))

    rows = []
    for item in monthly_data:
        rows.extend(
            [
                {"month": item["month"], "type": "Money in", "amount": item["inflow"]},
                {"month": item["month"], "type": "Money out", "amount": item["outflow"]},
            ]
        )

    figure = px.bar(
        pd.DataFrame(rows),
        x="month",
        y="amount",
        color="type",
        pattern_shape="type",
        barmode="group",
        text_auto="$.2s",
        title="Monthly cash flow",
        labels={"amount": "Amount ($)", "month": "Month"},
        category_orders={"type": ["Money in", "Money out"]},
        color_discrete_map={
            "Money in": FINANCIAL_COLORS["income"],
            "Money out": FINANCIAL_COLORS["expense"],
        },
        pattern_shape_map={"Money in": "", "Money out": "/"},
    )
    return _style_figure(figure)


def make_category_donut_chart(category_data: Dict[str, float]):
    if not category_data:
        return _style_figure(px.pie(title="No Expense Data Available"))

    rows = [{"category": category, "amount": amount} for category, amount in category_data.items()]
    total = sum(item["amount"] for item in rows)
    figure = go.Figure(
        go.Pie(
            labels=[item["category"] for item in rows],
            values=[item["amount"] for item in rows],
            hole=0.5,
            sort=True,
            direction="clockwise",
            marker={
                "colors": CATEGORY_COLORS,
                "line": {"color": "#0B0F14", "width": 2},
            },
            textinfo="percent",
            textposition="inside",
            insidetextorientation="horizontal",
            textfont={"size": 15, "color": "#0B0F14"},
            hovertemplate="<b>%{label}</b><br>$%{value:,.2f}<br>%{percent}<extra></extra>",
            domain={"x": [0, 1], "y": [0.12, 1]},
        )
    )
    figure.update_layout(
        title="Spending by category",
        annotations=[
            {
                "text": f"<span style='font-size:14px'>Total spent</span><br><b>${total:,.0f}</b>",
                "x": 0.5,
                "y": 0.56,
                "showarrow": False,
                "font": {"size": 24, "color": "#F8FAFC"},
            }
        ],
    )
    # Applied after the shared theme, which would otherwise reset the legend and margins.
    figure = _style_figure(figure)
    figure.update_layout(
        height=560,
        margin={"l": 8, "r": 8, "t": 56, "b": 8},
        legend={
            "orientation": "h",
            "y": 0.06,
            "yanchor": "top",
            "x": 0.5,
            "xanchor": "center",
            "font": {"size": 14},
        },
        # Hide percentages on slices too thin to fit them legibly; hover still shows them.
        uniformtext={"minsize": 12, "mode": "hide"},
    )
    return figure


def make_profit_loss_line_chart(cumulative_data: List[Dict[str, Any]]):
    """Render cumulative net flow with emerald gains and red losses around zero."""
    if not cumulative_data:
        return _style_figure(go.Figure().update_layout(title="No gain/loss data available"))

    frame = pd.DataFrame(cumulative_data).sort_values("date")
    expanded = [frame.iloc[0].to_dict()]
    for index in range(1, len(frame)):
        previous = frame.iloc[index - 1].to_dict()
        current = frame.iloc[index].to_dict()
        if previous["pnl"] * current["pnl"] < 0:
            fraction = abs(previous["pnl"]) / (abs(previous["pnl"]) + abs(current["pnl"]))
            previous_date = pd.Timestamp(previous["date"])
            current_date = pd.Timestamp(current["date"])
            expanded.append(
                {
                    "date": previous_date + (current_date - previous_date) * fraction,
                    "pnl": 0.0,
                    "daily_change": current["daily_change"],
                }
            )
        expanded.append(current)
    frame = pd.DataFrame(expanded)
    positive = frame["pnl"].where(frame["pnl"] >= 0)
    negative = frame["pnl"].where(frame["pnl"] <= 0)
    custom_data = frame[["daily_change"]].to_numpy()
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=positive,
            customdata=custom_data,
            mode="lines+markers",
            name="Gain",
            connectgaps=False,
            line={"color": "#34D399", "width": 3, "shape": "linear"},
            marker={"size": 7, "color": "#34D399"},
            hovertemplate=(
                "<b>%{x|%b %-d, %Y}</b><br>Cumulative gain: $%{y:,.2f}"
                "<br>Daily change: $%{customdata[0]:,.2f}<extra></extra>"
            ),
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=negative,
            customdata=custom_data,
            mode="lines+markers",
            name="Loss",
            connectgaps=False,
            line={"color": "#FB7185", "width": 3, "shape": "linear"},
            marker={"size": 7, "color": "#FB7185"},
            hovertemplate=(
                "<b>%{x|%b %-d, %Y}</b><br>Cumulative loss: $%{y:,.2f}"
                "<br>Daily change: $%{customdata[0]:,.2f}<extra></extra>"
            ),
        )
    )
    figure.add_hline(y=0, line_width=2, line_dash="dot", line_color="#94A3B8")
    figure.update_layout(
        title="Gain and loss across the selected period",
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
    )
    figure.update_xaxes(title_text="Date")
    figure.update_yaxes(title_text="Cumulative net flow ($)", tickprefix="$", separatethousands=True)
    return _style_figure(figure)


def make_category_bar_chart(category_summary: List[Dict[str, Any]]):
    if not category_summary:
        return _style_figure(px.bar(title="No Category Spending Available"))
    frame = pd.DataFrame(category_summary).sort_values("amount", ascending=True)
    figure = px.bar(
        frame,
        x="amount",
        y="category",
        orientation="h",
        text="amount",
        title="Category spending detail",
        labels={"amount": "Spend ($)", "category": "Category"},
        hover_data={"transactions": True, "share": ":.1f", "average": ":.2f"},
    ).update_traces(
        marker_color=FINANCIAL_COLORS["category"],
        texttemplate="$%{text:,.0f}",
        textposition="outside",
        cliponaxis=False,
    )
    return _style_figure(figure)


def make_category_heatmap(monthly_category_data: List[Dict[str, Any]]):
    if not monthly_category_data:
        return _style_figure(px.imshow(
            [[0]], x=["No data"], y=["No categories"], title="Monthly category spending"
        ))
    frame = pd.DataFrame(monthly_category_data)
    pivot = frame.pivot_table(
        index="category", columns="month", values="amount", aggfunc="sum", fill_value=0
    )
    figure = px.imshow(
        pivot,
        aspect="auto",
        text_auto=".0f",
        color_continuous_scale="Blues",
        labels={"x": "Month", "y": "Category", "color": "Spend ($)"},
        title="Monthly spending by category",
    )
    figure.update_layout(coloraxis_colorbar={"title": "Spend ($)"})
    return _style_figure(figure)


def make_comparison_summary_chart(comparisons: List[Dict[str, Any]]):
    frame = pd.DataFrame(
        [
            {
                "statement": item["label"],
                "metric": "Money out",
                "value": item["summary"]["outflow"],
            }
            for item in comparisons
        ]
        + [
            {
                "statement": item["label"],
                "metric": "Net cash flow",
                "value": item["summary"]["net_savings"],
            }
            for item in comparisons
        ]
    )
    figure = px.bar(
        frame,
        x="statement",
        y="value",
        color="metric",
        barmode="group",
        text="value",
        title="Money out and net cash flow by period",
        labels={"statement": "Statement", "value": "Amount ($)"},
        color_discrete_map={
            "Money out": FINANCIAL_COLORS["expense"],
            "Net cash flow": FINANCIAL_COLORS["income"],
        },
    ).update_traces(texttemplate="$%{text:,.0f}", textposition="outside", cliponaxis=False)
    return _style_figure(figure)


def make_comparison_category_chart(comparisons: List[Dict[str, Any]]):
    category_totals: Dict[str, float] = defaultdict(float)
    for comparison in comparisons:
        for category, amount in comparison["categories"].items():
            category_totals[category] = max(category_totals[category], amount)
    categories = [
        category
        for category, _ in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    ]
    rows = []
    for comparison in comparisons:
        for category in categories:
            rows.append(
                {
                    "category": category,
                    "statement": comparison["label"],
                    "amount": comparison["categories"].get(category, 0.0),
                }
            )
    if not rows:
        return _style_figure(px.bar(title="No category spending to compare"))
    statement_color_map = {
        comparison["label"]: STATEMENT_COLORS[index % len(STATEMENT_COLORS)]
        for index, comparison in enumerate(comparisons)
    }
    figure = px.bar(
        pd.DataFrame(rows),
        x="amount",
        y="category",
        color="statement",
        barmode="group",
        orientation="h",
        title="Category spending across statements",
        labels={"amount": "Spend ($)", "category": "Category"},
        color_discrete_map=statement_color_map,
    )
    return _style_figure(figure)


def make_comparison_delta_chart(comparisons: List[Dict[str, Any]]):
    if len(comparisons) < 2:
        return _style_figure(px.bar(title="Upload at least two statements for category changes"))
    previous = comparisons[-2]["categories"]
    current = comparisons[-1]["categories"]
    categories = sorted(set(previous) | set(current))
    rows = [
        {
            "category": category,
            "change": current.get(category, 0.0) - previous.get(category, 0.0),
            "direction": (
                "Higher spending"
                if current.get(category, 0.0) > previous.get(category, 0.0)
                else "Lower spending"
            ),
        }
        for category in categories
    ]
    if not rows:
        return _style_figure(px.bar(title="No category changes to compare"))
    figure = px.bar(
        pd.DataFrame(rows).sort_values("change"),
        x="change",
        y="category",
        color="direction",
        orientation="h",
        title=f"Latest category change vs {comparisons[-2]['label']}",
        labels={"change": "Change in spend ($)", "category": "Category"},
        color_discrete_map={
            "Higher spending": FINANCIAL_COLORS["higher_spending"],
            "Lower spending": FINANCIAL_COLORS["lower_spending"],
        },
    )
    return _style_figure(figure)


def make_savings_rate_chart(monthly: List[Dict[str, Any]]):
    """Monthly net cash flow bars with the savings rate on a second axis."""
    if not monthly:
        return _style_figure(go.Figure().update_layout(title="No savings history available"))
    frame = pd.DataFrame(monthly)
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=frame["month"],
            y=frame["net"],
            name="Net cash flow",
            marker_color=[
                FINANCIAL_COLORS["lower_spending"] if value >= 0 else FINANCIAL_COLORS["higher_spending"]
                for value in frame["net"]
            ],
            hovertemplate="<b>%{x}</b><br>Net: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["month"],
            y=frame["savings_rate"],
            name="Savings rate",
            yaxis="y2",
            mode="lines+markers",
            connectgaps=False,
            line={"color": FINANCIAL_COLORS["savings"], "width": 3},
            hovertemplate="<b>%{x}</b><br>Savings rate: %{y:.1f}%<extra></extra>",
        )
    )
    figure.update_layout(
        title="Savings rate by month",
        yaxis={"title": "Net cash flow ($)", "tickprefix": "$", "separatethousands": True},
        yaxis2={"title": "Savings rate (%)", "overlaying": "y", "side": "right", "ticksuffix": "%", "showgrid": False},
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
        hovermode="x unified",
    )
    return _style_figure(figure)


def make_merchant_bar_chart(merchants: List[Dict[str, Any]]):
    if not merchants:
        return _style_figure(px.bar(title="No merchant spending available"))
    frame = pd.DataFrame(merchants).sort_values("amount", ascending=True)
    figure = px.bar(
        frame,
        x="amount",
        y="merchant",
        orientation="h",
        text="amount",
        title="Top merchants",
        labels={"amount": "Spend ($)", "merchant": "Merchant"},
        hover_data={"visits": True, "average": ":$,.2f", "share": ":.1f", "category": True},
    ).update_traces(
        marker_color=FINANCIAL_COLORS["expense"],
        texttemplate="$%{text:,.0f}",
        textposition="outside",
        cliponaxis=False,
    )
    return _style_figure(figure)


def make_weekday_chart(weekday: List[Dict[str, Any]]):
    if not weekday:
        return _style_figure(px.bar(title="No weekday spending available"))
    frame = pd.DataFrame(weekday)
    frame["kind"] = frame["day"].map(
        lambda day: "Weekend" if day in ("Saturday", "Sunday") else "Weekday"
    )
    figure = px.bar(
        frame,
        x="day",
        y="average_per_day",
        color="kind",
        text="average_per_day",
        title="Average spending by day of week",
        labels={"average_per_day": "Average spend per day ($)", "day": ""},
        hover_data={"total": ":$,.2f", "transactions": True, "kind": False},
        color_discrete_map={"Weekday": FINANCIAL_COLORS["category"], "Weekend": FINANCIAL_COLORS["savings"]},
        category_orders={"day": list(frame["day"])},
    ).update_traces(texttemplate="$%{text:,.0f}", textposition="outside", cliponaxis=False)
    return _style_figure(figure)


def make_week_of_month_chart(buckets: List[Dict[str, Any]]):
    if not buckets:
        return _style_figure(px.bar(title="No spending timing available"))
    figure = px.bar(
        pd.DataFrame(buckets),
        x="bucket",
        y="average_per_day",
        text="average_per_day",
        title="Average spending by time of month",
        labels={"average_per_day": "Average spend per day ($)", "bucket": ""},
        hover_data={"total": ":$,.2f", "share": ":.1f"},
    ).update_traces(
        marker_color=FINANCIAL_COLORS["category"],
        texttemplate="$%{text:,.0f}",
        textposition="outside",
        cliponaxis=False,
    )
    return _style_figure(figure)


def make_daily_spending_chart(daily: List[Dict[str, Any]]):
    if not daily:
        return _style_figure(go.Figure().update_layout(title="No daily spending available"))
    frame = pd.DataFrame(daily)
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=frame["date"],
            y=frame["spend"],
            name="Daily spending",
            marker_color=FINANCIAL_COLORS["expense"],
            opacity=0.55,
            hovertemplate="<b>%{x|%b %-d, %Y}</b><br>Spent: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["rolling_7"],
            name="7-day average",
            mode="lines",
            line={"color": FINANCIAL_COLORS["income"], "width": 3},
            hovertemplate="7-day average: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        title="Daily spending and 7-day average",
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
    )
    figure.update_yaxes(title_text="Spend ($)", tickprefix="$", separatethousands=True)
    return _style_figure(figure)


def make_fixed_flexible_chart(months: List[Dict[str, Any]]):
    if not months:
        return _style_figure(px.bar(title="No spending available"))
    rows = []
    for item in months:
        rows.append({"month": item["month"], "type": "Fixed costs", "amount": item["fixed"]})
        rows.append({"month": item["month"], "type": "Flexible spending", "amount": item["flexible"]})
    figure = px.bar(
        pd.DataFrame(rows),
        x="month",
        y="amount",
        color="type",
        title="Fixed costs vs flexible spending",
        labels={"amount": "Spend ($)", "month": "Month"},
        category_orders={"type": ["Fixed costs", "Flexible spending"]},
        color_discrete_map={
            "Fixed costs": FINANCIAL_COLORS["savings"],
            "Flexible spending": FINANCIAL_COLORS["expense"],
        },
    )
    figure.update_layout(barmode="stack")
    return _style_figure(figure)


def make_cash_flow_sankey(income_sources: Dict[str, float], expenses: Dict[str, float], max_categories: int = 8):
    """Show income flowing into spending categories and savings."""
    income_total = sum(value for value in income_sources.values() if value > 0)
    expense_total = sum(value for value in expenses.values() if value > 0)
    if not income_total and not expense_total:
        return _style_figure(go.Figure().update_layout(title="No cash flow to show"))
    ranked = sorted(((name, value) for name, value in expenses.items() if value > 0), key=lambda item: item[1], reverse=True)
    shown = ranked[:max_categories]
    other = sum(value for _, value in ranked[max_categories:])
    if other:
        shown.append(("Other spending", other))

    labels = ["Money in"]
    colors = [FINANCIAL_COLORS["income"]]
    sources, targets, values = [], [], []
    for name, value in sorted(income_sources.items(), key=lambda item: item[1], reverse=True):
        if value <= 0:
            continue
        labels.append(name)
        colors.append(FINANCIAL_COLORS["income"])
        sources.append(len(labels) - 1)
        targets.append(0)
        values.append(value)
    if expense_total > income_total:
        labels.append("Drawn from savings")
        colors.append(FINANCIAL_COLORS["higher_spending"])
        sources.append(len(labels) - 1)
        targets.append(0)
        values.append(expense_total - income_total)
    for index, (name, value) in enumerate(shown):
        labels.append(name)
        colors.append(CATEGORY_COLORS[index % len(CATEGORY_COLORS)])
        sources.append(0)
        targets.append(len(labels) - 1)
        values.append(value)
    if income_total > expense_total:
        labels.append("Saved")
        colors.append(FINANCIAL_COLORS["lower_spending"])
        sources.append(0)
        targets.append(len(labels) - 1)
        values.append(income_total - expense_total)

    figure = go.Figure(
        go.Sankey(
            arrangement="snap",
            node={
                "label": labels,
                "color": colors,
                "pad": 18,
                "thickness": 16,
                "line": {"color": "#0B0F14", "width": 1},
                "hovertemplate": "<b>%{label}</b><br>$%{value:,.2f}<extra></extra>",
            },
            link={
                "source": sources,
                "target": targets,
                "value": values,
                "color": "rgba(148,163,184,0.25)",
                "hovertemplate": "%{source.label} → %{target.label}<br>$%{value:,.2f}<extra></extra>",
            },
        )
    )
    figure.update_layout(title="Where the money went", height=460)
    return _style_figure(figure)


def make_budget_history_chart(cells: List[Dict[str, Any]]):
    rows = [cell for cell in cells if cell["ratio"] is not None]
    if not rows:
        return _style_figure(px.imshow([[0]], x=["No data"], y=["No budgets"], title="Budget history"))
    pivot = pd.DataFrame(rows).pivot_table(index="category", columns="month", values="ratio", aggfunc="sum")
    figure = px.imshow(
        pivot,
        aspect="auto",
        text_auto=".0f",
        zmin=0,
        zmax=200,
        color_continuous_scale=[
            [0.0, "#134E4A"],
            [0.45, "#2DD4BF"],
            [0.5, "#FACC15"],
            [0.6, "#FB923C"],
            [1.0, "#BE123C"],
        ],
        labels={"x": "Month", "y": "Budget", "color": "% of limit"},
        title="Share of each monthly budget used (%)",
    )
    figure.update_layout(coloraxis_colorbar={"title": "% of limit", "ticksuffix": "%"})
    return _style_figure(figure)


def make_net_worth_chart(history: List[Dict[str, Any]]):
    if not history:
        return _style_figure(go.Figure().update_layout(title="No balance history yet"))
    frame = pd.DataFrame(history)
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["net_worth"],
            name="Net worth",
            mode="lines+markers",
            line={"color": FINANCIAL_COLORS["savings"], "width": 3},
            hovertemplate="<b>%{x|%b %-d, %Y}</b><br>Net worth: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["assets"],
            name="Assets",
            mode="lines",
            line={"color": FINANCIAL_COLORS["income"], "width": 2, "dash": "dot"},
            hovertemplate="Assets: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["liabilities"],
            name="Debts",
            mode="lines",
            line={"color": FINANCIAL_COLORS["higher_spending"], "width": 2, "dash": "dot"},
            hovertemplate="Debts: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(
        title="Net worth over time",
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 1, "xanchor": "right"},
    )
    figure.update_yaxes(tickprefix="$", separatethousands=True)
    return _style_figure(figure)


def make_income_chart(months: List[Dict[str, Any]]):
    if not months:
        return _style_figure(px.bar(title="No income history available"))
    figure = px.bar(
        pd.DataFrame(months),
        x="month",
        y="income",
        text="income",
        title="Income by month",
        labels={"income": "Income ($)", "month": "Month"},
    ).update_traces(
        marker_color=FINANCIAL_COLORS["income"],
        texttemplate="$%{text:,.0f}",
        textposition="outside",
        cliponaxis=False,
    )
    return _style_figure(figure)
