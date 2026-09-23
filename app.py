from __future__ import annotations

import hashlib
import time
import uuid
from datetime import date

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.domain.models import Budget, CategoryRule, SavingsGoal
from src.repositories.supabase_repo import (
    SupabasePlaidRepository,
    SupabaseTransactionRepository,
)
from src.services.analytics import AnalyticsService
from src.services.categorizer import CATEGORIES, auto_categorize, categorize_with_custom_rules
from src.services.parser import BankStatementParser
from src.services.plaid_service import PlaidConfig, PlaidConfigurationError, PlaidService
from src.services.production import validate_production_configuration
from src.services.security import bind_session_owner, clear_private_session, read_backup
from src.services.supabase import (
    SupabaseAuth,
    SupabaseConfig,
    SupabaseDataClient,
    SupabaseError,
    has_verified_email,
    normalized_session,
)
from src.ui.charts import (
    make_budget_history_chart,
    make_cash_flow_sankey,
    make_category_bar_chart,
    make_category_donut_chart,
    make_category_heatmap,
    make_comparison_category_chart,
    make_comparison_delta_chart,
    make_comparison_summary_chart,
    make_daily_spending_chart,
    make_fixed_flexible_chart,
    make_income_chart,
    make_merchant_bar_chart,
    make_monthly_bar_chart,
    make_net_worth_chart,
    make_profit_loss_line_chart,
    make_savings_rate_chart,
    make_week_of_month_chart,
    make_weekday_chart,
)
from src.ui.components import (
    format_currency,
    inject_app_styles,
    period_label,
    render_feature_intro,
    render_insights,
    render_metrics,
    render_progress,
    render_transaction_table,
    transaction_frame,
    transactions_to_csv,
)
from src.ui.plaid_link import render_plaid_link


load_dotenv()
st.set_page_config(page_title="FinanceBuddy", page_icon="💸", layout="wide")
inject_app_styles()


def _streamlit_secrets():
    try:
        return dict(st.secrets)
    except Exception:
        return {}


app_secrets = _streamlit_secrets()
production_errors = validate_production_configuration(app_secrets)
if production_errors:
    st.title("💸 FinanceBuddy")
    st.error("FinanceBuddy cannot start because its production configuration is incomplete.")
    for production_error in production_errors:
        st.write(f"• {production_error}")
    st.caption("No secret values are displayed. Correct the named settings and restart the service.")
    st.stop()

supabase_config = SupabaseConfig.from_sources(app_secrets)
if not supabase_config.is_configured:
    st.title("💸 FinanceBuddy")
    st.error("Supabase must be configured before FinanceBuddy can be used.")
    st.caption(
        "Add the Supabase project URL and publishable key to Streamlit secrets. Supabase then "
        "handles account creation, email verification, password recovery, and database security."
    )
    st.code(
        "cp .streamlit/secrets.toml.example .streamlit/secrets.toml",
        language="bash",
    )
    st.stop()

auth = SupabaseAuth(supabase_config)


def _save_supabase_session(payload: dict) -> None:
    session_value = normalized_session(payload)
    if not session_value["access_token"] or not session_value["refresh_token"]:
        raise SupabaseError("Supabase did not return a complete login session.")
    st.session_state.supabase_session = session_value


def _clear_supabase_session() -> None:
    clear_private_session(st.session_state)


def _consume_auth_link() -> None:
    token_hash = st.query_params.get("token_hash")
    token_type = st.query_params.get("type")
    if not token_hash and not token_type:
        return
    # Remove one-time credentials from the address bar before rendering financial data.
    st.query_params.clear()
    try:
        payload = auth.verify_email_link(str(token_hash or ""), str(token_type or ""))
        if token_type == "email":
            _save_supabase_session(payload)
            st.session_state.pop("pending_verification_email", None)
        else:
            recovery_session = normalized_session(payload)
            if not recovery_session["access_token"] or not recovery_session["refresh_token"]:
                raise SupabaseError("Supabase did not return a complete recovery session.")
            _clear_supabase_session()
            st.session_state.recovery_link_session = recovery_session
    except SupabaseError:
        st.session_state.auth_link_error = True


def _render_authentication() -> None:
    with st.container(key="auth_shell"):
        st.title("💸 FinanceBuddy")
        st.subheader("Your finances stay private to your account")
        st.write(
            "Log in to see your spending, manage budgets, and plan your savings. "
            "New here? Create an account and verify your email to get started."
        )
        if st.session_state.pop("auth_link_error", None):
            st.error("This email link could not be used. Request a fresh email and try again.")
        if st.session_state.pop("unverified_email_error", None):
            st.warning("Verify your email address before accessing financial data.")
        if st.session_state.pop("session_expired_notice", None):
            st.info("Your session expired after 30 minutes without activity. Please log in again.")
        recovery_link_session = st.session_state.get("recovery_link_session")
        if recovery_link_session:
            st.info("Email verified. Choose a new password to finish recovering your account.")
            st.caption("Use at least 12 characters and enter the same password twice.")
            with st.form("finish_link_recovery"):
                new_password = st.text_input(
                    "New password", type="password", autocomplete="new-password"
                )
                confirm_password = st.text_input(
                    "Confirm new password", type="password", autocomplete="new-password"
                )
                reset_submitted = st.form_submit_button(
                    "Set new password", type="primary", width="stretch"
                )
            if reset_submitted:
                if len(new_password) < 12:
                    st.error("Use at least 12 characters for your password.")
                elif new_password != confirm_password:
                    st.error("The passwords do not match.")
                else:
                    try:
                        auth.update_password(recovery_link_session["access_token"], new_password)
                        _save_supabase_session(recovery_link_session)
                        st.session_state.pop("recovery_link_session", None)
                        st.rerun()
                    except (KeyError, SupabaseError) as error:
                        st.error(str(error))
            return
        auth_stage = st.radio(
            "Account access",
            ["Log in", "Create account", "Reset password"],
            horizontal=True,
            key="auth_stage",
        )
        if auth_stage == "Log in":
            with st.form("supabase_login"):
                login_email = st.text_input("Email", autocomplete="email")
                login_password = st.text_input(
                    "Password", type="password", autocomplete="current-password"
                )
                login_submitted = st.form_submit_button(
                    "Log in", type="primary", width="stretch"
                )
            if login_submitted:
                try:
                    _save_supabase_session(
                        auth.sign_in(login_email.strip().lower(), login_password)
                    )
                    st.rerun()
                except SupabaseError as error:
                    st.error(str(error))

        elif auth_stage == "Create account":
            st.caption("Use at least 12 characters. You will verify your email before opening financial data.")
            with st.form("supabase_signup"):
                signup_email = st.text_input("Email", autocomplete="email", key="signup_email")
                signup_password = st.text_input(
                    "Password",
                    type="password",
                    autocomplete="new-password",
                    key="signup_password",
                )
                signup_confirmation = st.text_input(
                    "Confirm password",
                    type="password",
                    autocomplete="new-password",
                    key="signup_confirmation",
                )
                signup_submitted = st.form_submit_button(
                    "Create secure account", width="stretch"
                )
            if signup_submitted:
                if len(signup_password) < 12:
                    st.error("Use at least 12 characters for your password.")
                elif signup_password != signup_confirmation:
                    st.error("The passwords do not match.")
                else:
                    try:
                        response = auth.sign_up(signup_email.strip().lower(), signup_password)
                        if response.get("access_token"):
                            _save_supabase_session(response)
                            st.rerun()
                        st.session_state.pending_verification_email = signup_email.strip().lower()
                        st.success(
                            "If this is a new account, check your email for a verification code. "
                            "If you already verified this address, use Log in instead."
                        )
                    except SupabaseError as error:
                        st.error(str(error))
            if st.session_state.get("pending_verification_email"):
                with st.form("verify_signup"):
                    verification_code = st.text_input("Email verification code")
                    verify_submitted = st.form_submit_button(
                        "Verify and continue", type="primary", width="stretch"
                    )
                if verify_submitted:
                    try:
                        _save_supabase_session(
                            auth.verify_signup_otp(
                                st.session_state.pending_verification_email,
                                verification_code.strip(),
                            )
                        )
                        st.session_state.pop("pending_verification_email", None)
                        st.rerun()
                    except SupabaseError as error:
                        st.error(str(error))
            with st.expander("Didn't receive the verification email?"):
                resend_email = st.text_input(
                    "Account email",
                    value=st.session_state.get("pending_verification_email", ""),
                    key="resend_email",
                )
                if st.button("Resend verification email", width="stretch"):
                    try:
                        auth.resend_signup_email(resend_email.strip().lower())
                        st.success(
                            "If this account still needs verification, check for a new email. "
                            "Already verified? Use Log in instead."
                        )
                    except SupabaseError as error:
                        st.error(str(error))

        else:
            st.caption("Request a recovery code, then enter it with a new password of at least 12 characters.")
            with st.form("request_recovery"):
                recovery_email = st.text_input("Account email", key="recovery_email_input")
                request_code = st.form_submit_button("Send recovery email", width="stretch")
            if request_code:
                try:
                    auth.send_recovery_email(recovery_email.strip().lower())
                    st.session_state.recovery_email = recovery_email.strip().lower()
                    st.success("Check your inbox for the Supabase recovery code.")
                except SupabaseError as error:
                    st.error(str(error))
            if st.session_state.get("recovery_email"):
                with st.form("finish_recovery"):
                    recovery_code = st.text_input("Recovery code")
                    new_password = st.text_input(
                        "New password", type="password", autocomplete="new-password"
                    )
                    confirm_password = st.text_input(
                        "Confirm new password", type="password", autocomplete="new-password"
                    )
                    reset_submitted = st.form_submit_button(
                        "Set new password", type="primary", width="stretch"
                    )
                if reset_submitted:
                    if len(new_password) < 12:
                        st.error("Use at least 12 characters for your password.")
                    elif new_password != confirm_password:
                        st.error("The passwords do not match.")
                    else:
                        try:
                            session_payload = auth.verify_recovery_otp(
                                st.session_state.recovery_email, recovery_code.strip()
                            )
                            auth.update_password(session_payload["access_token"], new_password)
                            _save_supabase_session(session_payload)
                            st.session_state.pop("recovery_email", None)
                            st.rerun()
                        except (KeyError, SupabaseError) as error:
                            st.error(str(error))
        st.caption(
            "Sign-in is secured by Supabase. Log out when you finish on a shared device."
        )


