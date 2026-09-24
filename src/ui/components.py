from __future__ import annotations

import html
from datetime import date
from typing import Any, Dict, Iterable, List

import pandas as pd
import streamlit as st

from src.domain.models import Transaction


def inject_app_styles() -> None:
    # light-dark() follows the color-scheme Streamlit sets for the active theme, so
    # these colors switch together with the built-in light and dark themes.
    st.markdown(
        """
        <style>
        :root {
            --fb-surface: light-dark(#FFFFFF, #1B1C1A);
            --fb-surface-2: light-dark(#EFECE5, #262723);
            --fb-line: light-dark(#E3DED4, #33342F);
            --fb-ink: light-dark(#1C1B18, #F1EFE9);
            --fb-muted: light-dark(#5F5A51, #A7A399);
            --fb-income: light-dark(#1F5FA8, #7DB2F2);
            --fb-spend: light-dark(#B8500C, #F29A5E);
            --fb-focus: light-dark(#1F5FA8, #7DB2F2);
        }
        .block-container { padding-top: 2rem; padding-bottom: 4rem; max-width: 1360px; }
        [data-testid="stMainBlockContainer"] { font-variant-numeric: tabular-nums; }
        h1 { letter-spacing: -0.015em; }
        .st-key-auth_shell { max-width: 34rem; margin-inline: auto; }
        :is(button, input, select, textarea, [role="radio"], [role="combobox"]):focus-visible {
            outline: 3px solid var(--fb-focus) !important;
            outline-offset: 2px;
        }

        /* Page header */
        .fb-page-sub { color: var(--fb-muted); margin: -.6rem 0 .25rem; font-size: 1rem; }
        .fb-period { color: var(--fb-muted); margin: 0 0 .75rem; }

        /* Cards: metrics and every container keyed fbcard_* */
        [data-testid="stMetric"] {
            background: var(--fb-surface);
            border: 1px solid var(--fb-line);
            border-radius: 14px;
            padding: 1rem 1.1rem;
        }
        [data-testid="stMetricLabel"] p { font-weight: 600; }
        [data-testid="stMetricLabel"] * { white-space: normal !important; overflow: visible !important; }
        [data-testid="stMetricValue"] { font-size: clamp(1.35rem, 1.9vw, 2rem) !important; }
        [data-testid="stMetricValue"] * { overflow: visible !important; text-overflow: clip !important; }
        [class*="st-key-fbcard"] { background: var(--fb-surface); }
        .fb-card-title { font-size: 1.05rem; font-weight: 600; margin: 0; }
        .fb-card-sub { color: var(--fb-muted); font-size: .85rem; margin: .1rem 0 0; }

        /* Filter bar */
        .st-key-fbcard_filters [data-testid="stHorizontalBlock"] { align-items: center; }
        .fb-chips { display: flex; flex-wrap: wrap; gap: .4rem; align-items: center; min-height: 2rem; }
        .fb-chip {
            display: inline-flex; align-items: center; min-height: 1.9rem; padding: 0 .75rem;
            border-radius: 999px; background: var(--fb-surface-2); font-size: .85rem;
        }
        .fb-count { font-size: .9rem; color: var(--fb-muted); margin-right: .25rem; }
        .fb-count strong { color: var(--fb-ink); font-weight: 600; }

        /* Plain-language insights */
        .fb-insights {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(15rem, 1fr));
            gap: .75rem;
            margin: .25rem 0 1rem;
        }
        .fb-insight {
            margin: 0;
            border-radius: 12px;
            padding: .85rem 1rem;
            background: var(--fb-surface-2);
            line-height: 1.45;
        }

        /* Sidebar menu */
        .fb-brand { display: flex; align-items: center; gap: .6rem; margin: .1rem 0 .6rem; }
        .fb-brand-name { font-family: Newsreader, Georgia, serif; font-size: 1.45rem; font-weight: 600; }
        .fb-mark {
            position: relative; width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0;
            background: var(--fb-ink); color: var(--fb-surface); font-weight: 700;
            display: inline-flex; align-items: center; justify-content: center; font-size: .95rem;
        }
        .fb-mark::after {
            content: ""; position: absolute; right: 5px; bottom: 5px; width: 6px; height: 6px;
            border-radius: 50%; background: #E08A3C;
        }
        .fb-menu-label {
            font-size: .75rem; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
            color: var(--fb-muted); margin: .5rem 0 .1rem .75rem;
        }
        .st-key-nav_menu [data-testid="stElementContainer"], .st-key-nav_menu .stRadio { width: 100%; }
        .st-key-nav_menu [role="radiogroup"] { gap: 2px; width: 100%; }
        .st-key-nav_menu [role="radiogroup"] > div { width: 100%; }
        .st-key-nav_menu [data-testid="stRadioOption"] {
            box-sizing: border-box; width: 100%; min-height: 44px; margin: 0; padding: 0 .75rem;
            display: flex; align-items: center; border-radius: 10px; cursor: pointer;
        }
        .st-key-nav_menu [data-testid="stRadioOption"] > div > div:first-child:not([data-testid]) { display: none; }
        .st-key-nav_menu [data-testid="stRadioOption"]:hover { background: var(--fb-surface-2); }
        .st-key-nav_menu [data-testid="stRadioOption"][data-selected="true"] { background: var(--fb-ink); }
        .st-key-nav_menu [data-testid="stRadioOption"][data-selected="true"] p { color: var(--fb-surface); font-weight: 600; }
        .st-key-nav_menu [data-testid="stRadioOption"]:has(input:focus-visible) {
            outline: 3px solid var(--fb-focus); outline-offset: 2px;
        }
        .st-key-nav_menu [data-testid="stRadioOption"] p { font-size: 1rem; font-weight: 500; }
        .st-key-menu_actions { gap: .1rem; }
        [data-testid="stSidebar"] .stButton button { justify-content: flex-start; }

        /* Transactions */
        .fb-card {
            border: 1px solid var(--fb-line);
            border-radius: 12px;
            padding: .85rem 1rem;
            margin-bottom: .65rem;
            background: var(--fb-surface);
        }
        .fb-card-top { display:flex; justify-content:space-between; gap:1rem; font-weight:600; }
        .fb-card-meta { color: var(--fb-muted); font-size:.85rem; margin-top:.3rem; }
        .fb-positive { color: var(--fb-income); }
        .fb-negative { color: var(--fb-spend); }
        .fb-mobile-transactions, .st-key-mobile_history_controls { display:none; }
        .fb-progress-label { display:flex; justify-content:space-between; gap:1rem; }
        .fb-budget-meta { color: var(--fb-muted); font-size:.85rem; margin: -.4rem 0 .9rem; }

        @media (max-width: 640px) {
            .block-container { padding-left: 1rem; padding-right: 1rem; padding-top: 1.2rem; }
            .st-key-desktop_transactions { display:none; }
            .fb-mobile-transactions, .st-key-mobile_history_controls { display:block; }
            h1 { font-size: 2rem !important; }
            [data-testid="stMetricValue"] { font-size: 1.45rem !important; }
            /* Two-up key numbers and filter buttons instead of one long column. */
            :is(.st-key-fb_metrics, .st-key-filter_buttons) [data-testid="stHorizontalBlock"] {
                display: grid !important; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .6rem;
            }
            :is(.st-key-fb_metrics, .st-key-filter_buttons) [data-testid="stColumn"] {
                width: auto !important; min-width: 0 !important;
            }
            .st-key-fbcard_filters [data-testid="stButtonGroup"] [role="radiogroup"] { flex-wrap: wrap; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(title: str, description: str | None = None) -> None:
    st.title(title)
    if description:
        st.markdown(f'<p class="fb-page-sub">{html.escape(description)}</p>', unsafe_allow_html=True)


def render_card_header(title: str, subtitle: str | None = None) -> None:
    sub = f'<p class="fb-card-sub">{html.escape(subtitle)}</p>' if subtitle else ""
    st.markdown(f'<p class="fb-card-title">{html.escape(title)}</p>{sub}', unsafe_allow_html=True)


def format_currency(value: float) -> str:
    prefix = "-" if value < 0 else ""
    return f"{prefix}${abs(value):,.2f}"


def format_metric(metric: Dict[str, Any]) -> str:
    if metric["format"] == "currency":
        return format_currency(metric["value"])
    if metric["format"] == "percent":
        return f"{metric['value']:,.1f}%"
    return f"{int(metric['value']):,}"


def format_delta(metric: Dict[str, Any]) -> str | None:
    delta = metric.get("delta")
    if delta is None:
        return None
    if metric["format"] == "currency":
        return ("+" if delta >= 0 else "-") + format_currency(abs(delta))
    if metric["format"] == "percent":
        return f"{delta:+,.1f} pts"
    return f"{int(delta):+,}"


def render_metrics(metrics: List[Dict[str, Any]], delta_caption: str | None = None) -> None:
    columns = st.container(key="fb_metrics").columns(len(metrics))
    for column, metric in zip(columns, metrics):
        column.metric(
            metric["label"],
            format_metric(metric),
            delta=format_delta(metric),
            delta_color=metric.get("delta_color", "normal"),
            help=metric.get("help"),
        )
    if delta_caption and any(metric.get("delta") is not None for metric in metrics):
        st.caption(delta_caption)


def render_insights(insights: List[str]) -> None:
    if not insights:
        return
    cards = "".join(f'<p class="fb-insight">{html.escape(text)}</p>' for text in insights)
    st.markdown(f'<div class="fb-insights">{cards}</div>', unsafe_allow_html=True)


def period_label(transactions: List[Transaction]) -> str:
    if not transactions:
        return "No transactions match the current filters."
    start = min(item.date for item in transactions)
    end = max(item.date for item in transactions)
    accounts = len({item.account_name for item in transactions})
    account_text = f"{accounts} account" + ("s" if accounts != 1 else "")
    return (
        f"{start.strftime('%b %-d, %Y')}–{end.strftime('%b %-d, %Y')} · "
        f"{len(transactions):,} transactions · {account_text}"
    )


def transaction_frame(transactions: Iterable[Transaction]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Date": transaction.date,
                "Amount": transaction.amount,
                "Category": transaction.category,
                "Description": transaction.description,
                "Account": transaction.account_name,
                "Type": transaction.account_type,
            }
            for transaction in transactions
        ]
    )


def _status_column(transactions: List[Transaction]) -> List[str] | None:
    if not any(transaction.pending for transaction in transactions):
        return None
    return ["Pending" if transaction.pending else "Posted" for transaction in transactions]


def render_transaction_table(
    transactions: List[Transaction], height: int = 520, selection_key: str | None = None
) -> int | None:
    """Show transactions as a table on wide screens and as cards on phones.

    With ``selection_key``, a row can be clicked and its position is returned.
    """
    frame = transaction_frame(transactions)
    column_order = ["Date", "Description", "Amount", "Category", "Account", "Type"]
    status = _status_column(transactions)
    if status:
        frame["Status"] = status
        column_order.insert(1, "Status")
    selection = {"on_select": "rerun", "selection_mode": "single-row", "key": selection_key} if selection_key else {}
    with st.container(key="desktop_transactions"):
        event = st.dataframe(
            frame,
            width="stretch",
            height=height,
            hide_index=True,
            column_order=column_order,
            **selection,
            column_config={
                "Date": st.column_config.DateColumn(format="MMM D, YYYY", width=110),
                "Amount": st.column_config.NumberColumn(format="dollar", width=100),
                "Category": st.column_config.TextColumn(width="medium"),
                "Description": st.column_config.TextColumn(width="medium"),
                "Account": st.column_config.TextColumn(width="medium"),
                "Type": st.column_config.TextColumn(width="small"),
            },
        )

    page_size = 50
    page_count = max(1, (len(transactions) + page_size - 1) // page_size)
    if st.session_state.get("mobile_history_page", 1) > page_count:
        st.session_state.mobile_history_page = page_count
    with st.container(key="mobile_history_controls"):
        page = st.selectbox(
            "Mobile history page",
            options=list(range(1, page_count + 1)),
            format_func=lambda number: f"Page {number} of {page_count}",
            key="mobile_history_page",
        )
        st.caption(f"Transactions {(page - 1) * page_size + 1}–{min(page * page_size, len(transactions))} of {len(transactions):,}")
    cards = []
    for transaction in transactions[(page - 1) * page_size:page * page_size]:
        amount_class = "fb-positive" if transaction.amount > 0 else "fb-negative"
        cards.append(
            (
                '<div class="fb-card"><div class="fb-card-top">'
                '<span>{description}</span><span class="{amount_class}">{amount}</span>'
                '</div><div class="fb-card-meta">{date}{pending} · {category} · {account}</div></div>'
            ).format(
                pending=" · Pending" if transaction.pending else "",
                description=html.escape(transaction.description),
                amount_class=amount_class,
                amount=format_currency(transaction.amount),
                date=transaction.date.strftime("%b %-d, %Y"),
                category=html.escape(transaction.category),
                account=html.escape(transaction.account_name),
            )
        )
    st.markdown(
        '<div class="fb-mobile-transactions">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )
    if not selection_key:
        return None
    try:
        rows = event.selection.rows
    except AttributeError:
        return None
    return rows[0] if rows else None


def transactions_to_csv(transactions: Iterable[Transaction]) -> bytes:
    frame = transaction_frame(transactions)
    for column in ("Category", "Description", "Account", "Type"):
        frame[column] = frame[column].map(
            lambda value: "'" + value
            if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))
            else value
        )
    return frame.to_csv(index=False).encode("utf-8")


def render_progress(label: str, current: float, target: float) -> None:
    ratio = min(max(current / target if target else 0.0, 0.0), 1.0)
    st.markdown(
        f'<div class="fb-progress-label"><strong>{html.escape(label)}</strong>'
        f"<span>{format_currency(current)} of {format_currency(target)}</span></div>",
        unsafe_allow_html=True,
    )
    st.progress(ratio)
