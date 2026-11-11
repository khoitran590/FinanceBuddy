"""Every chart in the app, plus one-sentence takeaways that say what each shows.

Conventions shared by all of them:
- Blue marks money in and good news; orange marks money out and things that need
  attention. A chart that only breaks spending down uses blue for every bar,
  because there is nothing to tell apart.
- One y-axis per chart, months as plain labels ("Sep 2026"), dollar amounts on
  ticks and in hover text, faint grid lines, and thin rounded bars.
- Text colors are left unset so Streamlit's theme fills them in for light or dark.
"""
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

import pandas as pd
import plotly.graph_objects as go


FINANCIAL_COLORS = {
    "income": "#2F6FCB",
    "expense": "#D9701E",
    "savings": "#9B7BD8",
    "category": "#2F6FCB",
    "higher_spending": "#D9701E",
    "lower_spending": "#2F6FCB",
}
STATEMENT_COLORS = ["#2F6FCB", "#2E9C95", "#9B7BD8", "#E08A3C", "#C9A227", "#8A8F98"]
CATEGORY_COLORS = [
    "#2F6FCB",
    "#E08A3C",
    "#2E9C95",
    "#9B7BD8",
    "#C9A227",
    "#6B9BE0",
    "#B7652B",
    "#8A8F98",
]
NEUTRAL_LINE = "rgba(128,128,128,0.55)"
GRID_LINE = "rgba(128,128,128,0.18)"
# Heat maps run from the card surface to a solid blue, so "nothing" looks empty in both themes.
HEAT_SCALES = {
    False: [[0.0, "#EEF3FB"], [1.0, "#1F4E8C"]],
    True: [[0.0, "#22262E"], [1.0, "#6FA3EC"]],
}
BUDGET_SCALES = {
    # 0–100% of the limit is blue, anything over is orange; the break sits at the limit.
    False: [[0.0, "#EEF3FB"], [0.4999, "#2F6FCB"], [0.5, "#F0B27A"], [1.0, "#A8440A"]],
    True: [[0.0, "#22262E"], [0.4999, "#6FA3EC"], [0.5, "#B7652B"], [1.0, "#F2A765"]],
}
DONUT_SLICES = 6
LEGEND_TOP = {"orientation": "h", "y": 1.02, "yanchor": "bottom", "x": 0, "xanchor": "left", "traceorder": "normal"}


def _labels_for(colors: List[str]) -> List[str]:
    """Choose the higher-contrast label against each actual chart fill."""
    labels = []
    for color in colors:
        channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
                  for value in channels]
        luminance = sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))
        white_contrast = 1.05 / (luminance + 0.05)
        black_contrast = (luminance + 0.05) / 0.05
        labels.append("#FFFFFF" if white_contrast >= black_contrast else "#000000")
    return labels


def _money(value, signed: bool = False) -> str:
    value = float(value)
    sign = "-" if value < 0 else ("+" if signed and value > 0 else "")
    return f"{sign}${abs(value):,.0f}"


def _month_label(month: Any) -> str:
    """'2026-09' -> 'Sep 2026'; anything else is shown as it came."""
    try:
        return datetime.strptime(str(month)[:7], "%Y-%m").strftime("%b %Y")
    except ValueError:
        return str(month)


def _short(text: Any, limit: int = 26) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _style_figure(figure):
    """Apply the shared look: transparent surface, faint grid, thin rounded bars."""
    figure.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"size": 13},
        title={"font": {"size": 17}},
        legend={"font": {"size": 12}, "title_text": ""},
        margin={"l": 8, "r": 16, "t": 64, "b": 8},
        barcornerradius=4,
        hoverlabel={"font": {"size": 13}},
    )
    figure.update_xaxes(showgrid=False, zeroline=False, automargin=True, title_standoff=8)
    figure.update_yaxes(gridcolor=GRID_LINE, zeroline=False, automargin=True, title_standoff=8)
    return figure


def _scale_color(scale: List[list], position: float) -> str:
    """The hex color a colorscale gives at a 0–1 position."""
    position = min(1.0, max(0.0, position))
    for (low_at, low), (high_at, high) in zip(scale, scale[1:]):
        if position <= high_at:
            span = (position - low_at) / (high_at - low_at) if high_at > low_at else 0
            mixed = [
                round(int(low[index:index + 2], 16) + (int(high[index:index + 2], 16) - int(low[index:index + 2], 16)) * span)
                for index in (1, 3, 5)
            ]
            return "#{:02X}{:02X}{:02X}".format(*mixed)
    return scale[-1][1]


def _label_cells(figure, values, texts, columns, rows, scale, top: float):
    """Write each heat-map cell's value in whichever of black or white reads best on its fill."""
    for row_index, row in enumerate(rows):
        for column_index, column in enumerate(columns):
            text = texts[row_index][column_index]
            if not text:
                continue
            value = values[row_index][column_index]
            fill = _scale_color(scale, (0 if pd.isna(value) else float(value)) / top if top else 0)
            figure.add_annotation(
                x=column, y=row, text=text, showarrow=False,
                font={"size": 12, "color": _labels_for([fill])[0]},
            )
    return figure