_consume_auth_link()
session = st.session_state.get("supabase_session")
user = None
if session and time.time() - st.session_state.get("last_activity_at", time.time()) > 30 * 60:
    _clear_supabase_session()
    st.session_state.session_expired_notice = True
    session = None
if session:
    try:
        if int(session.get("expires_at", 0)) <= time.time() + 60:
            _save_supabase_session(auth.refresh(session["refresh_token"]))
            session = st.session_state.supabase_session
        user = auth.get_user(session["access_token"])
    except (KeyError, SupabaseError):
        _clear_supabase_session()
        session = None

if not session or not user:
    _render_authentication()
    st.stop()

if not has_verified_email(user):
    _clear_supabase_session()
    st.session_state.unverified_email_error = True
    _render_authentication()
    st.stop()

user_id = str(user.get("id") or "")
if not user_id:
    _clear_supabase_session()
    st.error("Supabase returned an invalid user identity. Please log in again.")
    st.stop()

bind_session_owner(st.session_state, user_id)
st.session_state.last_activity_at = time.time()

data_client = SupabaseDataClient(supabase_config, session["access_token"])
repo = SupabaseTransactionRepository(data_client, user_id)
plaid_repo = SupabasePlaidRepository(data_client, user_id)


def apply_custom_rules(transactions):
    rules = repo.get_category_rules()
    for transaction in transactions:
        transaction.category = categorize_with_custom_rules(transaction.description, rules)
    return transactions


def parse_statement(upload, account_name: str, account_type: str):
    raw = upload.getvalue()
    cache_key = (hashlib.sha256(raw).hexdigest(), upload.name.lower().endswith(".pdf"),
                 account_name, account_type)
    cache = st.session_state.setdefault("statement_parse_cache", {})
    if cache_key not in cache:
        if len(cache) >= 6:
            cache.clear()
        cache[cache_key] = BankStatementParser.parse_file(
            raw, upload.name, account_name, account_type
        )
    parsed, metrics = cache[cache_key]
    return apply_custom_rules([item.model_copy(deep=True) for item in parsed]), metrics


def sync_import_account_name() -> None:
    defaults = {"Checking": "Primary Checking", "Credit Card": "Primary Credit Card"}
    current = st.session_state.get("import_account_name", "")
    if current in defaults.values() or not current.strip():
        st.session_state.import_account_name = defaults[st.session_state.import_account_type]


def clear_account_filter() -> None:
    st.session_state.dashboard_accounts = []


def select_all_accounts(account_options: list[str]) -> None:
    st.session_state.dashboard_accounts = account_options


DATE_PRESETS = [
    "All time",
    "This month",
    "Last month",
    "Last 30 days",
    "Last 3 months",
    "Year to date",
    "Last 12 months",
    "Custom",
]


def reset_dashboard_filters() -> None:
    for key in (
        "dashboard_dates",
        "dashboard_date_preset",
        "dashboard_accounts",
        "dashboard_categories",
        "dashboard_transaction_types",
        "dashboard_search",
        "dashboard_minimum_amount",
        "dashboard_include_transfers",
    ):
        st.session_state.pop(key, None)


def apply_date_preset(min_date: date, max_date: date) -> None:
    preset = st.session_state.dashboard_date_preset
    if preset != "Custom":
        st.session_state.dashboard_dates = AnalyticsService.preset_range(preset, min_date, max_date)


def mark_custom_dates() -> None:
    st.session_state.dashboard_date_preset = "Custom"


def navigate_to(page: str, section: str | None = None) -> None:
    st.session_state.nav_page = page
    if section and page == "Accounts":
        st.session_state.accounts_view = section
    if section and page == "Settings":
        st.session_state.settings_view = section


def render_start_actions() -> None:
    st.subheader("Get started with your financial history")
    st.write("Choose how to add your first transactions. You can use both methods later.")
    bank, statement = st.columns(2)
    bank.button(
        "Connect a bank",
        type="primary",
        width="stretch",
        on_click=navigate_to,
        args=("Accounts", "Bank connections"),
    )
    statement.button(
        "Upload a statement",
        width="stretch",
        on_click=navigate_to,
        args=("Accounts", "Import statement"),
    )
    st.caption("A bank connection syncs through Plaid. A CSV or text-based PDF is previewed before saving.")


def render_onboarding() -> None:
    with st.container(border=True):
        heading, close = st.columns([5, 1])
        heading.subheader("Welcome to FinanceBuddy 👋")
        heading.caption("Your financial records are restricted to your signed-in account.")
        if close.button(
            "Hide guide",
            help="Dismiss this guide. You can reopen it anytime with “How FinanceBuddy works.”",
            width="stretch",
        ):
            repo.set_setting("onboarding_complete", "true")
            st.session_state.show_onboarding = False
            st.rerun()

        first, second, third, fourth = st.columns(4)
        first.markdown("**1 · Add activity**")
        first.caption("Use Accounts to connect a bank or preview a statement before saving.")
        second.markdown("**2 · Review**")
        second.caption("Use **Transactions** to correct categories, teach merchant rules, or split a purchase.")
        third.markdown("**3 · Understand**")
        third.caption("Use Overview's tabs and filters to explore trends, habits, recurring charges, income, and balances.")
        fourth.markdown("**4 · Plan**")
        fourth.caption("Create limits and targets in Plan, or compare statement periods.")
        st.info(
            "Best first step: import one statement with **Append new transactions**. "
            "Exact duplicates are ignored, and nothing is saved until you confirm the preview."
        )


def _selected_points(event, axis: str) -> list:
    """Values the viewer clicked on a chart, or an empty list."""
    try:
        points = event["selection"]["points"]
    except (KeyError, TypeError):
        return []
    return [point.get(axis) for point in points if point.get(axis) is not None]


def _money_table(frame: pd.DataFrame, money_columns: list[str], **kwargs) -> None:
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config={
            **{column: st.column_config.NumberColumn(format="$%.2f") for column in money_columns},
            **kwargs.pop("column_config", {}),
        },
        **kwargs,
    )


def load_balances() -> tuple[list[dict], list[dict]]:
    """Connected-account balances and history; empty when unavailable."""
    if not PlaidConfig.from_sources(_streamlit_secrets()).is_configured:
        return [], []
    try:
        accounts = plaid_repo.get_accounts()
    except SupabaseError:
        return [], []
    try:
        history = plaid_repo.get_balance_history()
    except SupabaseError:
        history = []
    return accounts, history


def render_spending_drilldown(transactions, category_event, month_event) -> None:
    categories = [item["category"] for item in AnalyticsService.category_summary(transactions)]
    clicked_categories = [str(value) for value in _selected_points(category_event, "y")]
    clicked_months = [str(value)[:7] for value in _selected_points(month_event, "x")]
    chosen = st.selectbox(
        "Category to explore",
        ["All categories"] + categories,
        key="drilldown_category",
        help="Or click a bar in the category or monthly chart. Double-click a chart to clear its selection.",
    )
    selected_categories = clicked_categories or ([] if chosen == "All categories" else [chosen])
    if not selected_categories and not clicked_months:
        return
    rows = [
        item
        for item in transactions
        if item.amount < 0
        and (not selected_categories or item.category in selected_categories)
        and (not clicked_months or item.date.strftime("%Y-%m") in clicked_months)
    ]
    scope = " · ".join(
        part for part in (", ".join(selected_categories), ", ".join(clicked_months)) if part
    )
    if not rows:
        st.caption(f"No spending found for {scope}.")
        return
    total = sum(abs(item.amount) for item in rows)
    with st.container(border=True):
        st.markdown(f"**{scope}**")
        first, second, third = st.columns(3)
        first.metric("Spent", format_currency(total))
        second.metric("Transactions", f"{len(rows):,}")
        third.metric("Average", format_currency(total / len(rows)))
        details = AnalyticsService.subcategory_summary(rows)
        merchants = AnalyticsService.merchant_summary(rows, limit=5)
        left, right = st.columns(2)
        with left:
            st.markdown("###### Top merchants")
            _money_table(
                pd.DataFrame(merchants)[["merchant", "amount", "visits"]].rename(
                    columns={"merchant": "Merchant", "amount": "Spend", "visits": "Purchases"}
                ),
                ["Spend"],
            )
        with right:
            if details:
                st.markdown("###### Bank detail")
                _money_table(
                    pd.DataFrame(details).rename(
                        columns={"subcategory": "Detail", "amount": "Spend", "transactions": "Transactions"}
                    ),
                    ["Spend"],
                )
        st.dataframe(
            transaction_frame(sorted(rows, key=lambda item: item.date, reverse=True)),
            width="stretch",
            hide_index=True,
            column_config={
                "Amount": st.column_config.NumberColumn(format="$%.2f"),
                "Date": st.column_config.DateColumn(format="MMM D, YYYY"),
            },
        )


