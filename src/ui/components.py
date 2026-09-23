from __future__ import annotations

import html
from datetime import date
from typing import Any, Dict, Iterable, List

import pandas as pd
import streamlit as st

from src.domain.models import Transaction


def inject_app_styles() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 2rem; padding-bottom: 4rem; }
        [data-testid="stMetric"] {
            background: linear-gradient(145deg, rgba(30,41,59,.72), rgba(15,23,42,.72));
            border: 1px solid rgba(148,163,184,.18);
            border-radius: 14px;
            padding: 1rem;
        }
        [data-testid="stMetricValue"] { font-size: clamp(1.55rem, 2.8vw, 2.25rem); }
        .fb-period { color: #94A3B8; margin-top: -.45rem; margin-bottom: 1.25rem; }
        .fb-card {
            border: 1px solid rgba(148,163,184,.22);
            border-radius: 12px;
            padding: .85rem 1rem;
            margin-bottom: .65rem;
            background: rgba(30,41,59,.45);
        }
        .fb-card-top { display:flex; justify-content:space-between; gap:1rem; font-weight:650; }
        .fb-card-meta { color:#94A3B8; font-size:.85rem; margin-top:.3rem; }
        .fb-positive { color:#38BDF8; }
        .fb-negative { color:#FB923C; }
        .fb-mobile-transactions { display:none; }
        .fb-progress-label { display:flex; justify-content:space-between; gap:1rem; }
        .fb-feature-intro {
            border-left: 3px solid #60A5FA;
            padding: .7rem 1rem;
            margin: .25rem 0 1.25rem;
            color: #CBD5E1;
            background: rgba(30,41,59,.35);
            border-radius: 0 10px 10px 0;
        }
        @media (max-width: 640px) {
            .block-container { padding-left: 1rem; padding-right: 1rem; padding-top: 1.2rem; }
            .st-key-desktop_transactions { display:none; }
            .fb-mobile-transactions { display:block; }
            h1 { font-size: 2rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_feature_intro(title: str, description: str) -> None:
    st.markdown(
        f'<div class="fb-feature-intro"><strong>{html.escape(title)}</strong><br>'
        f'{html.escape(description)}</div>',
        unsafe_allow_html=True,
    )


def format_currency(value: float) -> str:
    prefix = "-" if value < 0 else ""
    return f"{prefix}${abs(value):,.2f}"


def format_metric(metric: Dict[str, Any]) -> str:
    if metric["format"] == "currency":
        return format_currency(metric["value"])
    if metric["format"] == "percent":
        return f"{metric['value']:,.1f}%"
    return f"{int(metric['value']):,}"


def render_metrics(metrics: List[Dict[str, Any]]) -> None:
    columns = st.columns(len(metrics))
    for column, metric in zip(columns, metrics):
        column.metric(
            metric["label"],
            format_metric(metric),
            help=metric.get("help"),
        )


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


def render_transaction_table(transactions: List[Transaction], height: int = 520) -> None:
    frame = transaction_frame(transactions)
    with st.container(key="desktop_transactions"):
        st.dataframe(
            frame,
            width="stretch",
            height=height,
            hide_index=True,
            column_order=["Date", "Amount", "Category", "Description", "Account", "Type"],
            column_config={
                "Date": st.column_config.DateColumn(format="MMM D, YYYY", width="small"),
                "Amount": st.column_config.NumberColumn(format="$%.2f", width="small"),
                "Category": st.column_config.TextColumn(width="medium"),
                "Description": st.column_config.TextColumn(width="large"),
                "Account": st.column_config.TextColumn(width="medium"),
                "Type": st.column_config.TextColumn(width="small"),
            },
        )

    cards = []
    for transaction in transactions[:50]:
        amount_class = "fb-positive" if transaction.amount > 0 else "fb-negative"
        cards.append(
            (
                '<div class="fb-card"><div class="fb-card-top">'
                '<span>{description}</span><span class="{amount_class}">{amount}</span>'
                '</div><div class="fb-card-meta">{date} · {category} · {account}</div></div>'
            ).format(
                description=html.escape(transaction.description),
                amount_class=amount_class,
                amount=format_currency(transaction.amount),
                date=transaction.date.strftime("%b %-d, %Y"),
                category=html.escape(transaction.category),
                account=html.escape(transaction.account_name),
            )
        )
    if len(transactions) > 50:
        cards.append(
            f'<p class="fb-card-meta">Showing the newest 50 of {len(transactions):,} transactions on mobile.</p>'
        )
    st.markdown(
        '<div class="fb-mobile-transactions">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


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
