from collections import defaultdict
from typing import Any, Dict, List

import pandas as pd
import plotly.express as px


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
    figure = px.pie(
        pd.DataFrame(rows),
        names="category",
        values="amount",
        hole=0.45,
        title="Spending by category",
        color_discrete_sequence=CATEGORY_COLORS,
    )
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