def render_overview_summary(analysis, categories, history) -> None:
    monthly = AnalyticsService.monthly_breakdown(analysis)
    left, right = st.columns(2)
    with left:
        month_event = st.plotly_chart(
            make_monthly_bar_chart(monthly),
            width="stretch",
            on_select="rerun",
            selection_mode="points",
            key="overview_month_chart",
        )
    with right:
        st.plotly_chart(
            make_category_donut_chart({item["category"]: item["amount"] for item in categories}),
            width="stretch",
        )

    st.plotly_chart(
        make_profit_loss_line_chart(AnalyticsService.cumulative_cash_flow(analysis)),
        width="stretch",
    )
    st.caption(
        "Gain/loss is cumulative transaction cash flow from zero at the beginning of the selected "
        "period. It is not an investment return or live account balance."
    )

    uncategorized = next(
        (item for item in categories if item["category"] == "Uncategorized"), None
    )
    if uncategorized:
        st.warning(
            f"{uncategorized['transactions']} transaction(s), totaling "
            f"{format_currency(uncategorized['amount'])}, still need a category. "
            "Review them on the Transactions page."
        )

    st.subheader("Explore spending")
    st.caption("Click a category or month bar, or pick a category, to see the transactions behind it.")
    category_event = st.plotly_chart(
        make_category_bar_chart(categories),
        width="stretch",
        on_select="rerun",
        selection_mode="points",
        key="overview_category_chart",
    )
    render_spending_drilldown(analysis, category_event, month_event)

    st.subheader("Category summary")
    st.dataframe(
        pd.DataFrame(categories).rename(
            columns={
                "category": "Category",
                "amount": "Spend",
                "transactions": "Transactions",
                "share": "Share (%)",
                "average": "Average",
            }
        ),
        width="stretch",
        hide_index=True,
        column_config={
            "Spend": st.column_config.NumberColumn(format="$%.2f"),
            "Share (%)": st.column_config.NumberColumn(format="%.1f%%"),
            "Average": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

    st.subheader("What changed")
    changes = AnalyticsService.category_changes(analysis)
    if changes:
        st.caption(
            f"{changes[0]['current_month']} compared with {changes[0]['previous_month']}. "
            "A month still in progress is compared with the same days of the month before."
        )
        insight_columns = st.columns(min(3, len(changes)))
        for column, item in zip(insight_columns, changes[:3]):
            direction = "up" if item["change"] > 0 else "down"
            column.metric(
                item["category"],
                format_currency(abs(item["change"])),
                delta=f"{direction} vs {item['previous_month']}",
                delta_color="inverse" if item["change"] > 0 else "normal",
            )
    else:
        st.caption("Import at least two comparable months of activity to see category changes.")

    unusual = AnalyticsService.unusual_expenses(analysis)
    forecast = AnalyticsService.cash_flow_forecast(history)
    first, second = st.columns(2)
    with first:
        st.markdown("#### Unusual expenses")
        if unusual:
            for transaction in unusual:
                st.write(f"{transaction.description} · {format_currency(abs(transaction.amount))}")
        else:
            st.caption("No unusually large expenses detected in this selection.")
    with second:
        st.markdown("#### Next-month estimate")
        if forecast["months"]:
            st.metric(
                "Estimated money out",
                format_currency(forecast["outflow"]),
                help="Recurring charges expected in the next 30 days plus your average variable spending.",
            )
            st.caption(
                f"Likely range {format_currency(forecast['outflow_low'])}–"
                f"{format_currency(forecast['outflow_high'])}: "
                f"{format_currency(forecast['recurring'])} in expected recurring charges plus "
                f"{format_currency(forecast['variable'])} of typical variable spending, based on "
                f"the latest {forecast['months']} complete month(s) of history."
            )
        else:
            st.caption("More history is needed for an estimate.")


def render_overview_trends(analysis, start_date: date, end_date: date) -> None:
    st.plotly_chart(
        make_savings_rate_chart(AnalyticsService.monthly_savings_rate(analysis)), width="stretch"
    )

    st.subheader("Fixed costs vs flexible spending")
    split = AnalyticsService.fixed_vs_flexible(analysis)
    first, second, third = st.columns(3)
    first.metric("Fixed costs", format_currency(split["fixed"]),
                 help="Housing, utilities, insurance, debt payments, and education.")
    second.metric("Flexible spending", format_currency(split["flexible"]),
                  help="Everything else: day-to-day spending you have the most control over.")
    third.metric("Fixed share", f"{split['fixed_share']:.0f}%")
    st.plotly_chart(make_fixed_flexible_chart(split["months"]), width="stretch")

    st.subheader("Daily spending")
    pace = AnalyticsService.spending_pace(analysis, start_date, end_date)
    first, second, third = st.columns(3)
    first.metric("Average per day", format_currency(pace["average_daily"]),
                 help=f"Across all {pace['days']} days in the selected range.")
    second.metric(
        "Last 7 days, per day",
        format_currency(pace["last_7_days"]),
        delta=format_currency(pace["last_7_days"] - pace["average_daily"]) + " vs average",
        delta_color="inverse",
    )
    third.metric("Last 30 days, per day", format_currency(pace["last_30_days"]))
    st.plotly_chart(
        make_daily_spending_chart(AnalyticsService.daily_spending(analysis, start_date, end_date)),
        width="stretch",
    )

    st.plotly_chart(
        make_cash_flow_sankey(
            AnalyticsService.income_sources(analysis), AnalyticsService.category_expenses(analysis)
        ),
        width="stretch",
    )

    month_count = len({item.date.strftime("%Y-%m") for item in analysis})
    with st.expander("Monthly category trend", expanded=month_count >= 3):
        if month_count >= 2:
            st.plotly_chart(
                make_category_heatmap(AnalyticsService.monthly_category_expenses(analysis)),
                width="stretch",
            )
        else:
            st.caption("Import at least two months to view this trend.")


def render_overview_habits(analysis, start_date: date, end_date: date) -> None:
    weekday = AnalyticsService.weekday_spending(analysis, start_date, end_date)
    split = AnalyticsService.weekend_vs_weekday(weekday)
    if split and split["weekday"]:
        direction = "more" if split["difference"] >= 0 else "less"
        st.info(
            f"You spend {format_currency(split['weekend'])} per weekend day and "
            f"{format_currency(split['weekday'])} per weekday: {abs(split['difference']):.0f}% "
            f"{direction} on weekends."
        )
    left, right = st.columns(2)
    with left:
        st.plotly_chart(make_weekday_chart(weekday), width="stretch")
    with right:
        st.plotly_chart(
            make_week_of_month_chart(
                AnalyticsService.week_of_month_spending(analysis, start_date, end_date)
            ),
            width="stretch",
        )

    st.subheader("Top merchants")
    merchants = AnalyticsService.merchant_summary(analysis, limit=10)
    if merchants:
        st.plotly_chart(make_merchant_bar_chart(merchants), width="stretch")
        _money_table(
            pd.DataFrame(merchants).rename(
                columns={
                    "merchant": "Merchant",
                    "amount": "Spend",
                    "visits": "Purchases",
                    "average": "Average ticket",
                    "share": "Share (%)",
                    "category": "Category",
                    "last_date": "Last purchase",
                }
            ),
            ["Spend", "Average ticket"],
            column_config={
                "Share (%)": st.column_config.NumberColumn(format="%.1f%%"),
                "Last purchase": st.column_config.DateColumn(format="MMM D, YYYY"),
            },
        )
    else:
        st.caption("No spending in this selection.")

    channels = AnalyticsService.payment_channel_summary(analysis)
    locations = AnalyticsService.location_summary(analysis)
    if channels or locations:
        left, right = st.columns(2)
        with left:
            if channels:
                st.markdown("#### Online vs in store")
                _money_table(
                    pd.DataFrame(channels).rename(
                        columns={"channel": "Channel", "amount": "Spend",
                                 "transactions": "Transactions", "share": "Share (%)"}
                    ),
                    ["Spend"],
                    column_config={"Share (%)": st.column_config.NumberColumn(format="%.1f%%")},
                )
        with right:
            if locations:
                st.markdown("#### Where you spend")
                _money_table(
                    pd.DataFrame(locations).rename(
                        columns={"location": "Location", "amount": "Spend", "transactions": "Transactions"}
                    ),
                    ["Spend"],
                )
        st.caption("Channel and location details come from connected banks; imported statements don't include them.")


def render_overview_recurring(history) -> None:
    st.caption(
        "Uses your full history for the selected accounts and categories, because recurring "
        "charges and pay schedules only show up across several months."
    )
    recurring = AnalyticsService.recurring_expenses(history)
    active = [item for item in recurring if item["active"]]
    stopped = [item for item in recurring if not item["active"]]
    st.subheader("Recurring charges")
    if active:
        first, second, third = st.columns(3)
        first.metric("Active recurring charges", f"{len(active):,}")
        second.metric("Per month", format_currency(sum(item["monthly_cost"] for item in active)))
        third.metric("Per year", format_currency(sum(item["annual_cost"] for item in active)))
        for item in active:
            if item["price_change"] > 0:
                st.warning(
                    f"{item['merchant']} went up from {format_currency(item['previous_amount'])} to "
                    f"{format_currency(item['amount'])} ({item['cadence'].lower()})."
                )
        _money_table(
            pd.DataFrame(active)[
                ["merchant", "category", "cadence", "amount", "annual_cost", "last_date", "next_date"]
            ].rename(
                columns={
                    "merchant": "Merchant",
                    "category": "Category",
                    "cadence": "Frequency",
                    "amount": "Amount",
                    "annual_cost": "Per year",
                    "last_date": "Last charge",
                    "next_date": "Next expected",
                }
            ),
            ["Amount", "Per year"],
            column_config={
                "Last charge": st.column_config.DateColumn(format="MMM D, YYYY"),
                "Next expected": st.column_config.DateColumn(format="MMM D, YYYY"),
            },
        )
        st.caption("Detected from charges that repeat on a regular schedule at a similar amount.")
    else:
        st.caption("No charges repeat on a regular schedule in this selection yet.")
    if stopped:
        with st.expander(f"Charges that seem to have stopped ({len(stopped)})"):
            for item in stopped:
                st.write(
                    f"{item['merchant']} · {format_currency(item['amount'])} {item['cadence'].lower()} · "
                    f"last charged {item['last_date'].strftime('%b %-d, %Y')}"
                )

    st.subheader("Income")
    income = AnalyticsService.income_summary(history)
    if not income:
        st.caption("No transactions categorized as Salary/Income in this selection.")
        return
    first, second, third, fourth = st.columns(4)
    first.metric("Typical paycheck", format_currency(income["typical_paycheck"]))
    second.metric("Pay frequency", income["cadence"])
    third.metric(
        "Next expected payday",
        income["next_pay_date"].strftime("%b %-d") if income["next_pay_date"] else "Unknown",
    )
    fourth.metric(
        "Stability",
        income["stability"],
        help=(
            f"Monthly income varies by {income['variation']:.0f}% on average "
            "(standard deviation divided by the monthly average)."
        ),
    )
    st.caption(
        f"Average monthly income {format_currency(income['average_monthly'])} across "
        f"{len(income['months'])} month(s) · Sources: "
        + ", ".join(f"{item['source']} ({format_currency(item['amount'])})" for item in income["sources"])
    )
    if len(income["months"]) >= 2:
        st.plotly_chart(make_income_chart(income["months"]), width="stretch")


def render_overview_balances(accounts: list[dict], history: list[dict], all_transactions) -> None:
    st.caption(
        "Balances come from connected banks, refresh on every sync, and ignore the sidebar filters."
    )
    overview = AnalyticsService.balance_overview(accounts)
    counted = AnalyticsService.exclude_transfers(all_transactions)
    income = AnalyticsService.income_summary(counted)
    recurring = AnalyticsService.recurring_expenses(counted)
    today = date.today()
    safe = AnalyticsService.safe_to_spend(
        overview["cash_available"], recurring, income["next_pay_date"] if income else None, today
    )
    first, second, third, fourth = st.columns(4)
    first.metric("Net worth", format_currency(overview["net_worth"]),
                 help="Connected assets minus credit-card and loan balances.")
    second.metric("Cash available", format_currency(overview["cash_available"]),
                  help="Available balance across connected checking and savings accounts.")
    third.metric(
        "Safe to spend",
        format_currency(safe["safe_to_spend"]),
        help=(
            f"Cash available minus {len(safe['charges'])} recurring charge(s) expected through "
            f"{safe['horizon'].strftime('%b %-d')}"
            + (", your next expected payday." if safe["until_payday"] else ".")
        ),
    )
    utilization = overview["credit_utilization"]
    fourth.metric(
        "Credit utilization",
        f"{utilization:.0f}%" if utilization is not None else "—",
        help="Card balances divided by credit limits. Under 30% is generally better for credit scores.",
    )
    pending = sum(abs(item.amount) for item in all_transactions if item.pending and item.amount < 0)
    if pending:
        st.caption(f"{format_currency(pending)} in pending card and bank transactions has not posted yet.")
    if safe["charges"]:
        with st.expander(f"Upcoming recurring charges ({format_currency(safe['committed'])})"):
            for charge in safe["charges"]:
                st.write(f"{charge['date'].strftime('%b %-d')} · {charge['merchant']} · {format_currency(charge['amount'])}")
    if overview["cards"]:
        st.markdown("#### Credit cards")
        _money_table(
            pd.DataFrame(overview["cards"])[["account", "balance", "limit", "utilization", "rating"]].rename(
                columns={
                    "account": "Card",
                    "balance": "Balance",
                    "limit": "Limit",
                    "utilization": "Utilization (%)",
                    "rating": "Rating",
                }
            ),
            ["Balance", "Limit"],
            column_config={"Utilization (%)": st.column_config.NumberColumn(format="%.0f%%")},
        )
    trend = AnalyticsService.net_worth_history(history)
    if len(trend) >= 2:
        st.plotly_chart(make_net_worth_chart(trend), width="stretch")
    else:
        st.caption("A net worth trend appears after balances have been recorded on two different days.")


def render_card_payment_fix(all_transactions) -> None:
    """Offer to recategorize card payments saved before they were treated as transfers."""
    candidates = [
        item
        for item in all_transactions
        if item.category == "Debt Payments" and auto_categorize(item.description) == "Credit Card Payments"
    ]
    if not candidates:
        return
    st.warning(
        f"{len(candidates)} saved transaction(s) categorized as Debt Payments look like credit-card "
        f"payments ({format_currency(sum(abs(item.amount) for item in candidates))}). Moving them to "
        "Credit Card Payments stops them from being counted as spending on top of the card purchases."
    )
    if st.button("Move them to Credit Card Payments", key="fix_card_payments"):
        for item in candidates:
            repo.update_category(item.id, "Credit Card Payments")
        st.rerun()


def render_overview(transactions, view: dict):
    render_feature_intro(
        "Overview",
        "See totals compared with the previous period, quick insights, and tabs for trends, spending habits, recurring charges, income, and balances. Sidebar filters update everything except balances.",
    )
    if not transactions:
        st.info("No transactions match the current filters. Reset them to see your full history.")
        return

    st.markdown(f'<p class="fb-period">{period_label(transactions)}</p>', unsafe_allow_html=True)
    render_card_payment_fix(view["all_transactions"])
    previous_start, previous_end = view["previous_range"]
    render_metrics(
        AnalyticsService.dashboard_metrics(
            transactions, view["previous_transactions"], view["include_transfers"]
        ),
        delta_caption=(
            f"Changes compare with {previous_start.strftime('%b %-d, %Y')}–"
            f"{previous_end.strftime('%b %-d, %Y')}, the matching period just before this range."
        ),
    )
    def counted(items):
        return items if view["include_transfers"] else AnalyticsService.exclude_transfers(items)

    analysis = counted(transactions)
    # Recurring charges, income cadence, and forecasts need more than a short date
    # range, so they use all dates that match the other filters.
    history = counted(view["history_transactions"])
    render_insights(AnalyticsService.insights(analysis, history))

    categories = AnalyticsService.category_summary(analysis)
    balance_accounts, balance_history = load_balances()
    has_balances = any(account.get("current_balance") is not None for account in balance_accounts)
    labels = ["Summary", "Trends", "Habits", "Recurring & income"] + (["Balances"] if has_balances else [])
    tabs = st.tabs(labels)
    with tabs[0]:
        render_overview_summary(analysis, categories, history)
    with tabs[1]:
        render_overview_trends(analysis, view["start_date"], view["end_date"])
    with tabs[2]:
        render_overview_habits(analysis, view["start_date"], view["end_date"])
    with tabs[3]:
        render_overview_recurring(history)
    if has_balances:
        with tabs[4]:
            render_overview_balances(balance_accounts, balance_history, view["all_transactions"])


def render_transactions(transactions):
    render_feature_intro(
        "Transactions",
        "Search and inspect individual activity, download the filtered list, correct automatic categories, create reusable merchant rules, split purchases, and review potential duplicates.",
    )
    st.markdown(f'<p class="fb-period">{period_label(transactions)}</p>', unsafe_allow_html=True)
    if not transactions:
        st.info("No transactions match the current filters.")
        return

    download_col, count_col = st.columns([1, 3])
    with download_col:
        st.download_button(
            "Download filtered CSV",
            transactions_to_csv(transactions),
            file_name="financebuddy-transactions.csv",
            mime="text/csv",
            width="stretch",
            help="Download only the transactions currently included by the sidebar filters.",
        )
    count_col.caption("On phones, transactions are presented as readable cards instead of a wide table.")
    render_transaction_table(transactions)

    st.subheader("Review and correct categories")
    review_candidates = [item for item in transactions if item.category == "Uncategorized"]
    source = review_candidates or transactions
    if not review_candidates:
        st.success("Every visible transaction has a category. You can still revise one below.")
    selected_id = st.selectbox(
        "Transaction",
        options=[item.id for item in source],
        format_func=lambda item_id: next(
            f"{item.date} · {item.description} · {format_currency(item.amount)}"
            for item in source
            if item.id == item_id
        ),
        key="category_transaction",
    )
    selected = next(item for item in source if item.id == selected_id)
    current_index = CATEGORIES.index(selected.category) if selected.category in CATEGORIES else 0
    with st.form("category_correction"):
        new_category = st.selectbox("Category", CATEGORIES, index=current_index)
        rule_keyword = st.text_input(
            "Merchant keyword (optional)",
            help="Save a short phrase such as 'indigo cow' to categorize similar imports automatically.",
        )
        apply_similar = st.checkbox("Apply this keyword to existing transactions")
        submitted = st.form_submit_button(
            "Save category",
            type="primary",
            help="Update this transaction. If you entered a merchant keyword, future imports can use the same category automatically.",
        )
    if submitted:
        repo.update_category(selected_id, new_category)
        updated = 1
        if rule_keyword.strip():
            repo.upsert_category_rule(CategoryRule(keyword=rule_keyword, category=new_category))
            if apply_similar:
                updated = repo.apply_category_rule(rule_keyword, new_category)
        st.success(f"Saved category and updated {updated} transaction(s).")
        st.rerun()

    with st.expander("Split a transaction between two categories"):
        expense_options = [item for item in transactions if item.amount < 0]
        if expense_options:
            split_id = st.selectbox(
                "Expense to split",
                [item.id for item in expense_options],
                format_func=lambda item_id: next(
                    f"{item.date} · {item.description} · {format_currency(abs(item.amount))}"
                    for item in expense_options
                    if item.id == item_id
                ),
                key="split_transaction",
            )
            split_item = next(item for item in expense_options if item.id == split_id)
            with st.form("split_form"):
                category_one = st.selectbox("First category", CATEGORIES, key="split_cat_1")
                amount_one = st.number_input(
                    "First amount",
                    min_value=0.01,
                    max_value=max(float(abs(split_item.amount) - 0.01), 0.01),
                    value=float(round(abs(split_item.amount) / 2, 2)),
                    step=0.01,
                )
                category_two = st.selectbox("Second category", CATEGORIES, key="split_cat_2")
                amount_two = round(abs(split_item.amount) - amount_one, 2)
                st.caption(f"Second amount: {format_currency(amount_two)}")
                split_submitted = st.form_submit_button(
                    "Split transaction",
                    help="Replace the original transaction with two categorized entries whose amounts add up to the original.",
                )
            if split_submitted:
                try:
                    repo.split_transaction(
                        split_id, category_one, amount_one, category_two, amount_two
                    )
                    st.success("Transaction split successfully.")
                    st.rerun()
                except SupabaseError:
                    st.error("The split could not be saved. The original transaction is unchanged; refresh and try again.")
        else:
            st.caption("No expenses are available to split.")

    duplicates = AnalyticsService.duplicate_candidates(transactions)
    with st.expander(f"Potential duplicates ({len(duplicates)})"):
        if not duplicates:
            st.caption("No exact duplicate date, amount, merchant, and account combinations found.")
        for group in duplicates:
            st.write(
                f"{group[0].date} · {group[0].description} · "
                f"{format_currency(group[0].amount)} · {len(group)} copies"
            )


def render_budgets(transactions) -> None:
    st.subheader("Monthly budgets")
    st.caption("Includes all accounts and categories for the selected month, regardless of dashboard filters.")
    budgets = repo.get_budgets()
    months = sorted({item.date.strftime("%Y-%m") for item in transactions})
    selected_month = st.selectbox("Budget month", months, index=len(months) - 1) if months else None
    if budgets and selected_month:
        statuses = AnalyticsService.budget_status(budgets, transactions, selected_month, date.today())
        total_limit = sum(row["limit"] for row in statuses)
        total_spent = sum(row["spent"] for row in statuses)
        first, second, third = st.columns(3)
        first.metric("Budgeted", format_currency(total_limit))
        second.metric("Spent in budgeted categories", format_currency(total_spent))
        third.metric(
            "Left to spend",
            format_currency(total_limit - total_spent),
            delta="Over budget" if total_spent > total_limit else None,
            delta_color="inverse",
        )
        for row in statuses:
            render_progress(row["category"], row["spent"], row["limit"])
            if 0 < row["days_elapsed"] < row["days_in_month"]:
                meta = (
                    f"Day {row['days_elapsed']} of {row['days_in_month']} · "
                    f"{row['elapsed_share']:.0f}% of the month gone · {row['spent_share']:.0f}% of budget used"
                )
                if row["daily_allowance"]:
                    meta += f" · about {format_currency(row['daily_allowance'])}/day left"
            elif row["days_elapsed"] == 0:
                meta = "This month hasn't started yet."
            else:
                meta = f"Month complete · {row['spent_share']:.0f}% of budget used"
            st.markdown(f'<p class="fb-budget-meta">{meta}</p>', unsafe_allow_html=True)
            if row["status"] == "Over budget":
                st.error(f"Over budget by {format_currency(row['spent'] - row['limit'])}")
            elif row["status"] == "Projected to go over":
                st.warning(
                    f"At this pace, {row['category']} reaches about {format_currency(row['projected'])} "
                    f"by month end, {format_currency(row['projected'] - row['limit'])} over the limit."
                )
    elif not budgets:
        st.info("Create a category budget to start tracking monthly limits.")

    if budgets and selected_month and len(months) >= 2:
        history_months = [month for month in months if month <= selected_month][-6:]
        history = AnalyticsService.budget_history(budgets, transactions, history_months)
        with st.expander("Budget history", expanded=False):
            st.caption(f"How each budget held up over the last {len(history_months)} month(s) with activity.")
            st.dataframe(
                pd.DataFrame(history["summary"]).rename(
                    columns={
                        "category": "Budget",
                        "limit": "Limit",
                        "months_on_budget": "Months on budget",
                        "months": "Months",
                        "average": "Average spend",
                        "highest": "Highest month spend",
                        "highest_month": "Highest month",
                    }
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Limit": st.column_config.NumberColumn(format="$%.2f"),
                    "Average spend": st.column_config.NumberColumn(format="$%.2f"),
                    "Highest month spend": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
            st.plotly_chart(make_budget_history_chart(history["cells"]), width="stretch")

    with st.form("budget_form"):
        budget_category = st.selectbox("Category", CATEGORIES, key="budget_category")
        budget_limit = st.number_input("Monthly limit", min_value=1.0, value=500.0, step=25.0)
        budget_submit = st.form_submit_button(
            "Save budget",
            type="primary",
            help="Create a new monthly category limit or update the existing limit for that category.",
        )
    if budget_submit:
        repo.upsert_budget(Budget(category=budget_category, monthly_limit=budget_limit))
        st.success("Budget saved.")
        st.rerun()

    if budgets:
        with st.expander("Remove a budget"):
            remove_budget = st.selectbox("Budget", [item.category for item in budgets])
            confirm_budget = st.checkbox("I understand this removes the selected budget")
            if st.button(
                "Remove budget",
                disabled=not confirm_budget,
                help="Delete this spending limit. Transactions and categories are not affected.",
            ):
                repo.delete_budget(remove_budget)
                st.rerun()


def render_goals(transactions) -> None:
    st.subheader("Savings goals")
    goals = repo.get_goals()
    today = date.today()
    average_savings = AnalyticsService.average_monthly_savings(transactions)
    required_total = 0.0
    for goal in goals:
        label = goal.name
        if goal.target_date:
            label += f" · target {goal.target_date.strftime('%b %-d, %Y')}"
        render_progress(label, goal.current_amount, goal.target_amount)
        plan = AnalyticsService.goal_plan(goal, today, average_savings)
        notes = []
        if plan["complete"]:
            notes.append("Goal reached 🎉")
        else:
            notes.append(f"{format_currency(plan['remaining'])} to go")
            if plan["required_monthly"] is not None:
                required_total += plan["required_monthly"]
                if plan["months_left"] and plan["months_left"] >= 1:
                    notes.append(f"save {format_currency(plan['required_monthly'])}/month to hit the date")
                else:
                    notes.append("the target date is less than a month away")
                if plan["on_track"] is True:
                    notes.append("your recent savings cover it")
                elif plan["on_track"] is False:
                    notes.append("more than your recent average net savings")
            if plan["projected_date"]:
                notes.append(f"projected to finish around {plan['projected_date'].strftime('%b %Y')}")
        st.markdown(f'<p class="fb-budget-meta">{" · ".join(notes)}</p>', unsafe_allow_html=True)
    if not goals:
        st.info("Create a goal for an emergency fund, trip, or major purchase.")
    elif average_savings is not None:
        summary = (
            f"Your recent average net savings is {format_currency(average_savings)}/month "
            "(income minus spending over the last complete months, ignoring transfers)."
        )
        if required_total:
            summary += f" Goals with target dates need {format_currency(required_total)}/month combined."
        st.caption(summary)
    else:
        st.caption("Import at least one complete month of activity to compare goals with your savings pace.")

    with st.form("goal_form"):
        goal_name = st.text_input("Goal name")
        goal_target = st.number_input("Target amount", min_value=1.0, value=1000.0, step=100.0)
        goal_current = st.number_input("Already saved", min_value=0.0, value=0.0, step=50.0)
        has_date = st.checkbox("Set a target date")
        goal_date = st.date_input("Target date", value=date.today(), disabled=not has_date)
        goal_submit = st.form_submit_button(
            "Create goal",
            type="primary",
            help="Save a target amount, current progress, and optional target date.",
        )
    if goal_submit:
        if not goal_name.strip():
            st.error("Enter a name for the goal.")
        else:
            repo.save_goal(
                SavingsGoal(
                    name=goal_name.strip(),
                    target_amount=goal_target,
                    current_amount=goal_current,
                    target_date=goal_date if has_date else None,
                )
            )
            st.success("Goal created.")
            st.rerun()

    if goals:
        with st.expander("Update or remove a goal"):
            goal_id = st.selectbox(
                "Goal", [item.id for item in goals], format_func=lambda item_id: next(item.name for item in goals if item.id == item_id)
            )
            goal = next(item for item in goals if item.id == goal_id)
            new_current = st.number_input(
                "Current saved amount",
                min_value=0.0,
                value=float(goal.current_amount),
                step=50.0,
            )
            if st.button(
                "Update progress",
                help="Save the latest amount accumulated toward this goal.",
            ):
                repo.save_goal(goal.model_copy(update={"current_amount": new_current}))
                st.rerun()
            confirm_goal = st.checkbox("I understand this removes the selected goal")
            if st.button(
                "Remove goal",
                disabled=not confirm_goal,
                help="Permanently remove this goal. Transaction history is not affected.",
            ):
                repo.delete_goal(goal_id)
                st.rerun()


def render_plan(transactions) -> None:
    render_feature_intro(
        "Plan",
        "Set monthly spending limits, see whether you're on pace, review budget history, and learn what each savings goal needs per month.",
    )
    view = st.radio("Plan view", ["Budgets", "Savings goals"], horizontal=True, key="plan_view")
    if view == "Budgets":
        render_budgets(transactions)
    else:
        render_goals(transactions)


def render_compare():
    render_feature_intro(
        "Compare statements",
        "Upload two to six statements for a temporary side-by-side analysis. Files are sorted by detected transaction dates and are never added to saved history.",
    )
    st.caption(
        "Upload up to six statements. FinanceBuddy detects each date range and sorts periods automatically. "
        "Comparison files never change saved history."
    )
    files = st.file_uploader(
        "Statements to compare",
        type=["csv", "txt", "pdf"],
        accept_multiple_files=True,
        key="comparison_files",
        help="Select statements from any supported account. You can assign an account type and name to each file after upload.",
    )
    if not files:
        st.info("Choose two or more statements to compare spending and category changes.")
        return
    if len(files) > 6:
        st.warning("Only the first six statements are included.")
    periods = []
    errors = []
    for index, upload in enumerate(files[:6]):
        left, right = st.columns(2)
        account_type = left.selectbox(
            f"Type · {upload.name}",
            ["Checking", "Credit Card"],
            key=f"compare_type_{index}_{upload.name}",
        )
        account_name = right.text_input(
            f"Account · {upload.name}",
            value=upload.name.rsplit(".", 1)[0],
            key=f"compare_account_{index}_{upload.name}",
        )
        parsed, metrics = parse_statement(upload, account_name, account_type)
        if not metrics.is_valid:
            errors.append(f"{upload.name}: {' '.join(metrics.errors[:2])}")
            continue
        start = min(item.date for item in parsed)
        end = max(item.date for item in parsed)
        periods.append(
            {
                "label": f"{start.strftime('%b %-d')}–{end.strftime('%b %-d, %Y')}",
                "transactions": parsed,
                "start": start,
            }
        )
    if errors:
        for error in errors:
            st.error(error)
    if len(periods) < 2:
        st.info("At least two readable statements are required for comparison.")
        return
    periods.sort(key=lambda item: item["start"])
    comparisons = AnalyticsService.compare_periods(periods)
    st.caption("Transfers and credit-card payments are left out of money in and out so purchases aren't counted twice.")
    rows = [
        {
            "Period": item["label"],
            "Money in": item["summary"]["inflow"],
            "Money out": item["summary"]["outflow"],
            "Net cash flow": item["summary"]["net_savings"],
            "Status": item["status"],
            "Spending change": item["outflow_delta"],
        }
        for item in comparisons
    ]
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={
            "Money in": st.column_config.NumberColumn(format="$%.2f"),
            "Money out": st.column_config.NumberColumn(format="$%.2f"),
            "Net cash flow": st.column_config.NumberColumn(format="$%.2f"),
            "Spending change": st.column_config.NumberColumn(format="$%.2f"),
        },
    )
    left, right = st.columns(2)
    with left:
        st.plotly_chart(make_comparison_summary_chart(comparisons), width="stretch")
    with right:
        st.plotly_chart(make_comparison_category_chart(comparisons), width="stretch")
    st.plotly_chart(make_comparison_delta_chart(comparisons), width="stretch")


def render_bank_connections() -> None:
    st.subheader("Connected banks")
    st.caption(
        "Connect through Plaid to import bank activity automatically. FinanceBuddy never receives "
        "your bank username or password, and stored Plaid access tokens are encrypted."
    )
    config = PlaidConfig.from_sources(_streamlit_secrets())
    if not config.is_configured:
        st.info(
            "Plaid is ready to use after its credentials are configured. Add `PLAID_CLIENT_ID`, "
            "`PLAID_SECRET`, and `PLAID_ENV` to a local `.env` file or your deployment secrets."
        )
        with st.expander("Configuration example"):
            st.code(
                "PLAID_CLIENT_ID=your_client_id\n"
                "PLAID_SECRET=your_production_secret\n"
                "PLAID_ENV=production\n"
                "PLAID_COUNTRY_CODES=US",
                language="bash",
            )
        return

    try:
        service = PlaidService(config, plaid_repo, repo)
    except (PlaidConfigurationError, ImportError) as error:
        st.error(f"Plaid setup is incomplete: {error}")
        return

    client_user_id = repo.get_setting("plaid_client_user_id")
    if not client_user_id:
        client_user_id = str(uuid.uuid4())
        repo.set_setting("plaid_client_user_id", client_user_id)

    items = plaid_repo.get_items()
    if items:
        for item in items:
            with st.container(border=True):
                heading, status = st.columns([3, 1])
                heading.markdown(f"**{item.institution_name}**")
                account_count = len(plaid_repo.get_accounts(item.item_id))
                last_sync = item.last_synced_at.replace("T", " ").replace("+00:00", " UTC") if item.last_synced_at else "Not synced yet"
                heading.caption(f"{account_count} account(s) · Last sync: {last_sync}")
                if item.status == "error":
                    status.error("Needs attention")
                    st.warning("Reconnect this institution to continue syncing.")
                else:
                    status.success("Connected")

                sync_col, repair_col = st.columns(2)
                if sync_col.button(
                    "Sync transactions",
                    key=f"sync_{item.item_id}",
                    type="primary",
                    width="stretch",
                    help="Fetch transactions added, changed, or removed since the last successful sync.",
                ):
                    try:
                        result = service.sync_item(item.item_id)
                        st.session_state.plaid_notice = (
                            f"Sync complete: {result['added']} added, {result['modified']} updated, "
                            f"and {result['removed']} removed."
                            + (
                                " Balances updated."
                                if result.get("balances_updated")
                                else " Balances could not be refreshed this time."
                            )
                        )
                        st.rerun()
                    except Exception as error:
                        st.error(f"Plaid sync failed: {service._friendly_error(error)}")

                if repair_col.button(
                    "Update connection",
                    key=f"repair_{item.item_id}",
                    width="stretch",
                    help="Reopen Plaid to repair expired credentials or institution access.",
                ):
                    try:
                        st.session_state.plaid_update_item = item.item_id
                        st.session_state.plaid_link_token = service.update_link_token(
                            item.item_id, client_user_id
                        )
                        st.rerun()
                    except Exception as error:
                        st.error(f"Could not start update mode: {service._friendly_error(error)}")

                with st.expander("Disconnect institution"):
                    remove_transactions = st.checkbox(
                        "Also remove imported transactions from these connected accounts",
                        key=f"remove_tx_{item.item_id}",
                    )
                    confirmed = st.checkbox(
                        "I understand this revokes FinanceBuddy's Plaid access",
                        key=f"disconnect_confirm_{item.item_id}",
                    )
                    if st.button(
                        "Disconnect",
                        key=f"disconnect_{item.item_id}",
                        disabled=not confirmed,
                        help="Revoke the Plaid Item and remove its saved connection metadata.",
                    ):
                        try:
                            deleted = service.remove_item(item.item_id, remove_transactions)
                            st.session_state.plaid_notice = (
                                f"Disconnected {item.institution_name}. Removed {deleted} transaction(s)."
                            )
                            st.rerun()
                        except Exception as error:
                            st.error(f"Could not disconnect: {service._friendly_error(error)}")
    else:
        st.info("No bank is connected yet. You can keep using statement imports alongside Plaid.")

    if st.session_state.get("plaid_notice"):
        st.success(st.session_state.pop("plaid_notice"))

    st.markdown("#### Add another institution")
    if "plaid_link_token" not in st.session_state:
        if st.button(
            "Prepare secure connection",
            type="primary",
            help="Create a short-lived Plaid Link session before choosing your institution.",
        ):
            try:
                st.session_state.plaid_update_item = None
                st.session_state.plaid_link_token = service.create_link_token(client_user_id)
                st.rerun()
            except Exception as error:
                st.error(f"Could not start Plaid Link: {service._friendly_error(error)}")
    else:
        updating = st.session_state.get("plaid_update_item")
        result = render_plaid_link(
            st.session_state.plaid_link_token,
            button_label="Update bank connection" if updating else "Choose a bank",
            key=f"plaid_link_{updating or 'new'}",
        )
        success = getattr(result, "success", None)
        link_error = getattr(result, "error", None)
        if success:
            try:
                if updating:
                    st.session_state.plaid_notice = "Bank connection updated. You can sync again now."
                else:
                    item = service.exchange_public_token(
                        success["public_token"], success.get("metadata")
                    )
                    sync_result = service.sync_item(item.item_id)
                    st.session_state.plaid_notice = (
                        f"Connected {item.institution_name}. Initial sync added "
                        f"{sync_result['added']} transaction(s)."
                    )
                st.session_state.pop("plaid_link_token", None)
                st.session_state.pop("plaid_update_item", None)
                st.rerun()
            except Exception as error:
                st.session_state.pop("plaid_link_token", None)
                st.error(f"Could not finish the Plaid connection: {service._friendly_error(error)}")
        elif link_error:
            st.error(link_error.get("message", "Plaid Link could not be completed."))
            if st.button("Start a fresh connection"):
                st.session_state.pop("plaid_link_token", None)
                st.session_state.pop("plaid_update_item", None)
                st.rerun()


def render_import_statement() -> None:
    st.subheader("Import a statement")
    st.caption("Files are uploaded to the FinanceBuddy server for processing. Reviewed transactions are saved to your account only when you confirm. Maximum file size: 10 MB.")
    receipt = st.session_state.get("import_receipt")
    if receipt:
        st.success(f"Saved {receipt['count']} transaction(s) to {receipt['account']}.")
        st.button("Review categories", on_click=navigate_to, args=("Transactions",))
    if "import_account_type" not in st.session_state:
        st.session_state.import_account_type = "Checking"
    if "import_account_name" not in st.session_state:
        st.session_state.import_account_name = "Primary Checking"
    st.markdown("#### 1. Choose an account")
    account_type = st.selectbox(
        "Statement type",
        ["Checking", "Credit Card"],
        key="import_account_type",
        on_change=sync_import_account_name,
        help="Checking and credit-card statements use different sign conventions and dashboard metrics.",
    )
    account_name = st.text_input("Account name", key="import_account_name")
    st.markdown("#### 2. Add a statement")
    upload = st.file_uploader(
        "CSV, TXT, or text-based PDF",
        type=["csv", "txt", "pdf"],
        key="statement_import",
        help="CSV and TXT files are read as tables. PDFs must contain selectable text; scanned images require OCR and are not supported yet.",
    )
    if upload:
        parsed, metrics = parse_statement(upload, account_name.strip(), account_type)
        if not metrics.is_valid:
            st.error("This statement could not be imported safely.")
            for error in metrics.errors:
                st.write(f"• {error}")
            st.info("Check the account type and file format, then choose a corrected file. Nothing was saved.")
        else:
            st.markdown("#### 3. Review the preview")
            duplicates = repo.count_existing_ids(parsed)
            start = min(item.date for item in parsed)
            end = max(item.date for item in parsed)
            st.success(
                f"Parsed {metrics.valid_rows} of {metrics.total_rows} rows "
                f"({metrics.accuracy_rate * 100:.1f}% row recognition)."
            )
            st.caption(
                f"Detected period: {start.strftime('%b %-d, %Y')}–{end.strftime('%b %-d, %Y')} · "
                f"{duplicates} already saved · {metrics.skipped_rows} skipped"
            )
            st.dataframe(
                transaction_frame(parsed[:20]),
                width="stretch",
                hide_index=True,
                column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
            )
            if len(parsed) > 20:
                st.caption(f"Previewing 20 of {len(parsed)} parsed rows.")
            if metrics.skipped_rows:
                with st.expander(f"Why {metrics.skipped_rows} row(s) were skipped"):
                    for error in metrics.errors:
                        st.write(f"• {error}")
                    if metrics.skipped_rows > len(metrics.errors):
                        st.caption("Only the first few row issues are shown. Export a cleaner CSV if important rows are missing.")
            st.caption("Duplicates are matched by the statement's transaction identifiers. Append keeps existing matches and saves only new rows.")
            st.markdown("#### 4. Choose how to save")
            mode = st.radio(
                "Save behavior",
                ["Append new transactions", "Replace this account"],
                help="Append ignores exact duplicates. Replace only clears history for the named account.",
            )
            confirmation = st.checkbox(
                "I reviewed the account, statement type, period, and preview"
            )
            if mode == "Replace this account":
                existing_count = sum(item.account_name == account_name.strip() for item in repo.get_all())
                st.warning(f"This replaces {existing_count} saved transaction(s) for “{account_name}” with {len(parsed)} parsed row(s). Replacement cannot be undone.")
                st.download_button(
                    "Download backup before replacing",
                    repo.export_backup(),
                    file_name="financebuddy-before-replacement.json",
                    mime="application/json",
                )
            if st.button(
                "Save statement",
                type="primary",
                disabled=not confirmation or not account_name.strip(),
                help="Append only new transactions, or replace history for the named account when that mode is selected.",
            ):
                try:
                    st.session_state.pop("undo_import_rows", None)
                    if mode == "Append new transactions":
                        before_ids = repo.get_existing_ids()
                        inserted = repo.insert_many(parsed)
                        new_ids = {item.id for item in parsed} - before_ids
                        if new_ids:
                            st.session_state.undo_import_rows = list({
                                item.id: item for item in parsed if item.id in new_ids
                            }.values())
                    else:
                        inserted = repo.replace_account(account_name.strip(), parsed)
                    st.session_state.import_receipt = {
                        "count": inserted, "account": account_name.strip(), "mode": mode,
                    }
                    st.rerun()
                except SupabaseError:
                    st.error("The statement could not be saved. Your existing history is unchanged; please retry.")
    if st.session_state.get("undo_import_rows"):
        if st.button(
            "Undo last import",
            help="Remove only the new rows from the latest appended statement, if none have changed since import.",
        ):
            try:
                removed = repo.undo_append(st.session_state.undo_import_rows)
                st.session_state.pop("undo_import_rows", None)
                st.success(f"Removed {removed} imported transaction(s).")
                st.rerun()
            except SupabaseError:
                st.error("Some imported transactions changed. Nothing was removed; review your history before retrying.")


def render_backup_restore(all_transactions) -> None:
    st.subheader("Download or restore your data")
    st.caption("Downloaded backups are unencrypted and contain financial records. Store them privately. Bank connections and settings are not included.")
    if st.session_state.get("restore_receipt"):
        st.success(st.session_state.restore_receipt)
    st.download_button(
        "Download full JSON backup",
        repo.export_backup(),
        file_name="financebuddy-backup.json",
        mime="application/json",
        help="Download transactions, budgets, goals, and category rules in one portable FinanceBuddy backup.",
    )
    restore = st.file_uploader("Restore a FinanceBuddy JSON backup", type=["json"])
    if restore:
        try:
            preview = read_backup(restore.getvalue())
            st.warning("Restoring replaces the four collections below for your account. Bank connections and settings stay in place.")
            st.dataframe(
                pd.DataFrame([
                    {"Collection": "Transactions", "Saved now": len(all_transactions), "In backup": len(preview["transactions"])},
                    {"Collection": "Budgets", "Saved now": len(repo.get_budgets()), "In backup": len(preview["budgets"])},
                    {"Collection": "Savings goals", "Saved now": len(repo.get_goals()), "In backup": len(preview["goals"])},
                    {"Collection": "Category rules", "Saved now": len(repo.get_category_rules()), "In backup": len(preview["category_rules"])},
                ]),
                hide_index=True,
                width="stretch",
            )
            confirm_restore = st.checkbox("I reviewed the counts and want to replace these four collections")
            if st.button(
                "Restore backup",
                disabled=not confirm_restore,
                help="Replace transactions, budgets, savings goals, and category rules. Bank connections and settings are kept.",
            ):
                result = repo.restore_backup(restore.getvalue())
                st.session_state.restore_receipt = (
                    f"Restore complete: {result['transactions']} transactions, "
                    f"{result['budgets']} budgets, {result['goals']} goals, and "
                    f"{result['category_rules']} category rules."
                )
                st.rerun()
        except (ValueError, SupabaseError):
            st.error("The backup could not be restored. Check its format and review your saved data before retrying.")


def render_saved_accounts(all_transactions) -> None:
    st.subheader("Saved accounts")
    accounts = sorted({item.account_name for item in all_transactions})
    if accounts:
        account_rows = []
        for account in accounts:
            items = [item for item in all_transactions if item.account_name == account]
            account_rows.append(
                {
                    "Account": account,
                    "Type": ", ".join(sorted({item.account_type for item in items})),
                    "Transactions": len(items),
                    "First date": min(item.date for item in items),
                    "Latest date": max(item.date for item in items),
                }
            )
        st.dataframe(pd.DataFrame(account_rows), width="stretch", hide_index=True)
        remove_account = st.selectbox("Account to remove", accounts)
        typed_name = st.text_input(
            f"Type {remove_account} to confirm permanent removal",
            key="delete_account_confirmation",
        )
        if st.button(
            "Remove account",
            disabled=typed_name != remove_account,
            help="Permanently delete every saved transaction for this account. Other accounts are not affected.",
        ):
            deleted = repo.delete_account(remove_account)
            st.success(f"Removed {deleted} transaction(s) from {remove_account}.")
            st.rerun()
    else:
        st.info("No saved accounts yet.")


def render_category_rules() -> None:
    st.subheader("Custom merchant rules")
    rules = repo.get_category_rules()
    if rules:
        st.dataframe(
            pd.DataFrame([rule.model_dump() for rule in rules]).rename(
                columns={"keyword": "Keyword", "category": "Category"}
            ),
            width="stretch",
            hide_index=True,
        )
        remove_rule = st.selectbox("Rule to remove", [rule.keyword for rule in rules])
        if st.button(
            "Remove rule",
            help="Stop automatically applying this keyword on future imports. Existing categories remain unchanged.",
        ):
            repo.delete_category_rule(remove_rule)
            st.rerun()
    else:
        st.info("Rules saved from category corrections will appear here.")


def render_accounts(all_transactions) -> None:
    render_feature_intro("Accounts", "Connect a bank, import a statement, or review saved accounts.")
    view = st.radio("Accounts view", ["Bank connections", "Import statement", "Saved accounts"],
                    horizontal=True, key="accounts_view")
    if view == "Bank connections":
        render_bank_connections()
    elif view == "Import statement":
        render_import_statement()
    else:
        render_saved_accounts(all_transactions)


def render_settings(all_transactions) -> None:
    render_feature_intro("Settings", "Manage backups, restore data, and review category rules.")
    view = st.radio("Settings view", ["Backup & restore", "Category rules"],
                    horizontal=True, key="settings_view")
    if view == "Backup & restore":
        render_backup_restore(all_transactions)
    else:
        render_category_rules()


title_column, account_column, guide_column = st.columns([4, 1, 1])
title_column.title("💸 FinanceBuddy")
title_column.caption("A private, account-isolated view of your money")
user_metadata = user.get("user_metadata") or {}
account_column.caption(
    str(
        user_metadata.get("full_name")
        or user_metadata.get("name")
        or user.get("email")
        or "Your account"
    )
)
if account_column.button(
    "Log out", help="End this browser's FinanceBuddy login session.", width="stretch"
):
    try:
        auth.sign_out(session["access_token"])
    except SupabaseError:
        pass
    _clear_supabase_session()
    st.rerun()
if guide_column.button(
    "How it works",
    help="Open the getting-started guide and a short explanation of the main workflow.",
    width="stretch",
):
    st.session_state.show_onboarding = True

show_onboarding = (
    repo.get_setting("onboarding_complete", "false") != "true"
    or st.session_state.get("show_onboarding", False)
)
if show_onboarding:
    render_onboarding()

page = st.selectbox(
    "Go to",
    ["Overview", "Transactions", "Plan", "Accounts", "Compare", "Settings"],
    key="nav_page",
    help="Choose the part of FinanceBuddy you want to use. Your place is kept while saving.",
)

all_transactions = repo.get_all()
if all_transactions and page in ("Overview", "Transactions"):
    with st.sidebar:
        st.header("Dashboard filters")
        st.caption("These controls update Overview and Transactions. Monthly budgets always include all saved spending for the selected month.")
        min_date = min(item.date for item in all_transactions)
        max_date = max(item.date for item in all_transactions)
        account_options = sorted({item.account_name for item in all_transactions})
        category_options = sorted({item.category for item in all_transactions})
        clear_column, all_column = st.columns(2)
        clear_column.button(
            "Clear accounts",
            on_click=clear_account_filter,
            width="stretch",
            help="Temporarily hide every account from Overview and Transactions.",
        )
        all_column.button(
            "Select all",
            on_click=select_all_accounts,
            args=(account_options,),
            width="stretch",
            help="Include every account in Overview and Transactions.",
        )
        st.button(
            "Reset overview & transactions",
            on_click=reset_dashboard_filters,
            width="stretch",
            help="Restore the full date range, all accounts, all categories, and clear search and amount filters.",
        )
        st.selectbox(
            "Quick range",
            DATE_PRESETS,
            key="dashboard_date_preset",
            on_change=apply_date_preset,
            args=(min_date, max_date),
            help="Ranges count back from your most recent transaction.",
        )
        stored_dates = st.session_state.get("dashboard_dates")
        # Keep a half-finished range selection, but repair dates outside the saved history.
        if not stored_dates or any(not min_date <= value <= max_date for value in stored_dates):
            st.session_state.dashboard_dates = (min_date, max_date)
        selected_dates = st.date_input(
            "Date range",
            min_value=min_date,
            max_value=max_date,
            key="dashboard_dates",
            on_change=mark_custom_dates,
        )
        if isinstance(selected_dates, tuple) and len(selected_dates) == 2:
            start_date, end_date = selected_dates
        else:
            start_date, end_date = min_date, max_date
        selected_accounts = st.multiselect(
            "Accounts", account_options, default=account_options, key="dashboard_accounts"
        )
        selected_categories = st.multiselect(
            "Categories", category_options, default=category_options, key="dashboard_categories"
        )
        selected_types = st.multiselect(
            "Transaction type",
            ["Income / credit", "Expense / purchase"],
            default=["Income / credit", "Expense / purchase"],
            key="dashboard_transaction_types",
        )
        search = st.text_input(
            "Merchant search", placeholder="e.g. Costco", key="dashboard_search"
        )
        minimum_amount = st.number_input(
            "Minimum absolute amount",
            min_value=0.0,
            value=0.0,
            step=10.0,
            key="dashboard_minimum_amount",
        )
        include_transfers = st.toggle(
            "Count transfers & card payments",
            key="dashboard_include_transfers",
            help=(
                "Off by default: money moved between your own accounts, including credit-card "
                "payments, is left out of income and spending so purchases aren't counted twice."
            ),
        )
    filter_arguments = (
        selected_accounts,
        selected_categories,
        selected_types,
        search,
        minimum_amount,
    )
    filtered_transactions = AnalyticsService.filter_transactions(
        all_transactions, start_date, end_date, *filter_arguments
    )
    previous_range = AnalyticsService.previous_period(start_date, end_date)
    overview_view = {
        "start_date": start_date,
        "end_date": end_date,
        "previous_range": previous_range,
        "previous_transactions": AnalyticsService.filter_transactions(
            all_transactions, *previous_range, *filter_arguments
        ),
        "include_transfers": include_transfers,
        "all_transactions": all_transactions,
        "history_transactions": AnalyticsService.filter_transactions(
            all_transactions, min_date, max_date, *filter_arguments
        ),
    }
else:
    filtered_transactions = all_transactions

if page in ("Overview", "Transactions") and all_transactions:
    active_count = len(filtered_transactions)
    st.caption(f"Showing {active_count:,} of {len(all_transactions):,} saved transactions")
    active_filters = []
    if (start_date, end_date) != (min_date, max_date):
        active_filters.append("date range")
    if set(selected_accounts) != set(account_options):
        active_filters.append("accounts")
    if set(selected_categories) != set(category_options):
        active_filters.append("categories")
    if len(selected_types) != 2:
        active_filters.append("transaction type")
    if search.strip():
        active_filters.append(f"merchant: {search.strip()}")
    if minimum_amount:
        active_filters.append(f"minimum: {format_currency(minimum_amount)}")
    if active_filters:
        st.info("Active filters: " + " · ".join(active_filters))
        st.button("Reset filters and show all", on_click=reset_dashboard_filters)

if page == "Overview":
    if not all_transactions:
        render_start_actions()
    else:
        render_overview(filtered_transactions, overview_view)
elif page == "Transactions":
    if not all_transactions:
        render_start_actions()
    else:
        render_transactions(filtered_transactions)
elif page == "Plan":
    render_plan(all_transactions)
elif page == "Compare":
    render_compare()
elif page == "Accounts":
    render_accounts(all_transactions)
else:
    render_settings(all_transactions)