def _empty(message: str):
    figure = go.Figure()
    figure.update_layout(
        title=message,
        height=180,
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    return _style_figure(figure)


def _month_axis(figure, months: List[Any]):
    """Show months as labelled categories, keeping the raw key as the clicked value."""
    keys = [str(month) for month in months]
    figure.update_xaxes(
        type="category",
        categoryorder="array",
        categoryarray=keys,
        tickmode="array",
        tickvals=keys,
        ticktext=[_month_label(key) for key in keys],
        tickangle=0,
        title_text=None,
    )
    return figure


def _dollar_axis(figure, axis: str = "y"):
    update = figure.update_yaxes if axis == "y" else figure.update_xaxes
    update(tickprefix="$", separatethousands=True, tickformat=",.0f", title_text=None)
    return figure


def _bar_gap(count: int) -> float:
    """Keep bars slim when there are only a few of them."""
    return 0.7 if count <= 2 else 0.55 if count <= 4 else 0.35


# --- Money in, money out -----------------------------------------------------


def make_monthly_bar_chart(monthly_data: List[Dict[str, Any]]):
    if not monthly_data:
        return _empty("No Monthly Data Available")
    months = [item["month"] for item in monthly_data]
    labels = [_month_label(month) for month in months]
    figure = go.Figure()
    for column, name, color in (
        ("inflow", "Money in", FINANCIAL_COLORS["income"]),
        ("outflow", "Money out", FINANCIAL_COLORS["expense"]),
    ):
        values = [float(item[column]) for item in monthly_data]
        figure.add_bar(
            x=months,
            y=values,
            name=name,
            marker_color=color,
            customdata=labels,
            text=[_money(value) for value in values] if len(months) <= 6 else None,
            textposition="outside",
            cliponaxis=False,
            hovertemplate=f"<b>%{{customdata}}</b><br>{name}: $%{{y:,.2f}}<extra></extra>",
        )
    figure.update_layout(
        title="Monthly cash flow",
        barmode="group",
        bargap=_bar_gap(len(months) * 2) if len(months) <= 2 else 0.3,
        bargroupgap=0.08,
        legend=LEGEND_TOP,
    )
    _style_figure(figure)
    _dollar_axis(figure).update_yaxes(rangemode="tozero")
    return _month_axis(figure, months)


def make_monthly_line_chart(monthly_data: List[Dict[str, Any]]):
    """Money in and money out as two lines, the "Lines" style of the cash flow card."""
    if not monthly_data:
        return _empty("No Monthly Data Available")
    months = [item["month"] for item in monthly_data]
    labels = [_month_label(month) for month in months]
    figure = go.Figure()
    for column, name in (("inflow", "Money in"), ("outflow", "Money out")):
        color = FINANCIAL_COLORS["income" if column == "inflow" else "expense"]
        figure.add_scatter(
            x=months,
            y=[float(item[column]) for item in monthly_data],
            name=name,
            mode="lines+markers",
            customdata=labels,
            line={"color": color, "width": 2.5},
            marker={"size": 8, "color": color},
            hovertemplate=f"<b>%{{customdata}}</b><br>{name}: $%{{y:,.2f}}<extra></extra>",
        )
    figure.update_layout(title="Monthly cash flow", legend=LEGEND_TOP)
    _style_figure(figure)
    _dollar_axis(figure).update_yaxes(rangemode="tozero")
    return _month_axis(figure, months)


def make_monthly_net_chart(monthly_data: List[Dict[str, Any]]):
    """Money kept each month; bars below zero are months with more out than in."""
    if not monthly_data:
        return _empty("No Monthly Data Available")
    months = [item["month"] for item in monthly_data]
    net = [float(item["inflow"] - item["outflow"]) for item in monthly_data]
    figure = go.Figure(
        go.Bar(
            x=months,
            y=net,
            name="Net",
            marker_color=[
                FINANCIAL_COLORS["income"] if value >= 0 else FINANCIAL_COLORS["expense"] for value in net
            ],
            customdata=[
                [_month_label(month), "Kept" if value >= 0 else "Overspent", abs(value)]
                for month, value in zip(months, net)
            ],
            text=[_money(value, signed=True) for value in net],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}: $%{customdata[2]:,.2f}<extra></extra>",
        )
    )
    figure.add_hline(y=0, line_width=1, line_color=NEUTRAL_LINE)
    figure.update_layout(title="Saved each month", showlegend=False, bargap=_bar_gap(len(months)))
    _style_figure(figure)
    _dollar_axis(figure)
    return _month_axis(figure, months)


def make_profit_loss_line_chart(cumulative_data: List[Dict[str, Any]]):
    """Running total of money in minus money out, blue above zero and orange below."""
    if not cumulative_data:
        return _empty("No gain/loss data available")

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
                    # Timedelta accepts a float scale; only the chart's date
                    # interpolation is approximate, while money stays Decimal.
                    "date": previous_date + (current_date - previous_date) * float(fraction),
                    "pnl": 0.0,
                    "daily_change": current["daily_change"],
                }
            )
        expanded.append(current)
    frame = pd.DataFrame(expanded)
    custom_data = frame[["daily_change"]].to_numpy()
    figure = go.Figure()
    for name, values, color, fill in (
        ("Ahead", frame["pnl"].where(frame["pnl"] >= 0), FINANCIAL_COLORS["income"], "rgba(47,111,203,0.14)"),
        ("Behind", frame["pnl"].where(frame["pnl"] <= 0), FINANCIAL_COLORS["expense"], "rgba(217,112,30,0.14)"),
    ):
        figure.add_trace(
            go.Scatter(
                x=frame["date"],
                y=values,
                customdata=custom_data,
                mode="lines",
                name=name,
                # A side the total never reaches has nothing to show in the legend.
                showlegend=bool((values.fillna(0) != 0).any()),
                connectgaps=False,
                fill="tozeroy",
                fillcolor=fill,
                line={"color": color, "width": 2.5, "shape": "linear"},
                hovertemplate=(
                    f"<b>%{{x|%b %-d, %Y}}</b><br>{name} by $%{{y:,.2f}} so far"
                    "<br>That day: $%{customdata[0]:,.2f}<extra></extra>"
                ),
            )
        )
    figure.add_hline(y=0, line_width=1, line_color=NEUTRAL_LINE)
    figure.update_layout(title="Running total across the selected period", legend=LEGEND_TOP)
    _style_figure(figure)
    _dollar_axis(figure)
    figure.update_xaxes(title_text=None)
    return figure


# --- Spending by category ----------------------------------------------------


def make_category_bar_chart(category_summary: List[Dict[str, Any]]):
    if not category_summary:
        return _empty("No Category Spending Available")
    rows = sorted(category_summary, key=lambda item: item["amount"])
    figure = go.Figure(
        go.Bar(
            x=[float(item["amount"]) for item in rows],
            y=[item["category"] for item in rows],
            orientation="h",
            marker_color=FINANCIAL_COLORS["category"],
            customdata=[
                [float(item.get("share", 0)), item.get("transactions", 0), float(item.get("average", 0))]
                for item in rows
            ],
            text=[
                f"{_money(item['amount'])} · {float(item['share']):.0f}%" if "share" in item else _money(item["amount"])
                for item in rows
            ],
            textposition="outside",
            cliponaxis=False,
            hovertemplate=(
                "<b>%{y}</b><br>$%{x:,.2f} · %{customdata[0]:.1f}% of spending"
                "<br>%{customdata[1]} transactions, $%{customdata[2]:,.2f} on average<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        title="Category spending detail",
        showlegend=False,
        bargap=0.4,
        height=max(220, 60 + 38 * len(rows)),
    )
    _style_figure(figure)
    figure.update_layout(margin={"r": 96})
    figure.update_xaxes(visible=False)
    figure.update_yaxes(showgrid=False, title_text=None)
    return figure


def _donut_rows(category_data: Dict[str, float]) -> List[tuple]:
    """Largest categories first, with the long tail folded into one "Other" slice."""
    ranked = sorted(category_data.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) <= DONUT_SLICES:
        return ranked
    kept = ranked[: DONUT_SLICES - 1]
    return kept + [("Other", sum(amount for _, amount in ranked[DONUT_SLICES - 1:]))]


def make_category_donut_chart(category_data: Dict[str, float]):
    if not category_data:
        return _empty("No Expense Data Available")

    rows = _donut_rows(category_data)
    total = sum(amount for _, amount in rows)
    colors = [CATEGORY_COLORS[index % len(CATEGORY_COLORS)] for index in range(len(rows))]
    if rows[-1][0] == "Other" and len(category_data) > DONUT_SLICES:
        colors[-1] = CATEGORY_COLORS[-1]
    figure = go.Figure(
        go.Pie(
            labels=[category for category, _ in rows],
            values=[float(amount) for _, amount in rows],
            hole=0.5,
            sort=False,
            direction="clockwise",
            marker={"colors": colors, "line": {"color": "rgba(0,0,0,0)", "width": 0}},
            pull=0.012,
            textinfo="percent",
            textposition="inside",
            insidetextorientation="horizontal",
            textfont={"size": 15, "color": _labels_for(colors)},
            hovertemplate="<b>%{label}</b><br>$%{value:,.2f}<br>%{percent} of spending<extra></extra>",
            domain={"x": [0, 1], "y": [0.14, 1]},
        )
    )
    figure.update_layout(
        title="Spending by category",
        annotations=[
            {
                "text": f"<span style='font-size:14px'>Total spent</span><br><b>${total:,.0f}</b>",
                "x": 0.5,
                "y": 0.57,
                "showarrow": False,
                "font": {"size": 24},
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
            "y": 0.08,
            "yanchor": "top",
            "x": 0.5,
            "xanchor": "center",
            "font": {"size": 14},
        },
        # Hide percentages on slices too thin to fit them legibly; hover still shows them.
        uniformtext={"minsize": 12, "mode": "hide"},
    )
    return figure


def make_category_treemap(category_summary: List[Dict[str, Any]]):
    """Spending by category as proportional tiles, the "Map" style of the category card."""
    rows = [item for item in category_summary if item["amount"] > 0]
    if not rows:
        return _empty("No Expense Data Available")
    rows = sorted(rows, key=lambda item: item["amount"], reverse=True)
    colors = [CATEGORY_COLORS[index % len(CATEGORY_COLORS)] for index in range(len(rows))]
    figure = go.Figure(
        go.Treemap(
            labels=[item["category"] for item in rows],
            parents=["" for _ in rows],
            values=[float(item["amount"]) for item in rows],
            marker={"colors": colors},
            texttemplate="<b>%{label}</b><br>$%{value:,.0f} · %{percentRoot:.0%}",
            textfont={"size": 14, "color": _labels_for(colors)},
            hovertemplate="<b>%{label}</b><br>$%{value:,.2f}<br>%{percentRoot:.1%} of spending<extra></extra>",
            tiling={"pad": 3},
            sort=True,
        )
    )
    figure.update_layout(title="Spending by category")
    figure = _style_figure(figure)
    figure.update_layout(margin={"l": 8, "r": 8, "t": 56, "b": 8})
    return figure


def make_category_heatmap(monthly_category_data: List[Dict[str, Any]], dark: bool = False):
    """A month-by-category grid; the darker (light theme) or brighter (dark theme) the cell, the more spent."""
    if not monthly_category_data:
        return _empty("Monthly category spending")
    frame = pd.DataFrame(monthly_category_data).assign(amount=lambda data: data["amount"].astype(float))
    pivot = frame.pivot_table(index="category", columns="month", values="amount", aggfunc="sum", fill_value=0)
    pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).index]
    months = [_month_label(month) for month in pivot.columns]
    cells = pivot.to_numpy()
    top = float(cells.max()) or 1.0
    texts = [[_money(value) if value else "–" for value in row] for row in cells]
    figure = go.Figure(
        go.Heatmap(
            z=cells,
            x=months,
            y=list(pivot.index),
            colorscale=HEAT_SCALES[bool(dark)],
            zmin=0,
            zmax=top,
            showscale=False,
            xgap=3,
            ygap=3,
            hovertemplate="<b>%{y}</b><br>%{x}: $%{z:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(title="Monthly spending by category", height=max(220, 70 + 36 * len(pivot.index)))
    _label_cells(figure, cells, texts, months, list(pivot.index), HEAT_SCALES[bool(dark)], top)
    _style_figure(figure)
    figure.update_xaxes(type="category", side="top", title_text=None)
    figure.update_yaxes(autorange="reversed", showgrid=False, title_text=None)
    return figure


def make_category_trend_lines(monthly_category_data: List[Dict[str, Any]], top_n: int = 5):
    """The largest categories month by month, as lines instead of a heat map."""
    if not monthly_category_data:
        return _empty("Monthly spending by category")
    frame = pd.DataFrame(monthly_category_data).assign(amount=lambda data: data["amount"].astype(float))
    leaders = frame.groupby("category")["amount"].sum().sort_values(ascending=False).head(top_n).index
    pivot = frame[frame["category"].isin(leaders)].pivot_table(
        index="month", columns="category", values="amount", aggfunc="sum", fill_value=0
    )
    months = list(pivot.index)
    labels = [_month_label(month) for month in months]
    figure = go.Figure()
    for index, category in enumerate(leaders):
        color = CATEGORY_COLORS[index % len(CATEGORY_COLORS)]
        figure.add_trace(
            go.Scatter(
                x=months,
                y=pivot[category],
                name=category,
                mode="lines+markers",
                customdata=labels,
                line={"color": color, "width": 2.5},
                marker={"size": 8, "color": color},
                hovertemplate=f"<b>{category}</b><br>%{{customdata}}: $%{{y:,.2f}}<extra></extra>",
            )
        )
    figure.update_layout(title=f"Top {len(leaders)} categories by month", legend=LEGEND_TOP)
    _style_figure(figure)
    _dollar_axis(figure).update_yaxes(rangemode="tozero")
    return _month_axis(figure, months)


# --- Comparing periods -------------------------------------------------------


def make_comparison_summary_chart(comparisons: List[Dict[str, Any]]):
    if not comparisons:
        return _empty("No periods to compare")
    periods = [item["label"] for item in comparisons]
    figure = go.Figure()
    for name, key, color in (
        ("Money out", "outflow", FINANCIAL_COLORS["expense"]),
        ("Kept (money in minus money out)", "net_savings", FINANCIAL_COLORS["income"]),
    ):
        values = [float(item["summary"][key]) for item in comparisons]
        figure.add_bar(
            x=periods,
            y=values,
            name=name,
            marker_color=color,
            text=[_money(value) for value in values],
            textposition="outside",
            cliponaxis=False,
            hovertemplate=f"<b>%{{x}}</b><br>{name}: $%{{y:,.2f}}<extra></extra>",
        )
    figure.add_hline(y=0, line_width=1, line_color=NEUTRAL_LINE)
    figure.update_layout(
        title="Money out and money kept by period", barmode="group", bargap=0.4, bargroupgap=0.08, legend=LEGEND_TOP
    )
    _style_figure(figure)
    _dollar_axis(figure)
    figure.update_xaxes(type="category", title_text=None)
    return figure


def make_comparison_category_chart(comparisons: List[Dict[str, Any]], max_categories: int = 8):
    category_totals: Dict[str, float] = defaultdict(float)
    for comparison in comparisons:
        for category, amount in comparison["categories"].items():
            category_totals[category] = max(category_totals[category], amount)
    categories = [
        category
        for category, _ in sorted(category_totals.items(), key=lambda item: item[1], reverse=True)
    ][:max_categories]
    if not categories:
        return _empty("No category spending to compare")
    figure = go.Figure()
    for index, comparison in enumerate(comparisons):
        figure.add_bar(
            x=[float(comparison["categories"].get(category, 0.0)) for category in categories],
            y=categories,
            name=comparison["label"],
            orientation="h",
            marker_color=STATEMENT_COLORS[index % len(STATEMENT_COLORS)],
            hovertemplate=f"<b>%{{y}}</b><br>{comparison['label']}: $%{{x:,.2f}}<extra></extra>",
        )
    figure.update_layout(
        title="Category spending across periods",
        barmode="group",
        bargap=0.3,
        legend=LEGEND_TOP,
        height=max(320, 90 + len(categories) * (14 * len(comparisons) + 18)),
    )
    _style_figure(figure)
    _dollar_axis(figure, "x")
    figure.update_xaxes(showgrid=True, gridcolor=GRID_LINE)
    figure.update_yaxes(autorange="reversed", showgrid=False, title_text=None)
    return figure


def _category_changes(comparisons: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    previous = comparisons[-2]["categories"]
    current = comparisons[-1]["categories"]
    rows = [
        {"category": category, "change": float(current.get(category, 0)) - float(previous.get(category, 0))}
        for category in set(previous) | set(current)
    ]
    return sorted((row for row in rows if round(row["change"], 2)), key=lambda row: row["change"])


def make_comparison_delta_chart(comparisons: List[Dict[str, Any]]):
    if len(comparisons) < 2:
        return _empty("Choose at least two periods for category changes")
    rows = _category_changes(comparisons)
    if not rows:
        return _empty("No category changes to compare")
    figure = go.Figure(
        go.Bar(
            x=[row["change"] for row in rows],
            y=[row["category"] for row in rows],
            orientation="h",
            marker_color=[
                FINANCIAL_COLORS["higher_spending"] if row["change"] > 0 else FINANCIAL_COLORS["lower_spending"]
                for row in rows
            ],
            customdata=[["more" if row["change"] > 0 else "less", abs(row["change"])] for row in rows],
            text=[_money(row["change"], signed=True) for row in rows],
            textposition="outside",
            cliponaxis=False,
            hovertemplate=(
                "<b>%{y}</b><br>$%{customdata[1]:,.2f} %{customdata[0]} than "
                f"{comparisons[-2]['label']}<extra></extra>"
            ),
        )
    )
    figure.add_vline(x=0, line_width=1, line_color=NEUTRAL_LINE)
    figure.update_layout(
        title=f"{comparisons[-1]['label']} vs {comparisons[-2]['label']}, by category",
        showlegend=False,
        bargap=0.4,
        height=max(240, 90 + 34 * len(rows)),
    )
    _style_figure(figure)
    figure.update_layout(margin={"l": 8, "r": 16})
    # Room on both sides so the amount written past a bar's end never runs into the category names.
    low, high = min(0.0, rows[0]["change"]), max(0.0, rows[-1]["change"])
    pad = (high - low) * 0.16
    figure.update_xaxes(visible=False, range=[low - pad, high + pad])
    figure.update_yaxes(showgrid=False, title_text=None)
    return figure


# --- Trends ------------------------------------------------------------------


def make_savings_rate_chart(monthly: List[Dict[str, Any]]):
    """The share of each month's income that was kept. One bar per month, one axis."""
    if not monthly:
        return _empty("No savings history available")
    months = [item["month"] for item in monthly]
    rates = [None if item["savings_rate"] is None else float(item["savings_rate"]) for item in monthly]
    figure = go.Figure(
        go.Bar(
            x=months,
            y=rates,
            name="Savings rate",
            marker_color=[
                FINANCIAL_COLORS["lower_spending"] if (rate or 0) >= 0 else FINANCIAL_COLORS["higher_spending"]
                for rate in rates
            ],
            customdata=[
                [_month_label(item["month"]), float(item["net"]), float(item.get("inflow", 0)),
                 "kept" if item["net"] >= 0 else "overspent"]
                for item in monthly
            ],
            text=["" if rate is None else f"{rate:.0f}%" for rate in rates],
            textposition="outside",
            cliponaxis=False,
            hovertemplate=(
                "<b>%{customdata[0]}</b><br>%{y:.1f}% of income %{customdata[3]}"
                "<br>Net $%{customdata[1]:,.2f} of $%{customdata[2]:,.2f} in<extra></extra>"
            ),
        )
    )
    figure.add_hline(y=0, line_width=1, line_color=NEUTRAL_LINE)
    figure.update_layout(title="Savings rate by month", showlegend=False, bargap=_bar_gap(len(months)))
    _style_figure(figure)
    figure.update_yaxes(ticksuffix="%", tickformat=".0f", title_text=None)
    return _month_axis(figure, months)


def make_fixed_flexible_chart(months: List[Dict[str, Any]], stacked: bool = True):
    if not months:
        return _empty("No spending available")
    keys = [item["month"] for item in months]
    labels = [_month_label(key) for key in keys]
    figure = go.Figure()
    for name, column, color in (
        ("Fixed costs", "fixed", FINANCIAL_COLORS["savings"]),
        ("Flexible spending", "flexible", FINANCIAL_COLORS["expense"]),
    ):
        figure.add_bar(
            x=keys,
            y=[float(item[column]) for item in months],
            name=name,
            marker={"color": color, "line": {"width": 0}},
            customdata=labels,
            hovertemplate=f"<b>%{{customdata}}</b><br>{name}: $%{{y:,.2f}}<extra></extra>",
        )
    figure.update_layout(
        title="Fixed costs vs flexible spending",
        barmode="stack" if stacked else "group",
        bargap=_bar_gap(len(keys)) if stacked else 0.3,
        bargroupgap=0.08,
        legend=LEGEND_TOP,
    )
    _style_figure(figure)
    _dollar_axis(figure)
    return _month_axis(figure, keys)


def make_daily_spending_chart(daily: List[Dict[str, Any]]):
    """Daily bars with a 7-day average line.

    One rent payment can be twenty times a normal day, which would flatten every
    other bar. The axis is therefore sized to ordinary days, and the few days that
    run past the top are labelled with their amount instead.
    """
    if not daily:
        return _empty("No daily spending available")
    frame = pd.DataFrame(daily).assign(
        spend=lambda data: data["spend"].astype(float), rolling_7=lambda data: data["rolling_7"].astype(float)
    )
    spent_days = frame.loc[frame["spend"] > 0, "spend"]
    ceiling = None
    if len(spent_days) >= 5:
        typical_top = max(float(spent_days.quantile(0.9)) * 2, float(frame["rolling_7"].quantile(0.95)) * 1.3)
        if float(spent_days.max()) > typical_top * 1.5:
            ceiling = typical_top
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=frame["date"],
            y=frame["spend"],
            name="Spent that day",
            marker_color=FINANCIAL_COLORS["category"],
            opacity=0.4,
            hovertemplate="<b>%{x|%a, %b %-d}</b><br>Spent: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["date"],
            y=frame["rolling_7"],
            name="7-day average",
            mode="lines",
            line={"color": FINANCIAL_COLORS["category"], "width": 2.5},
            hovertemplate="<b>%{x|%a, %b %-d}</b><br>7-day average: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(title="Daily spending and 7-day average", legend=LEGEND_TOP, bargap=0.2)
    _style_figure(figure)
    _dollar_axis(figure).update_yaxes(rangemode="tozero")
    figure.update_xaxes(title_text=None)
    if ceiling:
        figure.update_yaxes(range=[0, ceiling])
        for _, row in frame[frame["spend"] > ceiling].iterrows():
            figure.add_annotation(
                x=row["date"], y=ceiling, yanchor="top", xanchor="left", xshift=5, showarrow=False,
                text=f"▲ {_money(row['spend'])}", font={"size": 11},
            )
    return figure


def make_cash_flow_sankey(income_sources: Dict[str, float], expenses: Dict[str, float], max_categories: int = 8,
                          dark: bool = False):
    """Show income flowing into spending categories and savings."""
    income_total = sum(value for value in income_sources.values() if value > 0)
    expense_total = sum(value for value in expenses.values() if value > 0)
    if not income_total and not expense_total:
        return _empty("No cash flow to show")
    ranked = sorted(((name, value) for name, value in expenses.items() if value > 0), key=lambda item: item[1], reverse=True)
    shown = ranked[:max_categories]
    other = sum(value for _, value in ranked[max_categories:])
    if other:
        shown.append(("Other spending", other))

    names = ["Money in"]
    amounts = [max(income_total, expense_total)]
    colors = [FINANCIAL_COLORS["income"]]
    sources, targets, values, link_colors = [], [], [], []

    def add(name, value, color, inbound: bool) -> None:
        names.append(name)
        amounts.append(value)
        colors.append(color)
        sources.append(len(names) - 1 if inbound else 0)
        targets.append(0 if inbound else len(names) - 1)
        values.append(value)
        red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
        link_colors.append(f"rgba({red},{green},{blue},0.28)")

    for name, value in sorted(income_sources.items(), key=lambda item: item[1], reverse=True):
        if value > 0:
            add(name, value, FINANCIAL_COLORS["income"], inbound=True)
    if expense_total > income_total:
        add("Drawn from savings", expense_total - income_total, FINANCIAL_COLORS["higher_spending"], inbound=True)
    for index, (name, value) in enumerate(shown):
        add(name, value, CATEGORY_COLORS[index % len(CATEGORY_COLORS)], inbound=False)
    if income_total > expense_total:
        add("Saved", income_total - expense_total, FINANCIAL_COLORS["lower_spending"], inbound=False)

    figure = go.Figure(
        go.Sankey(
            arrangement="snap",
            # Sankey labels ignore the app theme, so the ink color is set for each theme here.
            textfont={"color": "#F1EFE9" if dark else "#1C1B18", "size": 12, "shadow": "none"},
            node={
                # The amount sits in the label so nothing depends on hovering.
                "label": [f"{name} · {_money(amount)}" for name, amount in zip(names, amounts)],
                "customdata": names,
                "color": colors,
                "pad": 20,
                "thickness": 14,
                "line": {"width": 0},
                "hovertemplate": "<b>%{customdata}</b><br>$%{value:,.2f}<extra></extra>",
            },
            link={
                "source": sources,
                "target": targets,
                "value": [float(value) for value in values],
                "color": link_colors,
                "hovertemplate": "%{source.customdata} → %{target.customdata}<br>$%{value:,.2f}<extra></extra>",
            },
        )
    )
    figure.update_layout(title="Where the money went", height=460)
    figure = _style_figure(figure)
    figure.update_layout(margin={"l": 8, "r": 8, "b": 16})
    return figure


# --- Habits ------------------------------------------------------------------


def make_merchant_bar_chart(merchants: List[Dict[str, Any]]):
    if not merchants:
        return _empty("No merchant spending available")
    rows = sorted(merchants, key=lambda item: item["amount"])
    figure = go.Figure(
        go.Bar(
            x=[float(item["amount"]) for item in rows],
            y=[_short(item["merchant"]) for item in rows],
            orientation="h",
            marker_color=FINANCIAL_COLORS["category"],
            customdata=[
                [item["merchant"], item.get("visits", 0), float(item.get("average", 0)),
                 float(item.get("share", 0)), item.get("category", "")]
                for item in rows
            ],
            text=[_money(item["amount"]) for item in rows],
            textposition="outside",
            cliponaxis=False,
            hovertemplate=(
                "<b>%{customdata[0]}</b> · %{customdata[4]}<br>$%{x:,.2f} · %{customdata[3]:.1f}% of spending"
                "<br>%{customdata[1]} purchases, $%{customdata[2]:,.2f} on average<extra></extra>"
            ),
        )
    )
    figure.update_layout(
        title="Top merchants", showlegend=False, bargap=0.4, height=max(220, 60 + 36 * len(rows))
    )
    _style_figure(figure)
    figure.update_layout(margin={"r": 72})
    figure.update_xaxes(visible=False)
    figure.update_yaxes(showgrid=False, title_text=None)
    return figure


def make_weekday_chart(weekday: List[Dict[str, Any]]):
    if not weekday:
        return _empty("No weekday spending available")
    days = [item["day"] for item in weekday]
    figure = go.Figure()
    for kind, color in (("Weekday", FINANCIAL_COLORS["category"]), ("Weekend", FINANCIAL_COLORS["savings"])):
        rows = [item for item in weekday if (item["day"] in ("Saturday", "Sunday")) == (kind == "Weekend")]
        if rows:
            figure.add_bar(
                x=[item["day"][:3] for item in rows],
                y=[float(item["average_per_day"]) for item in rows],
                name=kind,
                marker_color=color,
                customdata=[[item["day"], float(item["total"]), item["transactions"]] for item in rows],
                text=[_money(item["average_per_day"]) for item in rows],
                textposition="outside",
                cliponaxis=False,
                hovertemplate=(
                    "<b>A typical %{customdata[0]}</b><br>$%{y:,.2f} spent"
                    "<br>$%{customdata[1]:,.2f} in total across %{customdata[2]} transactions<extra></extra>"
                ),
            )
    figure.update_layout(title="Average spending by day of week", legend=LEGEND_TOP, bargap=0.35)
    _style_figure(figure)
    _dollar_axis(figure)
    figure.update_xaxes(
        type="category", categoryorder="array", categoryarray=[day[:3] for day in days], title_text=None
    )
    return figure


def make_week_of_month_chart(buckets: List[Dict[str, Any]]):
    if not buckets:
        return _empty("No spending timing available")
    figure = go.Figure(
        go.Bar(
            x=[item["bucket"] for item in buckets],
            y=[float(item["average_per_day"]) for item in buckets],
            marker_color=FINANCIAL_COLORS["category"],
            customdata=[[float(item["total"]), float(item["share"])] for item in buckets],
            text=[_money(item["average_per_day"]) for item in buckets],
            textposition="outside",
            cliponaxis=False,
            hovertemplate=(
                "<b>%{x}</b><br>$%{y:,.2f} per day on average"
                "<br>$%{customdata[0]:,.2f} in total · %{customdata[1]:.1f}% of spending<extra></extra>"
            ),
        )
    )
    figure.update_layout(title="Average spending by time of month", showlegend=False, bargap=_bar_gap(len(buckets)))
    _style_figure(figure)
    _dollar_axis(figure)
    figure.update_xaxes(type="category", tickangle=0, title_text=None)
    return figure


# --- Plan, recurring, and balances -------------------------------------------


def make_budget_history_chart(cells: List[Dict[str, Any]], dark: bool = False):
    rows = [cell for cell in cells if cell["ratio"] is not None]
    if not rows:
        return _empty("Budget history")
    frame = pd.DataFrame(rows).assign(ratio=lambda data: data["ratio"].astype(float))
    pivot = frame.pivot_table(index="category", columns="month", values="ratio", aggfunc="sum")
    values = pivot.to_numpy()
    months = [_month_label(month) for month in pivot.columns]
    texts = [["" if pd.isna(value) else f"{value:.0f}%" for value in row] for row in values]
    figure = go.Figure(
        go.Heatmap(
            z=values,
            x=months,
            y=list(pivot.index),
            colorscale=BUDGET_SCALES[bool(dark)],
            zmin=0,
            zmax=200,
            showscale=False,
            xgap=3,
            ygap=3,
            hoverongaps=False,
            hovertemplate="<b>%{y}</b><br>%{x}: %{z:.0f}% of the limit used<extra></extra>",
        )
    )
    figure.update_layout(
        title="Share of each monthly budget used (%)", height=max(200, 70 + 40 * len(pivot.index))
    )
    _label_cells(figure, values, texts, months, list(pivot.index), BUDGET_SCALES[bool(dark)], 200)
    _style_figure(figure)
    figure.update_xaxes(type="category", side="top", title_text=None)
    figure.update_yaxes(autorange="reversed", showgrid=False, title_text=None)
    return figure


def make_net_worth_chart(history: List[Dict[str, Any]]):
    if not history:
        return _empty("No balance history yet")
    frame = pd.DataFrame(history)
    figure = go.Figure()
    for column, name, color, width in (
        ("net_worth", "Net worth (what you own minus what you owe)", FINANCIAL_COLORS["savings"], 3),
        ("assets", "What you own", FINANCIAL_COLORS["income"], 1.5),
        ("liabilities", "What you owe", FINANCIAL_COLORS["higher_spending"], 1.5),
    ):
        figure.add_trace(
            go.Scatter(
                x=frame["date"],
                y=frame[column].astype(float),
                name=name,
                mode="lines",
                line={"color": color, "width": width},
                hovertemplate=f"<b>%{{x|%b %-d, %Y}}</b><br>{name.split(' (')[0]}: $%{{y:,.2f}}<extra></extra>",
            )
        )
    figure.update_layout(title="Net worth over time", legend=LEGEND_TOP)
    _style_figure(figure)
    _dollar_axis(figure)
    figure.update_xaxes(title_text=None)
    return figure


def make_income_chart(months: List[Dict[str, Any]]):
    if not months:
        return _empty("No income history available")
    keys = [item["month"] for item in months]
    figure = go.Figure(
        go.Bar(
            x=keys,
            y=[float(item["income"]) for item in months],
            marker_color=FINANCIAL_COLORS["income"],
            customdata=[_month_label(key) for key in keys],
            text=[_money(item["income"]) for item in months],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="<b>%{customdata}</b><br>Income: $%{y:,.2f}<extra></extra>",
        )
    )
    figure.update_layout(title="Income by month", showlegend=False, bargap=_bar_gap(len(keys)))
    _style_figure(figure)
    _dollar_axis(figure).update_yaxes(rangemode="tozero")
    return _month_axis(figure, keys)


def make_cash_flow_timeline(events: List[Dict[str, Any]]):
    """Expected paydays and bills as bars, with the running balance as a step line."""
    if not events:
        return _empty("No bills or paydays expected")
    frame = pd.DataFrame(events).assign(amount=lambda data: data["amount"].astype(float),
                                        running=lambda data: data["running"].astype(float))
    figure = go.Figure()
    for kind, color in (("Payday", FINANCIAL_COLORS["income"]), ("Bill", FINANCIAL_COLORS["expense"])):
        rows = frame[frame["kind"] == kind]
        if not rows.empty:
            figure.add_bar(x=rows["date"], y=rows["amount"], name=kind, marker_color=color,
                           customdata=rows["label"], width=1000 * 60 * 60 * 14,
                           hovertemplate="<b>%{customdata}</b><br>%{x|%a, %b %-d}: $%{y:,.2f}<extra></extra>")
    figure.add_scatter(x=frame["date"], y=frame["running"], name="Running balance", mode="lines",
                       line={"shape": "hv", "color": FINANCIAL_COLORS["savings"], "width": 2.5},
                       hovertemplate="<b>%{x|%a, %b %-d}</b><br>After this: $%{y:,.2f}<extra></extra>")
    figure.add_hline(y=0, line_width=1, line_color=NEUTRAL_LINE)
    figure.update_layout(title="Next 30 days", barmode="relative", legend=LEGEND_TOP)
    _style_figure(figure)
    _dollar_axis(figure)
    figure.update_xaxes(title_text=None, tickformat="%b %-d")
    return figure


# --- Takeaways ---------------------------------------------------------------
# One plain sentence per chart, built from the same numbers the chart draws, so
# the point of the chart can be read without decoding it.


def cash_flow_takeaway(monthly_data: List[Dict[str, Any]]) -> str:
    if not monthly_data:
        return ""
    net = [(item["month"], float(item["inflow"] - item["outflow"])) for item in monthly_data]
    if len(net) == 1:
        month, value = net[0]
        return (f"In {_month_label(month)} you kept {_money(value)}." if value >= 0
                else f"In {_month_label(month)} you spent {_money(abs(value))} more than came in.")
    ahead = sum(1 for _, value in net if value >= 0)
    best_month, best = max(net, key=lambda item: item[1])
    return (f"More came in than went out in {ahead} of {len(net)} months. "
            f"Best month: {_month_label(best_month)} ({_money(best, signed=True)}).")


def category_takeaway(categories: List[Dict[str, Any]]) -> str:
    rows = [item for item in categories if item["amount"] > 0]
    if not rows:
        return ""
    total = sum(float(item["amount"]) for item in rows)
    top = max(rows, key=lambda item: item["amount"])
    return (f"{top['category']} is your biggest category: {_money(top['amount'])}, "
            f"{float(top['amount']) / total * 100:.0f}% of spending.")


def savings_rate_takeaway(monthly: List[Dict[str, Any]]) -> str:
    rated = [item for item in monthly if item["savings_rate"] is not None]
    if not rated:
        return ""
    latest = rated[-1]
    rate = float(latest["savings_rate"])
    sentence = (f"In {_month_label(latest['month'])} you kept {rate:.0f}% of your income." if rate >= 0
                else f"In {_month_label(latest['month'])} you spent {abs(rate):.0f}% more than you earned.")
    if len(rated) > 1:
        average = sum(float(item["savings_rate"]) for item in rated) / len(rated)
        sentence += f" Your average across {len(rated)} months is {average:.0f}%."
    return sentence


def running_total_takeaway(cumulative_data: List[Dict[str, Any]]) -> str:
    if not cumulative_data:
        return ""
    final = float(sorted(cumulative_data, key=lambda item: item["date"])[-1]["pnl"])
    if final >= 0:
        return f"Across these dates, {_money(final)} more came in than went out."
    return f"Across these dates, {_money(abs(final))} more went out than came in."


def merchant_takeaway(merchants: List[Dict[str, Any]]) -> str:
    if not merchants:
        return ""
    top = max(merchants, key=lambda item: item["amount"])
    return (f"{_short(top['merchant'], 40)} is where the most money went: {_money(top['amount'])}, "
            f"{float(top.get('share', 0)):.0f}% of spending.")


def week_of_month_takeaway(buckets: List[Dict[str, Any]]) -> str:
    rows = [item for item in buckets if item["average_per_day"] > 0]
    if len(rows) < 2:
        return ""
    top = max(rows, key=lambda item: item["average_per_day"])
    return f"{top['bucket']} is your most expensive stretch, at {_money(top['average_per_day'])} a day."


def fixed_flexible_takeaway(months: List[Dict[str, Any]]) -> str:
    fixed = sum(float(item["fixed"]) for item in months)
    flexible = sum(float(item["flexible"]) for item in months)
    if not fixed + flexible:
        return ""
    return (f"{fixed / (fixed + flexible) * 100:.0f}% of your spending is fixed bills; "
            f"the other {_money(flexible)} is where you have room to adjust.")


def comparison_takeaway(comparisons: List[Dict[str, Any]]) -> str:
    if len(comparisons) < 2:
        return ""
    rows = _category_changes(comparisons)
    if not rows:
        return "Spending by category was the same in both periods."
    parts = []
    if rows[-1]["change"] > 0:
        parts.append(f"Biggest increase: {rows[-1]['category']} ({_money(rows[-1]['change'], signed=True)}).")
    if rows[0]["change"] < 0:
        parts.append(f"Biggest drop: {rows[0]['category']} ({_money(rows[0]['change'], signed=True)}).")
    return " ".join(parts)
