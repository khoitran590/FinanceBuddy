from __future__ import annotations

import hashlib
import html
import json
import time
import uuid
from datetime import date, timedelta
from decimal import Decimal
from src.domain.money import money
from src.services.data_service import DataService

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.domain.models import Budget, CategoryRule, SavingsGoal, Transaction
from src.repositories.supabase_repo import (
    SupabasePlaidRepository,
    SupabaseTransactionRepository,
)
from src.services.analytics import AnalyticsService
from src.services.categorizer import CATEGORIES, TRANSFER_CATEGORIES, auto_categorize, categorize_with_custom_rules
from src.services.merchants import is_processor_phrase, keyword_warning, rule_keyword, uncategorized_groups
from src.services.parser import BankStatementParser
from src.services.sign_detection import sign_evidence
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
    make_cash_flow_timeline,
    make_category_bar_chart,
    make_category_donut_chart,
    make_category_heatmap,
    make_category_treemap,
    make_category_trend_lines,
    make_comparison_category_chart,
    make_comparison_delta_chart,
    make_comparison_summary_chart,
    make_daily_spending_chart,
    make_fixed_flexible_chart,
    make_income_chart,
    make_merchant_bar_chart,
    make_monthly_bar_chart,
    make_monthly_line_chart,
    make_monthly_net_chart,
    make_net_worth_chart,
    make_profit_loss_line_chart,
    make_savings_rate_chart,
    make_week_of_month_chart,
    make_weekday_chart,
)
from src.ui.components import (
    active_dialog,
    close_dialog,
    format_currency,
    inject_app_styles,
    open_dialog,
    period_label,
    render_card_header,
    render_insights,
    render_metrics,
    render_page_header,
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
        st.header("Your finances stay private to your account")
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
            st.caption("Use at least 12 characters. Verify your email before accessing your financial records.")
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
                    st.success("Check your inbox for your password recovery code.")
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
data_client.pending_operation_ids = st.session_state.setdefault("pending_operation_ids", {})
repo = SupabaseTransactionRepository(data_client, user_id)
data_service = DataService(repo)
plaid_repo = SupabasePlaidRepository(data_client, user_id)


def apply_custom_rules(transactions):
    return data_service.categorize(transactions)


def parse_statement(upload, account_name: str, account_type: str, **options):
    raw = upload.getvalue()
    cache_key = (hashlib.sha256(raw).hexdigest(), upload.name.lower().endswith(".pdf"),
                 account_name, account_type, tuple(sorted(options.items())))
    cache = st.session_state.setdefault("statement_parse_cache", {})
    if cache_key not in cache:
        if len(cache) >= 6:
            cache.clear()
        cache[cache_key] = BankStatementParser.parse_file(
            raw, upload.name, account_name, account_type, **options
        )
    parsed, metrics = cache[cache_key]
    return apply_custom_rules([item.model_copy(deep=True) for item in parsed]), metrics


def statement_format_options(account_type: str, key: str) -> dict:
    convention = st.selectbox(
        "Amount column sign convention", [None, "outflow_negative", "outflow_positive"],
        index=0 if account_type == "Credit Card" else 1,
        format_func=lambda value: {None: "Choose for a single Amount column",
                                  "outflow_negative": "Money out is negative",
                                  "outflow_positive": "Money out is positive"}[value],
        key=f"{key}_sign_{account_type}",
        help="Debit/Credit columns always determine direction. For a single Amount column, use the sign convention shown in your bank's file.",
    )
    with st.expander("Statement date, number format, and total"):
        date_order = st.selectbox(
            "Date format", ["month_first", "day_first"],
            format_func=lambda value: "Month/day/year" if value == "month_first" else "Day/month/year",
            key=f"{key}_date_order", help="ISO dates such as 2026-10-13 work with either choice.",
        )
        number_format = st.selectbox(
            "Number format", ["decimal_dot", "decimal_comma"],
            format_func=lambda value: "1,234.56" if value == "decimal_dot" else "1.234,56",
            key=f"{key}_number_format",
        )
        use_references = st.checkbox(
            "Use bank transaction references", value=True, key=f"{key}_use_references",
            help="Turn this off if the Reference column contains shared labels rather than unique bank transaction IDs.",
        )
        override_year = st.checkbox("Choose a year for dates without a year", key=f"{key}_override_year")
        statement_year = int(st.number_input(
            "Statement year", min_value=1, max_value=9999, value=date.today().year, step=1,
            key=f"{key}_statement_year",
        )) if override_year else None
        check_total = st.checkbox("Check against a statement total", key=f"{key}_check_total")
        total = st.number_input(
            "Expected money in minus money out", step=0.01, value=0.0,
            key=f"{key}_expected_total", help="Use the net transaction movement, not the ending account balance.",
        ) if check_total else None
    return dict(sign_convention=convention, date_order=date_order,
                number_format=number_format, expected_total=total, use_references=use_references,
                statement_year=statement_year)


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


PRESET_LABELS = {
    "This month": "This month",
    "Last month": "Last month",
    "Last 30 days": "30 days",
    "Last 3 months": "3 months",
    "Year to date": "YTD",
    "Last 12 months": "12 months",
    "All time": "All",
    "Custom": "Custom",
}
TRANSACTION_TYPES = {
    "Both": ["Income / credit", "Expense / purchase"],
    "Money in": ["Income / credit"],
    "Money out": ["Expense / purchase"],
}


def reset_dashboard_filters() -> None:
    for key in (
        "dashboard_dates",
        "dashboard_custom_dates",
        "dashboard_date_preset",
        "dashboard_accounts",
        "dashboard_categories",
        "dashboard_type",
        "dashboard_search",
        "dashboard_minimum_amount",
        "dashboard_include_transfers",
        "dashboard_tag",
    ):
        st.session_state.pop(key, None)


def apply_custom_dates() -> None:
    # The picker reports one date while a range is half chosen; wait for both.
    picked = st.session_state.get("dashboard_custom_dates")
    if isinstance(picked, (tuple, list)) and len(picked) == 2:
        st.session_state.dashboard_dates = tuple(picked)


def keep_choice(key: str, options: list[str], format_func=None) -> None:
    """Keep a choice whose label includes a live count, such as "Pending (3)".

    The browser remembers the label, so when the count changes Streamlit hands back
    the old label text instead of the option. Map it back, or fall back to the first option.
    """
    labels_key = f"_labels_{key}"
    value = st.session_state.get(key)
    if value not in options:
        value = st.session_state.get(labels_key, {}).get(value)
        st.session_state[key] = value if value in options else options[0]
    if format_func:
        # Keep older labels too: the browser can send one from a few runs back.
        labels = st.session_state.setdefault(labels_key, {})
        labels.update({format_func(option): option for option in options})


def view_switcher(label: str, options: list[str], key: str, **kwargs):
    """A segmented control that always has one option selected."""
    keep_choice(key, options, kwargs.get("format_func"))
    return st.segmented_control(
        label, options, key=key, required=True, label_visibility="collapsed", **kwargs
    )


CHART_PREFERENCES_SETTING = "chart_preferences"


def chart_preferences() -> dict:
    """Chart styles and Summary charts the person chose, kept with their account."""
    if "chart_prefs" not in st.session_state:
        try:
            saved = json.loads(repo.get_setting(CHART_PREFERENCES_SETTING, "{}") or "{}")
        except (ValueError, TypeError, SupabaseError):
            saved = {}
        st.session_state.chart_prefs = saved if isinstance(saved, dict) else {}
    return st.session_state.chart_prefs


def save_chart_preferences() -> None:
    try:
        repo.set_setting(CHART_PREFERENCES_SETTING, json.dumps(chart_preferences()))
    except SupabaseError:
        st.toast("Your chart choice applies now but couldn't be saved for next time.")


def _remember_chart_style(name: str, widget_key: str) -> None:
    chart_preferences()[name] = st.session_state[widget_key]
    save_chart_preferences()


def chart_style(name: str, options: list[str]) -> str:
    """Let the viewer switch how one chart is drawn; the choice is remembered."""
    widget_key = f"chart_style_{name}"
    if st.session_state.get(widget_key) not in options:
        saved = chart_preferences().get(name)
        st.session_state[widget_key] = saved if saved in options else options[0]
    return st.segmented_control(
        "Chart style",
        options,
        key=widget_key,
        required=True,
        label_visibility="collapsed",
        on_change=_remember_chart_style,
        args=(name, widget_key),
    )


def card_chart(figure):
    """Drop a chart's own title when it sits under a card heading."""
    if figure.data:
        figure.update_layout(title_text="", margin={"t": 16})
    return figure


def apply_date_preset(min_date: date, max_date: date) -> None:
    preset = st.session_state.dashboard_date_preset
    if preset != "Custom":
        st.session_state.dashboard_dates = AnalyticsService.preset_range(preset, min_date, max_date)
    st.session_state.pop("dashboard_custom_dates", None)


def navigate_to(page: str, section: str | None = None) -> None:
    close_dialog()
    st.session_state.nav_page = page
    if section and page == "Transactions":
        st.session_state.tx_view = section
    if section and page == "Accounts":
        st.session_state.accounts_view = section
    if section and page == "Settings":
        st.session_state.settings_view = section


def render_start_actions() -> None:
    st.header("Get started with your financial history")
    st.write("Choose how to add your first transactions. You can use both methods later.")
    bank, statement, sample = st.columns(3)
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
    if sample.button("Explore with sample data", width="stretch",
                     help="Load six months of made-up activity. Clear it in one click when you're ready."):
        try:
            with st.spinner("Adding sample data…"):
                data_service.load_sample_data(date.today())
            st.rerun()
        except (ValueError, SupabaseError):
            st.error("Couldn't finish loading sample data. Use Clear sample data, then try again.")
    st.caption("A bank connection syncs through Plaid. A CSV or text-based PDF is previewed before saving.")


@st.dialog("Clear sample data", on_dismiss=close_dialog)
def render_clear_sample_dialog() -> None:
    st.write("This removes the sample accounts and their transactions, plus sample budgets and goals you haven't changed. "
             "Your own accounts and transactions are not affected.")
    rules = data_service.rules_added_since_sample()
    remove_rules = bool(rules) and st.checkbox(
        f"Also remove {len(rules)} category rule(s) created while exploring", value=True,
        help="Rules apply to future imports too: " + ", ".join(rules[:10]))
    if st.button("Clear sample data", key="clear_sample_confirm", type="primary", width="stretch"):
        try:
            data_service.clear_sample_data(remove_rules)
            close_dialog()
            st.rerun()
        except (ValueError, SupabaseError):
            st.error("Couldn't confirm clearing sample data. Retry to recover its saved result.")


def render_sample_banner() -> None:
    try:
        loaded = bool(data_service.sample_data())
    except SupabaseError:
        return
    if not loaded:
        return
    with st.container(border=True, key="sample_banner"):
        text, action = st.columns([4, 1], vertical_alignment="center")
        text.markdown("**You're exploring sample data.** Accounts ending in “· demo” are made up.")
        action.button("Clear sample data", key="clear_sample", width="stretch",
                      on_click=open_dialog, args=("clear_sample",))
    if active_dialog("clear_sample"):
        render_clear_sample_dialog()


def render_onboarding() -> None:
    with st.container(border=True):
        heading, close = st.columns([5, 1])
        heading.subheader("Welcome to FinanceBuddy")
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
        second.caption("Use **Transactions** to correct categories, automatically categorize a merchant, or split a purchase.")
        third.markdown("**3 · Understand**")
        third.caption("Start with spending in **Overview**. Open More spending details when you want to explore further.")
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
        history = plaid_repo.get_balance_history(since=date.today() - timedelta(days=366))
    except SupabaseError:
        history = []
    return accounts, history


def render_spending_drilldown(transactions, category_event, month_event) -> None:
    categories = [item["category"] for item in AnalyticsService.category_summary(transactions)]
    clicked_categories = [str(value) for value in _selected_points(category_event, "y")]
    clicked_months = [str(value)[:7] for value in _selected_points(month_event, "x")]
    if st.session_state.get("drilldown_category") not in ["All categories"] + categories:
        st.session_state.pop("drilldown_category", None)
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


SUMMARY_CHARTS = {
    "running_total": ("Running total", "Gain or loss built up across the selected dates."),
    "savings_rate": ("Savings rate", "Share of income kept each month."),
    "daily": ("Daily spending", "Each day's spending with a 7-day average."),
    "fixed_flexible": ("Fixed vs flexible", "Bills you can't easily change vs spending you can."),
    "cash_flow": ("Where the money went", "How money in flows to each kind of spending."),
    "category_trend": ("Category by month", "Which categories grow or shrink over time."),
    "weekday": ("Day of the week", "Which days you tend to spend the most."),
    "week_of_month": ("Time of month", "Early, middle, or late-month spending."),
    "merchants": ("Top merchants", "Where your money goes most often."),
    "income": ("Income by month", "Paychecks and other income over time."),
}
DEFAULT_SUMMARY_CHARTS = ["running_total"]


def summary_chart_keys() -> list[str]:
    chosen = chart_preferences().get("summary_charts", DEFAULT_SUMMARY_CHARTS)
    return [key for key in chosen if key in SUMMARY_CHARTS] if isinstance(chosen, list) else []


@st.dialog("Add a chart", width="large")
def choose_summary_charts() -> None:
    st.write("Pick what you want to see on Summary. Every chart follows your filters.")
    current = set(summary_chart_keys())
    columns = st.columns(2)
    picks = {}
    for index, (key, (title, description)) in enumerate(SUMMARY_CHARTS.items()):
        with columns[index % 2]:
            picks[key] = st.checkbox(title, value=key in current, key=f"pick_chart_{key}")
            st.caption(description)
    if st.button("Save charts", type="primary", width="stretch"):
        chart_preferences()["summary_charts"] = [key for key, picked in picks.items() if picked]
        save_chart_preferences()
        st.rerun()


def summary_chart_figure(key: str, analysis, history, start_date: date, end_date: date):
    if key == "running_total":
        return make_profit_loss_line_chart(AnalyticsService.cumulative_cash_flow(analysis))
    if key == "savings_rate":
        return make_savings_rate_chart(AnalyticsService.monthly_savings_rate(analysis))
    if key == "daily":
        return make_daily_spending_chart(AnalyticsService.daily_spending(analysis, start_date, end_date))
    if key == "fixed_flexible":
        return make_fixed_flexible_chart(AnalyticsService.fixed_vs_flexible(analysis)["months"])
    if key == "cash_flow":
        return make_cash_flow_sankey(
            AnalyticsService.income_sources(analysis), AnalyticsService.category_expenses(analysis)
        )
    if key == "category_trend":
        return make_category_heatmap(AnalyticsService.monthly_category_expenses(analysis))
    if key == "weekday":
        return make_weekday_chart(AnalyticsService.weekday_spending(analysis, start_date, end_date))
    if key == "week_of_month":
        return make_week_of_month_chart(
            AnalyticsService.week_of_month_spending(analysis, start_date, end_date)
        )
    if key == "merchants":
        return make_merchant_bar_chart(AnalyticsService.merchant_summary(analysis, limit=10))
    income = AnalyticsService.income_summary(history)
    return make_income_chart(income["months"] if income else [])


def render_summary_charts(analysis, history, start_date: date, end_date: date) -> None:
    heading, action = st.columns([3, 1], vertical_alignment="bottom")
    heading.subheader("Your charts")
    if action.button(
        "Add a chart",
        icon=":material/add_chart:",
        width="stretch",
        help="Choose which extra charts appear on Summary. Your choice is saved with your account.",
    ):
        choose_summary_charts()
    keys = summary_chart_keys()
    if not keys:
        st.caption("No extra charts yet. Use Add a chart to pick some.")
        return
    columns = st.container(key="overview_extra_charts").columns(2)
    for index, key in enumerate(keys):
        title, description = SUMMARY_CHARTS[key]
        with columns[index % 2]:
            with st.container(border=True, key=f"fbcard_extra_{key}"):
                render_card_header(title, description)
                st.plotly_chart(
                    card_chart(summary_chart_figure(key, analysis, history, start_date, end_date)),
                    width="stretch",
                    key=f"summary_chart_{key}",
                )


def render_overview_summary(analysis, categories, history, start_date: date, end_date: date, extra_metrics, all_transactions) -> None:
    category_event = None
    category_col, activity_col = st.columns([1.15, 1], gap="large")
    with category_col:
        st.markdown("## Top spending categories")
        if categories:
            top_categories = sorted(categories, key=lambda item: item["amount"], reverse=True)[:5]
            figure = card_chart(make_category_bar_chart(top_categories))
            figure.update_layout(height=300, margin={"l": 0, "r": 60, "t": 8, "b": 24})
            figure.update_xaxes(title_text=None)
            figure.update_yaxes(title_text=None, automargin=True)
            category_event = st.plotly_chart(
                figure, width="stretch", on_select="rerun", selection_mode="points",
                key="summary_top_categories", config={"displayModeBar": False},
            )
            selected_categories = _selected_points(category_event, "y")
            if selected_categories:
                selected_rows = [
                    item for item in analysis
                    if item.amount < 0 and item.category in selected_categories
                ]
                st.caption("Transactions in " + ", ".join(str(value) for value in selected_categories))
                st.dataframe(
                    transaction_frame(sorted(selected_rows, key=lambda item: item.date, reverse=True)),
                    key="summary_category_transactions", width="stretch", hide_index=True,
                    column_config={
                        "Amount": st.column_config.NumberColumn(format="$%.2f"),
                        "Date": st.column_config.DateColumn(format="MMM D, YYYY"),
                    },
                )
        else:
            st.caption("No spending in this selection.")
    with activity_col:
        st.markdown("## Recent activity")
        recent = sorted(analysis, key=lambda item: (item.date, item.id), reverse=True)[:5]
        rows = []
        for item in recent:
            amount_class = "fb-positive" if item.amount > 0 else ""
            rows.append(
                '<div class="fb-activity-row"><div class="fb-activity-copy">'
                f'<div class="fb-activity-name">{html.escape(item.description)}</div>'
                f'<div class="fb-card-meta">{item.date.strftime("%b %-d")} · '
                f'{html.escape(item.category)} · {html.escape(item.account_name)}{" · Pending" if item.pending else ""}</div></div>'
                f'<span class="fb-activity-amount {amount_class}">{format_currency(item.amount)}</span></div>'
            )
        st.markdown('<div class="fb-activity">' + "".join(rows) + '</div>', unsafe_allow_html=True)
        st.button("View all transactions", key="summary_transactions", type="tertiary",
                  on_click=navigate_to, args=("Transactions",))

    uncategorized = next((item for item in categories if item["category"] == "Uncategorized"), None)
    if uncategorized:
        with st.container(key="summary_review"):
            st.caption(
                f"{uncategorized['transactions']} {'transaction' if uncategorized['transactions'] == 1 else 'transactions'}, totaling "
                f"{format_currency(uncategorized['amount'])}, still {'needs' if uncategorized['transactions'] == 1 else 'need'} a category."
            )
            st.button("Review categories", key="review_uncategorized", type="tertiary",
                      on_click=navigate_to, args=("Transactions", "Needs a category"))
    else:
        render_insights(AnalyticsService.insights(analysis, history, limit=1, excluded=recurring_exclusions()))

    with st.expander("More spending details"):
        if extra_metrics:
            render_metrics(extra_metrics, container_key="overview_more_totals")
        render_card_payment_fix(all_transactions)
        render_overview_details(analysis, categories, history, start_date, end_date)


def render_overview_details(analysis, categories, history, start_date: date, end_date: date) -> None:
    monthly = AnalyticsService.monthly_breakdown(analysis)
    month_event = category_event = None
    left, right = st.container(key="overview_detail_charts").columns(2)
    with left, st.container(border=True, key="fbcard_cash_flow"):
        title, picker = st.columns([1, 1], vertical_alignment="center")
        with picker:
            style = chart_style("cash_flow", ["Columns", "Lines", "Net"])
        with title:
            if style == "Net":
                render_card_header("Saved each month", "Below zero means more went out than came in.")
            else:
                render_card_header("Money in vs money out", "Monthly totals.")
        if style == "Columns":
            month_event = st.plotly_chart(
                card_chart(make_monthly_bar_chart(monthly)),
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key="overview_month_chart",
            )
        elif style == "Lines":
            st.plotly_chart(card_chart(make_monthly_line_chart(monthly)), width="stretch", key="overview_month_lines")
        else:
            month_event = st.plotly_chart(
                card_chart(make_monthly_net_chart(monthly)),
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key="overview_month_net",
            )
    with right, st.container(border=True, key="fbcard_categories"):
        title, picker = st.columns([1, 1], vertical_alignment="center")
        with picker:
            style = chart_style("categories", ["Donut", "Bars", "Map"])
        with title:
            total = sum(item["amount"] for item in categories)
            render_card_header("Where it went", f"{format_currency(total)} spent")
        if style == "Donut":
            st.plotly_chart(
                card_chart(make_category_donut_chart({item["category"]: item["amount"] for item in categories})),
                width="stretch",
                key="overview_category_donut",
            )
        elif style == "Bars":
            category_event = st.plotly_chart(
                card_chart(make_category_bar_chart(categories)),
                width="stretch",
                on_select="rerun",
                selection_mode="points",
                key="overview_category_chart",
            )
        else:
            st.plotly_chart(card_chart(make_category_treemap(categories)), width="stretch", key="overview_category_map")

    st.header("Explore spending")
    st.caption("Click a bar in either chart, or pick a category, to see the transactions behind it.")
    render_spending_drilldown(analysis, category_event, month_event)

    render_summary_charts(analysis, history, start_date, end_date)

    st.header("What changed")
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
    forecast = AnalyticsService.cash_flow_forecast(history, recurring_exclusions())
    first, second = st.columns(2)
    with first, st.container(border=True, key="fbcard_unusual"):
        render_card_header("Unusual expenses")
        if unusual:
            for transaction in unusual:
                st.write(f"{transaction.description} · {format_currency(abs(transaction.amount))}")
        else:
            st.caption("No unusually large expenses detected in this selection.")
    with second, st.container(border=True, key="fbcard_forecast"):
        render_card_header("Next-month estimate")
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
                f"the latest {forecast['months']} complete month(s) of history.".replace("$", "\\$")
            )
        else:
            st.caption("More history is needed for an estimate.")

    with st.expander("Category table"):
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


def render_overview_trends(analysis, start_date: date, end_date: date) -> None:
    with st.container(border=True, key="fbcard_savings_rate"):
        render_card_header("Savings rate by month", "Net cash flow as bars, the share of income kept as a line.")
        st.plotly_chart(
            card_chart(make_savings_rate_chart(AnalyticsService.monthly_savings_rate(analysis))),
            width="stretch",
        )

    split = AnalyticsService.fixed_vs_flexible(analysis)
    with st.container(border=True, key="fbcard_fixed_flexible"):
        title, picker = st.columns([1, 1], vertical_alignment="center")
        with title:
            render_card_header("Fixed costs vs flexible spending")
        with picker:
            style = chart_style("fixed_flexible", ["Stacked", "Side by side"])
        first, second, third = st.columns(3)
        first.metric("Fixed costs", format_currency(split["fixed"]),
                     help="Housing, utilities, insurance, debt payments, and education.")
        second.metric("Flexible spending", format_currency(split["flexible"]),
                      help="Everything else: day-to-day spending you have the most control over.")
        third.metric("Fixed share", f"{split['fixed_share']:.0f}%")
        st.plotly_chart(
            card_chart(make_fixed_flexible_chart(split["months"], stacked=style == "Stacked")),
            width="stretch",
        )

    with st.container(border=True, key="fbcard_daily"):
        render_card_header("Daily spending", "Each day with a 7-day average.")
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
            card_chart(make_daily_spending_chart(AnalyticsService.daily_spending(analysis, start_date, end_date))),
            width="stretch",
        )

    with st.container(border=True, key="fbcard_sankey"):
        render_card_header("Where the money went", "Money in on the left, spending and savings on the right.")
        st.plotly_chart(
            card_chart(make_cash_flow_sankey(
                AnalyticsService.income_sources(analysis), AnalyticsService.category_expenses(analysis)
            )),
            width="stretch",
        )

    with st.container(border=True, key="fbcard_category_trend"):
        title, picker = st.columns([1, 1], vertical_alignment="center")
        with title:
            render_card_header("Category by month")
        with picker:
            style = chart_style("category_trend", ["Heat map", "Lines"])
        month_count = len({item.date.strftime("%Y-%m") for item in analysis})
        if month_count >= 2:
            monthly_categories = AnalyticsService.monthly_category_expenses(analysis)
            figure = (
                make_category_heatmap(monthly_categories)
                if style == "Heat map"
                else make_category_trend_lines(monthly_categories)
            )
            st.plotly_chart(card_chart(figure), width="stretch")
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
    with left, st.container(border=True, key="fbcard_weekday"):
        render_card_header("Day of the week", "Average spending per day.")
        st.plotly_chart(card_chart(make_weekday_chart(weekday)), width="stretch")
    with right, st.container(border=True, key="fbcard_week_of_month"):
        render_card_header("Time of month", "Average spending per day in each part of the month.")
        st.plotly_chart(
            card_chart(make_week_of_month_chart(
                AnalyticsService.week_of_month_spending(analysis, start_date, end_date)
            )),
            width="stretch",
        )

    st.header("Top merchants")
    merchants = AnalyticsService.merchant_summary(analysis, limit=10)
    if merchants:
        style = chart_style("merchants", ["Bars", "Table"])
        if style == "Bars":
            st.plotly_chart(card_chart(make_merchant_bar_chart(merchants)), width="stretch")
        else:
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


RECURRING_EXCLUSIONS_SETTING = "recurring_exclusions"
CASH_BALANCE_SETTING = "manual_cash_balance"


def recurring_exclusions() -> list[str]:
    """Lowercased merchants the person said are not recurring bills."""
    if "recurring_exclusion_keys" not in st.session_state:
        try:
            saved = data_service.get_json_setting(RECURRING_EXCLUSIONS_SETTING, [])
        except SupabaseError:
            saved = []
        st.session_state.recurring_exclusion_keys = sorted({str(item) for item in saved})
    return st.session_state.recurring_exclusion_keys


def save_recurring_exclusions(keys: list[str]) -> None:
    keys = sorted(set(keys))[:200]
    try:
        data_service.set_json_setting(RECURRING_EXCLUSIONS_SETTING, keys)
        st.session_state.recurring_exclusion_keys = keys
        st.rerun()
    except (ValueError, SupabaseError):
        st.error("Couldn't save this change. Try again.")


def render_cash_flow_timeline(active, income, history) -> None:
    st.header("Next 30 days")
    today = date.today()
    latest = max((item.date for item in history), default=None)
    if latest and latest < today - timedelta(days=35):
        st.caption(f"Your saved history ends {latest.strftime('%b %-d, %Y')}, so these expected dates may be out of date.")
    saved = data_service.get_json_setting(CASH_BALANCE_SETTING, {})
    try:
        starting, as_of = money(saved["amount"]), date.fromisoformat(saved["as_of"])
    except (KeyError, TypeError, ValueError):
        starting, as_of = None, None
    events = AnalyticsService.cash_flow_timeline(active, income, today, 30, starting)
    if not events:
        st.caption("No recurring bills or paydays are expected in the next 30 days.")
    else:
        st.plotly_chart(card_chart(make_cash_flow_timeline(events)), width="stretch")
        _money_table(
            pd.DataFrame(events)[["date", "label", "amount", "running"]].rename(columns={
                "date": "Date", "label": "Expected", "amount": "Amount",
                "running": "Balance" if starting is not None else "Change from today"}),
            ["Amount", "Balance" if starting is not None else "Change from today"],
            column_config={"Date": st.column_config.DateColumn(format="MMM D")},
        )
    with st.expander("Starting cash" + (f" · {format_currency(starting)} as of {as_of.strftime('%b %-d')}" if starting is not None else "")):
        if as_of and as_of < today - timedelta(days=7):
            st.warning("This balance is more than a week old. Update it for an accurate timeline.")
        amount = st.number_input("Cash in checking today", step=50.0, key="manual_cash_amount",
                                 value=float(starting) if starting is not None else 0.0)
        if st.button("Save starting cash", key="manual_cash_save"):
            try:
                data_service.set_json_setting(CASH_BALANCE_SETTING, {"amount": str(money(amount)), "as_of": today.isoformat()})
                st.rerun()
            except (ValueError, SupabaseError):
                st.error("Couldn't save the starting cash. Try again.")


def render_overview_recurring(history) -> None:
    st.caption(
        "Uses your full history for the selected accounts and categories, because recurring "
        "charges and pay schedules only show up across several months."
    )
    excluded = recurring_exclusions()
    recurring = AnalyticsService.recurring_expenses(history, excluded)
    active = [item for item in recurring if item["active"]]
    stopped = [item for item in recurring if not item["active"]]
    income = AnalyticsService.income_summary(history)
    render_cash_flow_timeline(active, income, history)
    st.header("Recurring charges")
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
        with st.popover("Not a recurring bill?", icon=":material/block:"):
            merchant = st.selectbox("Charge", [item["merchant"] for item in active], key="recurring_exclude_choice")
            if st.button("Stop treating it as recurring", key="recurring_exclude", type="primary"):
                save_recurring_exclusions(excluded + [merchant.lower()])
    else:
        st.caption("No charges repeat on a regular schedule in this selection yet.")
    if excluded:
        with st.expander(f"Not treated as recurring ({len(excluded)})"):
            for key in excluded:
                name_col, restore_col = st.columns([4, 1], vertical_alignment="center")
                name_col.write(key)
                if restore_col.button("Restore", key=f"recurring_restore_{_plan_key(key)}", type="tertiary",
                                      help=f"Treat {key} as recurring again"):
                    save_recurring_exclusions([item for item in excluded if item != key])
    if stopped:
        with st.expander(f"Charges that seem to have stopped ({len(stopped)})"):
            for item in stopped:
                st.write(
                    f"{item['merchant']} · {format_currency(item['amount'])} {item['cadence'].lower()} · "
                    f"last charged {item['last_date'].strftime('%b %-d, %Y')}"
                )

    st.header("Income")
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
        "Balances come from connected banks, refresh on every sync, and ignore the filters above."
    )
    overview = AnalyticsService.balance_overview(accounts)
    counted = AnalyticsService.exclude_transfers(all_transactions)
    income = AnalyticsService.income_summary(counted)
    recurring = AnalyticsService.recurring_expenses(counted, recurring_exclusions())
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
    st.caption("History uses recorded USD balances, including retained snapshots from disconnected accounts. It carries each account’s last known balance forward.")
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
        try:
            repo.correct_categories([item.id for item in candidates], "Credit Card Payments")
            st.rerun()
        except (ValueError, SupabaseError):
            st.error("Couldn't confirm the category changes. Retry to recover the saved result.")


def render_overview(transactions, view: dict):
    if not transactions:
        st.info("No transactions match the current filters.")
        st.button("Reset filters", key="overview_empty_reset", on_click=reset_dashboard_filters)
        return

    st.markdown(f'<p class="fb-period">{period_label(transactions)}</p>', unsafe_allow_html=True)
    previous_start, previous_end = view["previous_range"]
    metrics = AnalyticsService.dashboard_metrics(
        transactions, view["previous_transactions"], view["include_transfers"]
    )
    spending_labels = {"Spending", "Purchases & fees"}
    primary_metrics = [metric for metric in metrics if metric["label"] in spending_labels]
    primary_metrics += [metric for metric in metrics if metric["label"] not in spending_labels][:2]
    with st.container(key="overview_totals"):
        render_metrics(
            primary_metrics,
            delta_caption=(
                f"Changes compare with {previous_start.strftime('%b %-d, %Y')}–"
                f"{previous_end.strftime('%b %-d, %Y')}."
            ),
        )
    remaining = [metric for metric in metrics if metric not in primary_metrics]

    def counted(items):
        return items if view["include_transfers"] else AnalyticsService.exclude_transfers(items)

    analysis = counted(transactions)
    # Recurring charges, income cadence, and forecasts need more than a short date
    # range, so they use all dates that match the other filters.
    history = counted(view["history_transactions"])

    categories = view["summary"]["categories"]
    labels = ["Summary", "Trends", "Habits", "Recurring & income", "Balances"]
    selected = view_switcher("Overview view", labels, "overview_view")
    if selected == "Summary":
        with st.container(key="overview_summary"):
            render_overview_summary(analysis, categories, history, view["start_date"], view["end_date"], remaining, view["all_transactions"])
    elif selected == "Trends":
        render_overview_trends(analysis, view["start_date"], view["end_date"])
    elif selected == "Habits":
        render_overview_habits(analysis, view["start_date"], view["end_date"])
    elif selected == "Recurring & income":
        render_overview_recurring(history)
    else:
        balance_accounts, balance_history = load_balances()
        if balance_accounts:
            render_overview_balances(balance_accounts, balance_history, view["all_transactions"])
        else:
            st.info("Connect a bank to see USD balances and net worth history.")


def _finish_transaction_edit(message: str) -> None:
    """Close the editor and reset table selection: saved edits can reorder rows."""
    st.toast(message)
    close_dialog()
    st.session_state.pop("last_table_pick", None)
    st.session_state.pop("rule_match_counts", None)
    st.session_state.tx_table_version = st.session_state.get("tx_table_version", 0) + 1
    st.rerun()


def _rule_match_count(phrase: str) -> int:
    """Saved transactions whose description or merchant contains the phrase (rules check descriptions only)."""
    counts = st.session_state.setdefault("rule_match_counts", {})
    if phrase not in counts:
        counts[phrase] = repo.get_page({"search": phrase}, limit=1)["count"]
    return counts[phrase]


def render_category_editor(selected) -> None:
    current_index = CATEGORIES.index(selected.category) if selected.category in CATEGORIES else 0
    suggested = rule_keyword([selected.description], selected.merchant_name) or ""
    with st.form(f"category_correction_{selected.id}", border=False):
        new_category = st.selectbox("Category", CATEGORIES, index=current_index)
        with st.expander("Categorize similar transactions"):
            rule_keyword_text = st.text_input(
                "Merchant name or phrase", value=suggested, max_chars=256,
                help="Descriptions that contain this exact phrase use the selected category.",
            )
            if suggested:
                matches = _rule_match_count(suggested)
                st.caption(f'About {matches:,} saved transaction(s) contain "{suggested}".')
                warning = keyword_warning(suggested, matches, catalog["count"])
                if warning:
                    st.warning(warning)
            else:
                st.caption("We couldn't find a reliable merchant phrase in this description. Type one to create a rule.")
            save_rule = st.checkbox("Use this category for future imports that contain the phrase")
            apply_similar = st.checkbox(
                "Also update saved transactions that contain the phrase",
                help="Replaces categories you set by hand and the categories of split parts for those transactions.",
            )
        submitted = st.form_submit_button(
            "Save category",
            type="primary",
            width="stretch",
            help="Save this category and any matching rule you chose above.",
        )
    if submitted:
        phrase = rule_keyword_text.strip() if save_rule or apply_similar else ""
        if (save_rule or apply_similar) and not phrase:
            st.error("Enter a merchant name or phrase, or uncheck the rule options.")
        elif phrase and is_processor_phrase(phrase):
            st.error(f'"{phrase}" names a payment method, not a merchant. Enter the merchant\'s name instead.')
        else:
            try:
                data_service.correct_category(selected.id, new_category, phrase, apply_similar)
                _finish_transaction_edit("Category saved." if not apply_similar else "Category saved and matching transactions updated.")
            except (ValueError, SupabaseError):
                st.error("Couldn't confirm the category save. Your choices are still here; retry to recover its saved result.")


def render_split_editor(selected) -> None:
    # Split the source, not a projected allocation. Re-splitting replaces its
    # category allocations and keeps the provider/import ID unchanged.
    source_id = selected.source_transaction_id or selected.id
    source_amount = abs(selected.source_amount if selected.source_amount is not None else selected.amount)
    if source_amount < Decimal("0.02"):
        st.caption("This amount is too small to split.")
        return
    if selected.source_transaction_id or selected.split_needs_review:
        st.info(f"This purchase is already split. Saving replaces both parts of the {format_currency(source_amount)} purchase.")
    category_one = st.selectbox("First category", CATEGORIES, key=f"split_cat_1_{source_id}",
                                index=CATEGORIES.index(selected.category) if selected.category in CATEGORIES else 0)
    # Keys are per purchase: a remembered amount must never exceed another purchase's limit.
    amount_one = st.number_input(
        "First amount",
        min_value=0.01,
        max_value=float(source_amount - Decimal("0.01")),
        value=float(round(source_amount / 2, 2)),
        step=0.01,
        key=f"split_amount_{source_id}",
    )
    category_two = st.selectbox("Second category", CATEGORIES, key=f"split_cat_2_{source_id}")
    amount_two = source_amount - money(amount_one)
    st.caption(f"Second amount: {format_currency(amount_two)}")
    if st.button(
        "Split transaction", type="primary", width="stretch",
        help="Keep the original transaction and divide its amount between two categories. Existing allocations are replaced.",
    ):
        try:
            repo.split_transaction(source_id, category_one, amount_one, category_two, amount_two)
            _finish_transaction_edit("Transaction split.")
        except (ValueError, SupabaseError):
            st.error("The split response could not be confirmed. Retry to recover its saved result.")


def parse_tags(text: str) -> list[str]:
    return sorted({tag.strip().lower() for tag in text.replace("#", " ").replace(",", " ").split() if tag.strip()})


def render_notes_editor(selected) -> None:
    key = selected.source_transaction_id or selected.id
    notes = st.text_area("Notes", value=selected.notes or "", max_chars=2000, key=f"notes_{key}")
    tags = st.text_input("Tags", value=" ".join(selected.tags), key=f"tags_{key}",
                         placeholder="e.g. japan-trip reimbursable", help="Separate tags with spaces or commas.")
    if selected.source_transaction_id:
        st.caption("Notes and tags apply to the whole split purchase.")
    if st.button("Save notes", type="primary", width="stretch"):
        try:
            repo.annotate(selected.id, notes, parse_tags(tags))
            _finish_transaction_edit("Notes saved.")
        except (ValueError, SupabaseError):
            st.error("Couldn't confirm the save. Use up to 20 tags of at most 40 characters, then retry.")
    if selected.id.startswith("manual:"):
        confirm = st.checkbox("I want to delete this manual transaction", key=f"confirm_delete_{selected.id}")
        if st.button("Delete transaction", disabled=not confirm, width="stretch"):
            try:
                repo.delete_manual_transaction(selected.id)
                _finish_transaction_edit("Transaction deleted.")
            except (ValueError, SupabaseError):
                st.error("Couldn't confirm the deletion. Retry to recover its saved result.")


@st.dialog("Add a transaction", on_dismiss=close_dialog)
def render_add_transaction_dialog() -> None:
    accounts = {f'{row["name"]} · {row["account_type"]}': row for row in catalog["accounts"] if not row.get("plaid_item_id")}
    choices = ["Cash"] + [label for label in accounts if label != "Cash · Checking"]
    when = st.date_input("Date", value=date.today(), key="manual_date")
    description = st.text_input("Description", max_chars=256, key="manual_description", placeholder="e.g. Farmers market")
    direction = st.segmented_control("Direction", ["Money out", "Money in"], default="Money out", required=True, key="manual_direction")
    amount = st.number_input("Amount", min_value=0.01, value=10.0, step=1.0, key="manual_amount")
    category = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index("Uncategorized"), key="manual_category")
    account = st.selectbox("Account", choices, key="manual_account",
                           help="Bank-connected accounts aren't listed: their own copy of a purchase arrives with the next sync.")
    notes = st.text_input("Notes (optional)", max_chars=2000, key="manual_notes")
    tags = st.text_input("Tags (optional)", key="manual_tags", placeholder="e.g. japan-trip")
    # One ID per entry, so retrying an uncertain save can't add a second copy.
    identifier = st.session_state.setdefault("manual_transaction_id", f"manual:{uuid.uuid4().hex}")
    if st.button("Save transaction", type="primary", width="stretch", disabled=not description.strip()):
        row = accounts.get(account)
        signed = money(amount) if direction == "Money in" else -money(amount)
        try:
            repo.add_manual_transaction(Transaction(
                id=identifier, date=when, description=description.strip(), amount=signed, category=category,
                account_id=row["id"] if row else None,
                account_name=row["name"] if row else "Cash", account_type=row["account_type"] if row else "Checking",
            ), notes, parse_tags(tags))
            st.session_state.pop("manual_transaction_id", None)
            _finish_transaction_edit("Transaction added.")
        except (ValueError, SupabaseError):
            st.error("Couldn't confirm the save. Your entry is still here; retry to recover its saved result.")


@st.dialog("Edit a transaction", width="medium", on_dismiss=close_dialog)
def render_transaction_editor(selected) -> None:
    st.write(selected.description)
    amount_class = "fb-positive" if selected.amount > 0 else "fb-negative"
    st.markdown(
        f'<p class="fb-card-sub">{selected.date.strftime("%b %-d, %Y")} · '
        f"{html.escape(selected.account_name)}"
        f'{" · Pending" if selected.pending else ""}</p>'
        f'<p class="{amount_class}" style="font-family:Newsreader,Georgia,serif;font-size:2rem;margin:0">'
        f"{format_currency(selected.amount)}</p>",
        unsafe_allow_html=True,
    )
    source_amount = selected.source_amount if selected.source_amount is not None else selected.amount
    tabs = ["Category & rule"]
    if source_amount < 0 or (source_amount > 0 and selected.allocations):
        tabs.append("Split")
    tabs.append("Notes & tags")
    tab = view_switcher("Edit", tabs, "transaction_editor_tab")
    if tab == "Split":
        render_split_editor(selected)
    elif tab == "Notes & tags":
        render_notes_editor(selected)
    else:
        render_category_editor(selected)
    if st.button("Cancel", width="stretch"):
        close_dialog()
        st.rerun()


def render_triage_queue() -> None:
    """Categorize uncategorized purchases one merchant at a time."""
    groups = uncategorized_groups(repo.get_filtered(st.session_state.transaction_filters))
    if not groups:
        return
    st.subheader("Sort by merchant")
    st.caption("Choose a category once for every uncategorized purchase from a merchant.")
    choices = [category for category in CATEGORIES if category != "Uncategorized"]
    for group in groups:
        key = hashlib.sha256((group["keyword"] or group["merchant"]).encode()).hexdigest()[:12]
        with st.container(border=True, key=f"triage_{key}"):
            st.markdown(
                f"**{html.escape(group['merchant'])}** · {group['count']} purchase(s) · {format_currency(group['total'])}",
            )
            if group["count"] > 1 or group["sample"] != group["merchant"]:
                st.caption(f"e.g. {group['sample']}")
            category_col, action_col = st.columns([3, 1], vertical_alignment="bottom")
            category = category_col.selectbox(
                f"Category for {group['merchant']}", choices, index=None, placeholder="Choose a category",
                key=f"triage_category_{key}",
            )
            save_rule = bool(group["keyword"]) and st.checkbox(
                f'Also use it for future imports containing "{group["keyword"]}"', value=True,
                key=f"triage_rule_{key}",
            )
            if action_col.button("Apply", key=f"triage_apply_{key}", type="primary", width="stretch", disabled=category is None):
                try:
                    if save_rule:
                        repo.triage_category(group["ids"], category, group["keyword"])
                    else:
                        repo.correct_categories(group["ids"], category)
                    st.toast(f"{group['count']} purchase(s) from {group['merchant']} moved to {category}.")
                    st.session_state.tx_table_version = st.session_state.get("tx_table_version", 0) + 1
                    st.rerun()
                except (ValueError, SupabaseError):
                    st.error("Couldn't confirm this change. Some purchases may have changed; retry to recover its saved result.")


def _show_all_transactions() -> None:
    st.session_state.tx_view = "All"


DUPLICATE_DISMISSALS_SETTING = "duplicate_dismissals"
TRANSACTION_SOURCES = {"plaid:": "Bank", "statement:": "Statement", "manual:": "Manual"}


def duplicate_dismissals() -> list[str]:
    try:
        return data_service.get_json_setting(DUPLICATE_DISMISSALS_SETTING, [])
    except SupabaseError:
        return []


def _transaction_source(item) -> str:
    return next((label for prefix, label in TRANSACTION_SOURCES.items() if item.id.startswith(prefix)), "Saved")


def render_duplicate_group(group) -> None:
    group_key = AnalyticsService.duplicate_group_key(group)
    key = _plan_key(group_key)
    with st.container(border=True, key=f"duplicate_{key}"):
        st.markdown(f"**{html.escape(group[0].description)}** · {group[0].date.strftime('%b %-d, %Y')} · "
                    f"{format_currency(group[0].amount)} · {html.escape(group[0].account_name)}")
        # Keep a bank copy when there is one: the bank would re-create a removed one.
        keep = next((item for item in group if item.id.startswith("plaid:")), group[0])
        for index, item in enumerate(group, start=1):
            label_col, action_col = st.columns([3, 1], vertical_alignment="center")
            label_col.write(f"Copy {index} · {_transaction_source(item)} · {item.category}")
            removable = item.id != keep.id and not item.id.startswith("plaid:") and not item.allocations
            if removable and action_col.button(
                "Remove this copy", key=f"duplicate_remove_{key}_{index}", type="tertiary", width="stretch",
                help=f"Remove copy {index} of {item.description} and keep copy {group.index(keep) + 1}",
            ):
                try:
                    repo.merge_duplicate(keep.id, item.id)
                    st.toast("Duplicate removed.")
                    st.session_state.tx_table_version = st.session_state.get("tx_table_version", 0) + 1
                    st.rerun()
                except (ValueError, SupabaseError):
                    st.error("Couldn't confirm the removal. It may have changed; retry to recover its saved result.")
        if st.button("Keep both", key=f"duplicate_keep_{key}", help="These are separate purchases. Stop suggesting them."):
            try:
                data_service.set_json_setting(DUPLICATE_DISMISSALS_SETTING, (duplicate_dismissals() + [group_key])[-300:])
                st.rerun()
            except (ValueError, SupabaseError):
                st.error("Couldn't save this choice. Try again.")


def render_transactions(transactions):
    if not transactions:
        view = st.session_state.get('tx_view', 'All')
        if view in ('Pending', 'Needs a category'):
            st.success('Nothing is pending among transactions matching your filters.' if view == 'Pending' else 'All transactions matching your filters have a category.')
            st.button('View all transactions', key='tx_empty_all', on_click=_show_all_transactions)
        else:
            st.info("No transactions match the current filters.")
        if len(st.session_state.get("transaction_cursors", [None])) > 1 and st.button("Back to first page"):
            st.session_state.transaction_cursors = [None]
            st.rerun()
        st.button("Reset filters", key="transactions_empty_reset", on_click=reset_dashboard_filters)
        return

    if any(item.split_needs_review for item in transactions):
        st.warning("A bank update changed the amount of a split transaction. Its current total is counted once; review and split it again below.")

    duplicates = AnalyticsService.duplicate_candidates(transactions, duplicate_dismissals())
    duplicate_ids = {item.id for group in duplicates for item in group}
    views = {
        "All": transactions,
        "Needs a category": [item for item in transactions if item.category == "Uncategorized"],
        "Pending": [item for item in transactions if item.pending],
        "Possible duplicates": [item for item in transactions if item.id in duplicate_ids],
    }
    view_col, add_col, export_col = st.container(key="tx_toolbar").columns([3, 1, 1], vertical_alignment="center")
    add_col.button("Add transaction", icon=":material/add:", width="stretch", on_click=open_dialog,
                   args=("add_transaction",), help="Record a cash purchase or an adjustment that isn't on a statement.")
    with view_col:
        view = view_switcher(
            "Show", list(views), "tx_view", format_func=lambda name: name
        )
    if export_col.button("Prepare CSV export", icon=":material/download:", width="stretch"):
        st.session_state.prepared_csv = transactions_to_csv(repo.get_filtered(st.session_state.transaction_filters))
        st.session_state.prepared_csv_query = st.session_state.transaction_query
    if st.session_state.get("prepared_csv_query") == st.session_state.get("transaction_query") and st.session_state.get("prepared_csv"):
        st.download_button("Download CSV", st.session_state.prepared_csv, file_name="financebuddy-transactions.csv", mime="text/csv")
    before, after, status = st.columns([1, 1, 3])
    if before.button("Previous page", disabled=len(st.session_state.transaction_cursors) == 1):
        st.session_state.transaction_cursors.pop()
        st.rerun()
    if after.button("Next page", disabled=st.session_state.transaction_next_cursor is None):
        st.session_state.transaction_cursors.append(st.session_state.transaction_next_cursor)
        st.rerun()
    status.caption(f"Page {len(st.session_state.transaction_cursors)} · up to 200 transactions per page. Duplicate suggestions use this page.")
    shown = views[view]
    if view == "Needs a category" and shown:
        render_triage_queue()
    if not shown:
        st.success(
            {
                "Needs a category": "All transactions matching your filters have a category.",
                "Pending": "Nothing is pending among transactions matching your filters.",
                "Possible duplicates": "No exact duplicate date, amount, merchant, and account combinations found.",
            }.get(view, "No transactions to show.")
        )
        st.button("View all transactions", key="tx_empty_all", on_click=_show_all_transactions)
        return

    picked_row = render_transaction_table(shown, selection_key=f"transactions_table_{view}",
                                          table_version=st.session_state.get("tx_table_version", 0))
    shown_ids = [item.id for item in shown]
    if picked_row is not None and picked_row < len(shown):
        picked_id = shown_ids[picked_row]
        # Open the editor for a new table click only, not on every rerun.
        if st.session_state.get("last_table_pick") != picked_id:
            st.session_state.last_table_pick = picked_id
            open_dialog("transaction", id=picked_id)
    with st.container(key="tx_desktop_edit"):
        target = st.session_state.get("last_table_pick")
        st.button(
            "Edit selected transaction", icon=":material/edit:", on_click=open_dialog, args=("transaction",),
            kwargs={"id": target if target in shown_ids else shown_ids[0]},
            help="Edits the row you last clicked in the table, or the first row.",
        )
    editing = active_dialog("transaction")
    if editing:
        selected = next((item for item in transactions if item.id == editing.get("id")), None)
        if selected is None:
            # Filters, splits or a bank update can remove the row being edited.
            close_dialog()
            st.toast("That transaction is no longer in this view.")
        else:
            render_transaction_editor(selected)
    elif active_dialog("add_transaction"):
        render_add_transaction_dialog()

    with st.expander(f"Potential duplicates ({len(duplicates)})", expanded=view == "Possible duplicates"):
        if not duplicates:
            st.caption("No exact duplicate date, amount, merchant, and account combinations found.")
        else:
            st.caption(
                "Removing an imported copy points its statement row at the copy you keep, so re-importing the file "
                "won't bring it back. Undo is then unavailable for that import. Bank copies can only be kept."
            )
        for group in duplicates:
            render_duplicate_group(group)


PLAN_SAVE_ERROR = "Couldn't confirm the save. Your choices are still here; retry to recover its saved result."


def _plan_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def _plan_saved(message: str) -> None:
    st.toast(message)
    close_dialog()
    st.rerun()


def _use_suggested_limit(key: str, amount: Decimal) -> None:
    st.session_state[key] = float(amount.to_integral_value(rounding="ROUND_CEILING"))


@st.dialog("Budget", on_dismiss=close_dialog)
def render_budget_dialog(budgets, transactions, category=None) -> None:
    limits = {item.category: item.monthly_limit for item in budgets}
    editing = category in limits
    if editing:
        st.markdown(f"**{html.escape(category)}**")
    else:
        choices = [item for item in CATEGORIES if item not in TRANSFER_CATEGORIES]
        category = st.selectbox("Category", choices, index=choices.index(category) if category in choices else 0,
                                key="budget_dialog_category")
        if category in limits:
            st.caption(f"Updates the existing {format_currency(limits[category])} budget for {category}.")
    limit_key = f"budget_limit_{_plan_key(category)}"
    history = AnalyticsService.category_spend_history(transactions, category)
    if history:
        st.caption(
            f"{history['last_label']}: {format_currency(history['last_month'])} · "
            f"{history['months']}-month average: {format_currency(history['average'])}"
        )
        st.button(f"Use the {history['months']}-month average", key="budget_use_average",
                  on_click=_use_suggested_limit, args=(limit_key, history["average"]),
                  disabled=not history["average"], help="Rounded up to the next whole dollar.")
    # Seeded through state, not value=, because the suggestion button also sets it.
    st.session_state.setdefault(limit_key, float(limits.get(category, 500)))
    limit = st.number_input("Monthly limit", min_value=1.0, step=25.0, key=limit_key)
    if st.button("Save budget", type="primary", width="stretch"):
        try:
            repo.upsert_budget(Budget(category=category, monthly_limit=limit))
            _plan_saved("Budget saved.")
        except (ValueError, SupabaseError):
            st.error(PLAN_SAVE_ERROR)


@st.dialog("Remove budget", on_dismiss=close_dialog)
def render_remove_budget_dialog(category) -> None:
    st.write(f"Remove the {category} budget? Transactions and categories are not affected.")
    remove, cancel = st.columns(2)
    if remove.button("Remove budget", type="primary", width="stretch"):
        try:
            repo.delete_budget(category)
            _plan_saved("Budget removed.")
        except SupabaseError:
            st.error(PLAN_SAVE_ERROR)
    if cancel.button("Cancel", width="stretch"):
        close_dialog()
        st.rerun()


def render_budgets(transactions) -> None:
    title, action = st.columns([3, 1], vertical_alignment="bottom")
    title.header("Monthly budgets")
    action.button("New budget", icon=":material/add:", type="primary", width="stretch",
                  on_click=open_dialog, args=("budget",))
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
            key = _plan_key(row["category"])
            meta_col, edit_col, remove_col = st.container(key=f"budget_card_{key}").columns(
                [4, 1, 1], vertical_alignment="center")
            meta_col.markdown(f'<p class="fb-budget-meta">{meta}</p>', unsafe_allow_html=True)
            edit_col.button("Edit", key=f"budget_edit_{key}", type="tertiary", width="stretch",
                            help=f"Change the {row['category']} limit",
                            on_click=open_dialog, args=("budget",), kwargs={"category": row["category"]})
            remove_col.button("Remove", key=f"budget_remove_{key}", type="tertiary", width="stretch",
                              help=f"Remove the {row['category']} budget",
                              on_click=open_dialog, args=("remove_budget",), kwargs={"category": row["category"]})
            if row["status"] == "Over budget":
                st.error(f"Over budget by {format_currency(row['spent'] - row['limit'])}")
            elif row["status"] == "Projected to go over":
                st.warning(
                    f"At this pace, {row['category']} reaches about {format_currency(row['projected'])} "
                    f"by month end, {format_currency(row['projected'] - row['limit'])} over the limit."
                )
    elif not budgets:
        st.info("Create a category budget to start tracking monthly limits.")

    unbudgeted = AnalyticsService.unbudgeted_spending(budgets, transactions, selected_month) if selected_month else []
    if unbudgeted:
        with st.container(border=True, key="unbudgeted_spending"):
            total = sum((row["spent"] for row in unbudgeted), Decimal(0))
            st.markdown(f"**Unbudgeted spending: {format_currency(total)}** in {len(unbudgeted)} categor{'y' if len(unbudgeted) == 1 else 'ies'}")
            for row in unbudgeted[:3]:
                label_col, action_col = st.columns([4, 1], vertical_alignment="center")
                label_col.write(f"{row['category']} · {format_currency(row['spent'])}")
                action_col.button("Set budget", key=f"budget_add_{_plan_key(row['category'])}", type="tertiary",
                                  width="stretch", help=f"Create a budget for {row['category']}",
                                  on_click=open_dialog, args=("budget",), kwargs={"category": row["category"]})

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

    if dialog := active_dialog("budget"):
        render_budget_dialog(budgets, transactions, dialog.get("category"))
    elif dialog := active_dialog("remove_budget"):
        render_remove_budget_dialog(dialog["category"])


def _goal_by_id(goal_id):
    # Dialog arguments are frozen for the dialog's reruns; read the saved goal again.
    return next((item for item in repo.get_goals() if item.id == goal_id), None)


@st.dialog("Savings goal", on_dismiss=close_dialog)
def render_goal_dialog(goal_id=None) -> None:
    goal = _goal_by_id(goal_id) if goal_id is not None else None
    suffix = goal_id if goal_id is not None else "new"
    name = st.text_input("Goal name", value=goal.name if goal else "", key=f"goal_name_{suffix}")
    target = st.number_input("Target amount", min_value=1.0, step=100.0, key=f"goal_target_{suffix}",
                             value=float(goal.target_amount) if goal else 1000.0)
    current = st.number_input("Already saved", min_value=0.0, step=50.0, key=f"goal_current_{suffix}",
                              value=float(goal.current_amount) if goal else 0.0)
    has_date = st.checkbox("Set a target date", value=bool(goal and goal.target_date), key=f"goal_has_date_{suffix}")
    target_date = st.date_input("Target date", value=goal.target_date if goal and goal.target_date else date.today(),
                                disabled=not has_date, key=f"goal_date_{suffix}")
    if st.button("Save goal", type="primary", width="stretch"):
        if not name.strip():
            st.error("Enter a name for the goal.")
            return
        try:
            repo.save_goal(SavingsGoal(id=goal.id if goal else None, name=name.strip(), target_amount=target,
                                       current_amount=current, target_date=target_date if has_date else None))
            _plan_saved("Goal saved.")
        except (ValueError, SupabaseError):
            st.error(PLAN_SAVE_ERROR)


@st.dialog("Add funds", on_dismiss=close_dialog)
def render_add_funds_dialog(goal_id) -> None:
    goal = _goal_by_id(goal_id)
    if goal is None:
        st.info("This goal no longer exists.")
        return
    st.write(f"{goal.name}: {format_currency(goal.current_amount)} of {format_currency(goal.target_amount)} saved.")
    amount = st.number_input("Amount to add", min_value=0.01, value=50.0, step=10.0, key=f"goal_funds_amount_{goal_id}")
    if st.button("Add funds", key="goal_add_confirm", type="primary", width="stretch"):
        try:
            repo.save_goal(goal.model_copy(update={"current_amount": goal.current_amount + money(amount)}))
            _plan_saved(f"Added {format_currency(money(amount))} to {goal.name}.")
        except (ValueError, SupabaseError):
            st.error(PLAN_SAVE_ERROR)


@st.dialog("Remove goal", on_dismiss=close_dialog)
def render_remove_goal_dialog(goal_id) -> None:
    goal = _goal_by_id(goal_id)
    if goal is None:
        st.info("This goal no longer exists.")
        return
    st.write(f"Remove {goal.name}? Transaction history is not affected.")
    remove, cancel = st.columns(2)
    if remove.button("Remove goal", type="primary", width="stretch"):
        try:
            repo.delete_goal(goal_id)
            _plan_saved("Goal removed.")
        except SupabaseError:
            st.error(PLAN_SAVE_ERROR)
    if cancel.button("Cancel", width="stretch"):
        close_dialog()
        st.rerun()


def render_goals(transactions) -> None:
    title, action = st.columns([3, 1], vertical_alignment="bottom")
    title.header("Savings goals")
    action.button("New goal", icon=":material/add:", type="primary", width="stretch",
                  on_click=open_dialog, args=("goal",))
    goals = repo.get_goals()
    today = date.today()
    average_savings = AnalyticsService.average_monthly_savings(transactions)
    required_total = Decimal(0)
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
        notes_col, add_col, edit_col, remove_col = st.container(key=f"goal_card_{goal.id}").columns(
            [3, 1, 1, 1], vertical_alignment="center")
        notes_col.markdown(f'<p class="fb-budget-meta">{" · ".join(notes)}</p>', unsafe_allow_html=True)
        add_col.button("Add funds", key=f"goal_add_funds_{goal.id}", type="tertiary", width="stretch",
                       help=f"Add savings to {goal.name}", on_click=open_dialog, args=("add_funds",), kwargs={"id": goal.id})
        edit_col.button("Edit", key=f"goal_edit_{goal.id}", type="tertiary", width="stretch",
                        help=f"Edit {goal.name}", on_click=open_dialog, args=("goal",), kwargs={"id": goal.id})
        remove_col.button("Remove", key=f"goal_remove_{goal.id}", type="tertiary", width="stretch",
                          help=f"Remove {goal.name}", on_click=open_dialog, args=("remove_goal",), kwargs={"id": goal.id})
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

    if dialog := active_dialog("goal"):
        render_goal_dialog(dialog.get("id"))
    elif dialog := active_dialog("add_funds"):
        render_add_funds_dialog(dialog["id"])
    elif dialog := active_dialog("remove_goal"):
        render_remove_goal_dialog(dialog["id"])


def render_plan(transactions) -> None:
    render_page_header("Plan", "Monthly spending limits and savings goals, with what each one needs.")
    view = view_switcher("Plan view", ["Budgets", "Savings goals"], "plan_view")
    if view == "Budgets":
        render_budgets(transactions)
    else:
        render_goals(transactions)


def render_compare():
    render_page_header(
        "Compare",
        "Line up months or quarters of saved history, or upload statements for a one-off look.",
    )
    if view_switcher("Compare source", ["Saved history", "Uploaded files"], "compare_source") == "Saved history":
        render_compare_saved()
    else:
        render_compare_uploads()


def render_compare_saved() -> None:
    if not catalog["count"]:
        st.info("Import a statement or connect a bank to compare saved history.")
        return
    granularity = view_switcher("Period length", ["Month", "Quarter"], "compare_granularity")
    periods = AnalyticsService.history_periods(
        date.fromisoformat(catalog["min_date"]), date.fromisoformat(catalog["max_date"]), granularity
    )
    by_label = {item["label"]: item for item in periods}
    chosen = st.multiselect(
        "Periods to compare", list(by_label), default=list(by_label)[-2:], max_selections=6,
        format_func=lambda label: label + (" (partial)" if by_label[label]["partial"] else ""),
        key=f"compare_periods_{granularity}",
        help="Partial periods are missing days at the start or end of your saved history.",
    )
    if len(chosen) < 2:
        st.info("Choose two to six periods to compare.")
        return
    selected = sorted((by_label[label] for label in chosen), key=lambda item: item["start"])
    # One read for the whole span, bucketed locally, instead of a query per period.
    rows = repo.get_filtered({"start": selected[0]["start"].isoformat(), "end": selected[-1]["end"].isoformat()})
    st.caption("Includes every saved account, regardless of Overview filters.")
    render_comparison([
        {"label": item["label"] + (" (partial)" if item["partial"] else ""), "start": item["start"],
         "transactions": [row for row in rows if item["start"] <= row.date <= item["end"]]}
        for item in selected
    ])


def render_compare_uploads() -> None:
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
        options = statement_format_options(account_type, f"compare_{index}_{upload.name}")
        parsed, metrics = parse_statement(upload, account_name, account_type, **options)
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
    render_comparison(periods)


def render_comparison(periods) -> None:
    periods = sorted(periods, key=lambda item: item["start"])
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
    st.header("Connected banks")
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

    client_user_id = user_id

    items = plaid_repo.get_items()
    if items:
        for item in items:
            with st.container(border=True):
                heading, status = st.columns([3, 1])
                heading.markdown(f"**{item.institution_name}**")
                account_count = len(plaid_repo.get_accounts(item.item_id))
                last_sync = item.last_synced_at.replace("T", " ").replace("+00:00", " UTC") if item.last_synced_at else "Not synced yet"
                heading.caption(f"{account_count} account(s) · Last sync: {last_sync}")
                if item.needs_reconciliation:
                    st.info("Bank history needs to be reconciled after a restore or account replacement. Sync transactions to refresh the full available history.")
                if item.status == "disconnecting":
                    status.warning("Disconnect pending")
                    st.info("Finish this disconnect to confirm bank access has been revoked. The saved removal choice will be used.")
                elif item.status == "error":
                    status.error("Needs attention")
                    st.warning("Reconnect this institution to continue syncing.")
                else:
                    status.success("Connected")

                sync_col, repair_col = st.columns(2)
                if sync_col.button(
                    "Sync transactions",
                    key=f"sync_{item.item_id}",
                    disabled=item.status == "disconnecting",
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
                        if result.get("reconciliation_pending"):
                            st.session_state.plaid_notice = "Plaid is still preparing the full history. Your restored data is kept; sync again when it is ready."
                        elif result.get("reconciled_imports"):
                            st.session_state.plaid_notice += f" Matched {result['reconciled_imports']} previously imported transaction(s)."
                        st.rerun()
                    except Exception as error:
                        st.error(f"Plaid sync failed: {service._friendly_error(error)}")

                if repair_col.button(
                    "Update connection",
                    key=f"repair_{item.item_id}",
                    disabled=item.status == "disconnecting",
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
                        st.error(f"Could not reconnect this bank: {service._friendly_error(error)}")

                with st.expander("Disconnect institution"):
                    remove_transactions = st.checkbox(
                        "Also remove imported transactions from these connected accounts",
                        key=f"remove_tx_{item.item_id}",
                        value=item.disconnect_remove_transactions,
                        disabled=item.status == "disconnecting",
                    )
                    confirmed = st.checkbox(
                        "I understand this revokes FinanceBuddy's Plaid access",
                        key=f"disconnect_confirm_{item.item_id}",
                    )
                    if st.button(
                        "Disconnect",
                        key=f"disconnect_{item.item_id}",
                        disabled=not confirmed,
                        help="Disconnect this bank and remove its saved connection. Delete its transactions only if you select that option above.",
                    ):
                        try:
                            deleted = service.remove_item(item.item_id, item.disconnect_remove_transactions if item.status == "disconnecting" else remove_transactions)
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


def _set_sign_convention(key: str, value: str) -> None:
    st.session_state[key] = value


SIGN_LABELS = {"outflow_negative": "Money out is negative", "outflow_positive": "Money out is positive"}


def render_sign_check(parsed, account_type: str, sign_key: str, convention: str | None) -> None:
    """Summarize directions so a reversed Amount column is caught before saving."""
    evidence = sign_evidence(parsed, account_type)
    counts = (f"{evidence['money_out']} money out ({format_currency(evidence['out_total'])}) · "
              f"{evidence['money_in']} money in ({format_currency(evidence['in_total'])})")
    with st.container(border=True, key="sign_check"):
        if convention is None:
            suggested = "outflow_positive" if evidence["looks_reversed"] else "outflow_negative"
            st.markdown("**This file has one Amount column. How does it show money out?**")
            st.caption(f"If money out is negative: {counts}.")
            if evidence["confirmed"] or evidence["looks_reversed"]:
                st.caption(f"Deposits and payments in this file suggest: {SIGN_LABELS[suggested].lower()}.")
            for column, value in zip(st.columns(2), SIGN_LABELS):
                column.button(SIGN_LABELS[value], key=f"choose_{value}", width="stretch",
                              type="primary" if value == suggested else "secondary",
                              on_click=_set_sign_convention, args=(sign_key, value))
            return
        st.markdown(f"**Money direction:** {counts}")
        if evidence["looks_reversed"]:
            st.warning("Deposits or payments read as money out, so the signs look reversed. Flip them before saving.")
        elif evidence["confirmed"]:
            st.caption("Deposits and payments read as money in, so the signs look right.")
        flipped = "outflow_positive" if convention == "outflow_negative" else "outflow_negative"
        st.button("Flip +/− signs", key="flip_signs", icon=":material/swap_vert:",
                  on_click=_set_sign_convention, args=(sign_key, flipped),
                  help="If you already saved this file with reversed signs, undo that import first. Re-importing with flipped signs adds new copies.")


def render_import_statement() -> None:
    st.header("Import a statement")
    st.caption("Files are uploaded to the FinanceBuddy server for processing. Reviewed transactions are saved to your account only when you confirm. Maximum file size: 10 MB.")
    receipt = st.session_state.get("import_receipt")
    if receipt:
        st.success(f"Saved {receipt['count']} transaction(s) to {receipt['account']}.")
        st.button("Review categories", on_click=navigate_to, args=("Transactions",))
    if st.session_state.get("import_undo_notice"):
        st.success(st.session_state.import_undo_notice)
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
    saved_accounts = catalog["accounts"]
    saved_lookup = {row["id"]: row for row in saved_accounts}
    destination = st.selectbox("Saved account (optional)", [None] + list(saved_lookup),
                               format_func=lambda identifier: "Create or match by name" if identifier is None else f'{saved_lookup[identifier]["name"]} · {saved_lookup[identifier].get("institution_name") or saved_lookup[identifier]["account_type"]} · {identifier[:8]}')
    account_name = st.text_input("Account name", key="import_account_name")
    if destination:
        account_name, account_type = saved_lookup[destination]["name"], saved_lookup[destination]["account_type"]
        st.caption(f"Import destination: {account_name} · {account_type}. USD amounts only.")
    else:
        st.caption("USD amounts only. Select a saved account when account names overlap.")
    st.markdown("#### 2. Add a statement")
    upload = st.file_uploader(
        "CSV, TXT, or text-based PDF",
        type=["csv", "txt", "pdf"],
        key="statement_import",
        help="CSV and TXT files are read as tables. PDFs must contain selectable text; scanned images require OCR and are not supported yet.",
    )
    sign_key = f"statement_import_sign_{account_type}"
    provisional = None
    if upload:
        options = statement_format_options(account_type, "statement_import")
        if options["sign_convention"] is None:
            # Show which way the signs go before asking, instead of rejecting the file.
            provisional, provisional_metrics = parse_statement(
                upload, account_name.strip(), account_type, **{**options, "sign_convention": "outflow_negative"})
            if not provisional_metrics.is_valid or provisional_metrics.direction_columns:
                provisional = None
    if provisional:
        render_sign_check(provisional, account_type, sign_key, None)
    elif upload:
        parsed, metrics = parse_statement(upload, account_name.strip(), account_type, **options)
        parsed = data_service.bind_statement_account(parsed, destination)
        if metrics.is_valid and not metrics.direction_columns:
            render_sign_check(parsed, account_type, sign_key, options["sign_convention"] or "outflow_negative")
        if not metrics.is_valid:
            st.error("This statement could not be imported safely.")
            for error in metrics.errors:
                st.write(f"• {error}")
            st.info("Check the account type and file format, then choose a corrected file. Nothing was saved.")
        else:
            st.markdown("#### 3. Review the preview")
            keep_matching = st.checkbox(
                "Keep matching rows as separate purchases", key="import_keep_matching",
                help="Use for genuinely new identical purchases from another file. Saved rows from this exact file or with the same bank reference are still skipped.",
            )
            conflict = None
            import_plan = None
            try:
                import_plan = repo.preview_statement_import(parsed, keep_matching=keep_matching)
                duplicates = import_plan["matched"]
            except (ValueError, SupabaseError) as error:
                conflict = str(error)
                duplicates = 0
                st.error(conflict)
            start = min(item.date for item in parsed)
            end = max(item.date for item in parsed)
            st.success(
                f"Read {metrics.valid_rows} of {metrics.total_rows} statement rows. Nothing has been saved yet."
            )
            st.caption(
                f"Detected period: {start.strftime('%b %-d, %Y')}–{end.strftime('%b %-d, %Y')} · "
                f"{duplicates} already saved · {metrics.skipped_rows} skipped"
            )
            preview_frame = transaction_frame(parsed[:20])
            new_ids = {item.id for item in import_plan["inserted_rows"]} if import_plan else set()
            destinations = {row["row_number"]: row["transaction_id"] for row in import_plan["mappings"]} if import_plan else {}
            preview_frame["Save action"] = [
                "Review reference" if conflict else "New purchase" if item.id in new_ids
                else "Matched bank transaction" if destinations.get(item.statement_row, "").startswith("plaid:")
                else "Already saved" for item in parsed[:20]
            ]
            st.dataframe(
                preview_frame,
                width="stretch",
                hide_index=True,
                column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
            )
            if len(parsed) > 20:
                st.caption(f"Showing the first 20 of {len(parsed)} transactions in this preview.")
            if metrics.skipped_rows:
                with st.expander(f"Why {metrics.skipped_rows} row(s) were skipped"):
                    for error in metrics.errors:
                        st.write(f"• {error}")
                    if metrics.skipped_rows > len(metrics.errors):
                        st.caption("Only the first few row issues are shown. Export a cleaner CSV if important rows are missing.")
            st.caption("Repeated purchases within this statement stay separate. Append matches overlapping statements by occurrence count, including exact matches from connected banks. Review matches or keep them as separate purchases when they are genuinely new.")
            st.markdown("#### 4. Choose how to save")
            mode = st.radio(
                "How should these transactions be saved?",
                ["Append new transactions", "Replace this account"],
                help="Append adds new transactions. Replace deletes saved transactions for this account and saves this statement in their place.",
            )
            confirmation = st.checkbox(
                "I reviewed the account, statement type, period, and preview"
            )
            if mode == "Replace this account":
                if repo.is_connected_account(account_name.strip(), destination):
                    st.info("This account is connected to a bank. Use Append to reconcile a statement with its saved transactions, or disconnect the bank before replacing its history.")
                    return
                existing_count = sum(row["transactions"] for row in catalog["accounts"] if (row["id"] == destination if destination else row["name"] == account_name.strip()))
                st.warning(f"This replaces {existing_count} saved transaction(s) for “{account_name}” with {len(parsed)} parsed row(s). Replacement cannot be undone. If this account is connected to a bank, its next sync will reconcile the full available bank history.")
                st.download_button(
                    "Download backup before replacing",
                    repo.export_backup(),
                    file_name="financebuddy-before-replacement.json",
                    mime="application/json",
                )
            if st.button(
                "Save statement",
                type="primary",
                disabled=not confirmation or not account_name.strip() or (conflict is not None and mode == "Append new transactions"),
                help="Append only new transactions, or replace history for the named account when that mode is selected.",
            ):
                try:
                    st.session_state.import_save_pending = True
                    if mode == "Append new transactions":
                        result = data_service.import_statement(parsed, keep_matching=keep_matching)
                        inserted = result["inserted"]
                        st.session_state.pop("undo_import_batch_id", None)
                        if result["inserted_rows"]:
                            st.session_state.undo_import_batch_id = result["batch_id"]
                    else:
                        inserted = repo.replace_account(account_name.strip(), parsed)
                        st.session_state.pop("undo_import_batch_id", None)
                    st.session_state.import_save_pending = False
                    st.session_state.pop("import_undo_notice", None)
                    st.session_state.import_receipt = {
                        "count": inserted, "account": account_name.strip(), "mode": mode,
                    }
                    st.rerun()
                except (ValueError, SupabaseError):
                    st.error("The statement save could not be confirmed. Review the preview and retry; the same file will not create extra copies.")
    if st.session_state.get("undo_import_batch_id"):
        if st.button(
            "Undo last import",
            disabled=bool(st.session_state.get("import_save_pending")),
            help="Remove only the new rows from the latest appended statement, if none have changed since import.",
        ):
            try:
                removed = data_service.undo_import(st.session_state.undo_import_batch_id)
                st.session_state.pop("undo_import_batch_id", None)
                st.session_state.pop("import_receipt", None)
                st.session_state.import_undo_notice = f"Removed {removed} imported transaction(s)."
                st.rerun()
            except (ValueError, SupabaseError):
                st.error("This import changed or its response could not be confirmed. Refresh and review the receipt before retrying.")

    with st.expander("Recent import receipts"):
        batches = repo.get_import_batches()
        sample_batches = set(data_service.sample_data().get("batch_ids", []))
        available = [row for row in batches if row['inserted'] and not row['undone'] and str(row['id']) not in sample_batches]
        if available:
            receipt_by_id = {row['id']: row for row in available}
            selected_batch = st.selectbox('Import to undo', list(receipt_by_id), format_func=lambda identifier: f"{receipt_by_id[identifier]['created_at']} · {receipt_by_id[identifier]['inserted']} new transactions")
            if st.button('Undo selected import'):
                try:
                    count = data_service.undo_import(selected_batch)
                    st.session_state.import_undo_notice = f'Removed {count} imported transaction(s).'
                    st.rerun()
                except (ValueError, SupabaseError):
                    st.error('This import has changed or its response could not be confirmed. Refresh and review its transactions before retrying.')
        else:
            st.caption('No imports are available to undo.')


def render_backup_restore(catalog) -> None:
    st.header("Download or restore your data")
    st.caption("Downloaded backups are unencrypted and contain financial records. Store them privately. USD transactions, account metadata, settings, and balance history are included. Bank access credentials are excluded.")
    if st.session_state.get("restore_receipt"):
        st.success(st.session_state.restore_receipt)
    if st.button("Prepare full JSON backup"):
        try:
            st.session_state.prepared_backup = repo.export_backup()
            st.session_state.prepared_backup_at = time.strftime('%Y-%m-%d %H:%M:%S')
        except (ValueError, SupabaseError):
            st.error("Backup preparation failed or the snapshot exceeds restore limits (10 MB, 50,000 records per collection). Your data is still saved.")
    if st.session_state.get("prepared_backup"):
        st.caption(f"Snapshot prepared at {st.session_state.prepared_backup_at}. Prepare again to include later changes.")
        st.download_button("Download full JSON backup", st.session_state.prepared_backup,
                           file_name="financebuddy-backup.json", mime="application/json")
    restore = st.file_uploader("Restore a FinanceBuddy JSON backup", type=["json"])
    if restore:
        try:
            preview = read_backup(restore.getvalue())
            st.warning("Restoring replaces the collections below. Version 3 also restores settings and balance history. Bank connections stay in place. Connected banks will need a full sync to reconcile restored history with the bank's current records.")
            st.dataframe(
                pd.DataFrame([
                    {"Collection": "Transactions", "Saved now": catalog["count"], "In backup": len(preview["transactions"])},
                    {"Collection": "Budgets", "Saved now": len(repo.get_budgets()), "In backup": len(preview["budgets"])},
                    {"Collection": "Savings goals", "Saved now": len(repo.get_goals()), "In backup": len(preview["goals"])},
                    {"Collection": "Category rules", "Saved now": len(repo.get_category_rules()), "In backup": len(preview["category_rules"])},
                ]),
                hide_index=True,
                width="stretch",
            )
            if preview['version'] == 3:
                st.caption(f"Also restores {len(preview['accounts'])} account records, {len(preview['settings'])} settings, and {len(preview['balance_snapshots'])} USD balance snapshots.")
            confirm_restore = st.checkbox("I reviewed the counts and want to restore this backup")
            if st.button(
                "Restore backup",
                disabled=not confirm_restore,
                help="Replace the portable collections in this backup. Version 3 includes settings and balance history; bank connections remain active.",
            ):
                result = data_service.restore(restore.getvalue())
                st.session_state.restore_receipt = (
                    f"Restore complete: {result['transactions']} transactions, "
                    f"{result['budgets']} budgets, {result['goals']} goals, and "
                    f"{result['category_rules']} category rules. Sync connected banks to reconcile their history."
                )
                st.rerun()
        except (ValueError, SupabaseError):
            st.error("The backup could not be restored. Check its format and review your saved data before retrying.")


def render_saved_accounts(catalog) -> None:
    st.header("Saved accounts")
    accounts = {row["id"]: row for row in catalog["accounts"]}
    if not accounts:
        st.info("No saved accounts yet.")
        return
    st.dataframe(pd.DataFrame([{"Account": row['name'], "Type": row['account_type'], "Transactions": row['transactions'],
                                "First date": row['first_date'], "Latest date": row['latest_date'], "Institution": row.get('institution_name') or "Statement", "ID": identifier[:8]} for identifier,row in accounts.items()]),
                 width="stretch", hide_index=True)
    selected_id = st.selectbox("Account to remove", list(accounts), format_func=lambda identifier: f"{accounts[identifier]['name']} · {accounts[identifier].get('institution_name') or accounts[identifier]['account_type']} · {identifier[:8]}")
    selected = accounts[selected_id]
    typed_name = st.text_input(f"Type {selected['name']} to confirm permanent removal", key="delete_account_confirmation")
    if st.button("Remove account", disabled=typed_name != selected['name'],
                 help="Delete this account's saved transactions. An active connection will reconcile its bank history on the next sync."):
        try:
            deleted = repo.delete_account(selected_id)
            st.success(f"Removed {deleted} transaction(s) from {selected['name']}.")
            st.rerun()
        except (ValueError, SupabaseError):
            st.error("The account removal response could not be confirmed. Retry to recover its saved result.")


def render_category_rules() -> None:
    st.header("Custom merchant rules")
    rules = repo.get_category_rules()
    if rules:
        st.dataframe(
            pd.DataFrame([rule.model_dump(exclude={"version"}) for rule in rules]).rename(
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


def render_accounts(catalog) -> None:
    render_page_header("Accounts", "Connect a bank, import a statement, or review saved accounts.")
    view = view_switcher(
        "Accounts view", ["Bank connections", "Import statement", "Saved accounts"], "accounts_view"
    )
    if view == "Bank connections":
        render_bank_connections()
    elif view == "Import statement":
        render_import_statement()
    else:
        render_saved_accounts(catalog)


def render_settings(catalog) -> None:
    render_page_header("Settings", "Back up or restore your data, and review category rules.")
    view = view_switcher("Settings view", ["Backup & restore", "Category rules"], "settings_view")
    if view == "Backup & restore":
        render_backup_restore(catalog)
    else:
        render_category_rules()


def clear_category_filter() -> None:
    st.session_state.dashboard_categories = []


def select_all_categories(category_options: list[str]) -> None:
    st.session_state.dashboard_categories = category_options


def _keep_valid_selection(key: str, options: list[str]) -> list[str]:
    """Keep an explicit subset, while letting an All selection follow new data."""
    previous_options = st.session_state.get(f"_{key}_options")
    selected = st.session_state.get(key)
    if not isinstance(selected, list) or (
        previous_options is not None and set(selected) == set(previous_options)
    ):
        st.session_state[key] = list(options)
    else:
        st.session_state[key] = [value for value in selected if value in options]
    st.session_state[f"_{key}_options"] = list(options)
    return st.session_state[key]


def _selection_label(noun: str, selected: list[str], options: list[str]) -> str:
    if set(selected) == set(options):
        return f"All {noun}"
    if not selected:
        return f"No {noun}"
    if len(selected) == 1:
        return selected[0]
    return f"{len(selected)} of {len(options)} {noun}"


def _chips_html(chips: list[str]) -> str:
    return '<div class="fb-chips">' + "".join(f'<span class="fb-chip">{html.escape(chip)}</span>' for chip in chips) + "</div>"


def render_filter_bar(catalog) -> dict:
    """The compact filter bar shared by Overview and Transactions."""
    min_date = date.fromisoformat(catalog["min_date"])
    max_date = date.fromisoformat(catalog["max_date"])
    account_labels = {row["id"]: (row["name"] if sum(other["name"] == row["name"] for other in catalog["accounts"]) == 1 else f'{row["name"]} · {row.get("institution_name") or row["account_type"]} · {row["id"][:8]}') for row in catalog["accounts"]}
    account_options = sorted(account_labels.values())
    category_options = catalog["categories"]

    preset_options = DATE_PRESETS[1:-1] + ["All time", "Custom"]

    def preset_format(value: str) -> str:
        return AnalyticsService.preset_label(value, max_date, date.today())

    # Labels name stale data's dates, so the browser can return an older label.
    st.session_state.setdefault("dashboard_date_preset", "Last 3 months")
    keep_choice("dashboard_date_preset", preset_options, preset_format)
    stored_dates = st.session_state.get("dashboard_dates")
    # Keep deliberate empty ranges; repair malformed or reversed dates only.
    if (
        not isinstance(stored_dates, (tuple, list))
        or len(stored_dates) != 2
        or any(type(value) is not date for value in stored_dates)
        or stored_dates[0] > stored_dates[1]
    ):
        st.session_state.dashboard_dates = AnalyticsService.preset_range(st.session_state.dashboard_date_preset, min_date, max_date)
    st.session_state.setdefault("dashboard_type", "Both")
    st.session_state.setdefault("dashboard_minimum_amount", 0.0)
    st.session_state.setdefault("dashboard_include_transfers", False)
    selected_accounts = _keep_valid_selection("dashboard_accounts", account_options)
    selected_categories = _keep_valid_selection("dashboard_categories", category_options)
    extra_count = sum(
        (
            st.session_state.dashboard_type != "Both",
            bool(st.session_state.dashboard_minimum_amount),
            bool(st.session_state.dashboard_include_transfers),
            bool(st.session_state.get("dashboard_tag", "").strip()),
        )
    )

    overview_filters = st.session_state.get("nav_page", "Overview") == "Overview"
    with st.container(key="fbcard_filters"):
        if overview_filters:
            st.selectbox(
                "Date range", preset_options,
                key="dashboard_date_preset", format_func=preset_format,
                on_change=apply_date_preset, args=(min_date, max_date),
                label_visibility="collapsed", help="Ranges count back from your most recent transaction.",
            )
        else:
            range_col, search_col = st.container(key="filter_search_row").columns([2.6, 1], vertical_alignment="center")
            with range_col:
                st.selectbox(
                    "Date range", preset_options,
                    key="dashboard_date_preset", format_func=preset_format,
                    on_change=apply_date_preset, args=(min_date, max_date),
                    label_visibility="collapsed", help="Ranges count back from your most recent transaction.",
                )
            with search_col:
                search = st.text_input(
                    "Merchant search", placeholder="Search merchants, e.g. Costco",
                    key="dashboard_search", icon=":material/search:", label_visibility="collapsed",
                )
        if st.session_state.dashboard_date_preset == "Custom":
            st.date_input(
                "Custom dates",
                value=tuple(st.session_state.dashboard_dates),
                min_value=min(min_date, st.session_state.dashboard_dates[0]),
                max_value=max(max_date, st.session_state.dashboard_dates[1]),
                key="dashboard_custom_dates",
                on_change=apply_custom_dates,
            )

        active_count = extra_count + sum((
            set(selected_accounts) != set(account_options),
            set(selected_categories) != set(category_options),
            bool(st.session_state.get("dashboard_search", "").strip()),
        ))
        # Active filters stay visible while the panel is closed; filled once widgets have run.
        active_chips_slot = st.container(key="active_filter_chips")
        filter_panel = st.expander("Filters" + (f" ({active_count} active)" if active_count else ""))
        with filter_panel:
            if overview_filters:
                search = st.text_input(
                    "Merchant search", placeholder="Search merchants, e.g. Costco",
                    key="dashboard_search", icon=":material/search:",
                )
            accounts_col, categories_col, more_col = st.container(key="filter_buttons").columns(3)
            with accounts_col.popover(
                _selection_label("accounts", selected_accounts, account_options),
                icon=":material/account_balance:",
                width="stretch",
            ):
                all_col, none_col = st.columns(2)
                all_col.button("Select all", key="accounts_all", on_click=select_all_accounts,
                               args=(account_options,), width="stretch")
                none_col.button("Clear", key="accounts_none", on_click=clear_account_filter, width="stretch")
                selected_accounts = st.multiselect("Accounts", account_options, key="dashboard_accounts")
            with categories_col.popover(
                _selection_label("categories", selected_categories, category_options),
                icon=":material/category:",
                width="stretch",
            ):
                all_col, none_col = st.columns(2)
                all_col.button("Select all", key="categories_all", on_click=select_all_categories,
                               args=(category_options,), width="stretch")
                none_col.button("Clear", key="categories_none", on_click=clear_category_filter, width="stretch")
                selected_categories = st.multiselect("Categories", category_options, key="dashboard_categories")
            with more_col.popover(
                "More filters" + (f" ({extra_count})" if extra_count else ""),
                icon=":material/tune:",
                width="stretch",
            ):
                transaction_type = st.segmented_control(
                    "Money in or out", list(TRANSACTION_TYPES), key="dashboard_type", required=True
                )
                minimum_amount = st.number_input(
                    "Minimum amount", min_value=0.0, step=10.0, key="dashboard_minimum_amount"
                )
                tag = st.text_input("Tag", key="dashboard_tag", placeholder="e.g. japan-trip").strip().lower()
                include_transfers = st.toggle(
                    "Include transfers & card payments",
                    key="dashboard_include_transfers",
                    help=(
                        "Off by default: money moved between your own accounts, including credit-card "
                        "payments, is left out of income and spending so purchases aren't counted twice."
                    ),
                )
                st.caption("Filters apply to Overview and Transactions. Budgets in Plan always use all spending.")

            start_date, end_date = st.session_state.dashboard_dates
            query_filters = {"start": start_date.isoformat(), "end": end_date.isoformat(),
                             "accounts": [identifier for identifier, label in account_labels.items() if label in selected_accounts],
                             "categories": selected_categories, "type": transaction_type,
                             "search": search.strip(), "minimum": str(money(minimum_amount))}
            if tag:
                query_filters["tags"] = [tag]
            if overview_filters:
                filtered = repo.get_filtered(query_filters)
                filtered_count = len(filtered)
            else:
                query_filters["view"] = st.session_state.get("tx_view", "All")
                query_key = json.dumps(query_filters, sort_keys=True)
                if st.session_state.get("transaction_query") != query_key:
                    st.session_state.transaction_query = query_key
                    st.session_state.transaction_cursors = [None]
                page_data = repo.get_page(query_filters, after=st.session_state.transaction_cursors[-1])
                filtered, filtered_count = page_data["rows"], page_data["count"]
                st.session_state.transaction_next_cursor = page_data["next_cursor"]
                st.session_state.transaction_filters = query_filters

            chips = []
            if set(selected_accounts) != set(account_options):
                chips.append("Accounts: " + _selection_label("accounts", selected_accounts, account_options))
            if set(selected_categories) != set(category_options):
                chips.append("Categories: " + _selection_label("categories", selected_categories, category_options))
            if transaction_type != "Both":
                chips.append(transaction_type)
            if search.strip():
                chips.append(f"Merchant: {search.strip()}")
            if minimum_amount:
                chips.append(f"At least {format_currency(minimum_amount)}")
            if tag:
                chips.append(f"Tag: {tag}")
            if include_transfers:
                chips.append("Counting transfers & card payments")
            date_chip = f"{start_date.strftime('%b %-d, %Y')} – {end_date.strftime('%b %-d, %Y')}"
            count_col, chips_col = st.columns([1.5, 2.5], vertical_alignment="center")
            count_col.caption(f"Showing {filtered_count:,} of {catalog["display_count"]:,} saved transactions")
            chips_col.markdown(_chips_html([date_chip] + ([] if chips else ["Transfers & card payments not counted"])),
                               unsafe_allow_html=True)
        if chips:
            with active_chips_slot:
                chips_col, reset_col = st.columns([4, 1], vertical_alignment="center")
                chips_col.markdown(_chips_html(chips), unsafe_allow_html=True)
                reset_col.button(
                    "Reset filters",
                    key="reset_filters",
                    icon=":material/restart_alt:",
                    type="tertiary",
                    width="stretch",
                    on_click=reset_dashboard_filters,
                    help="Show every date, account, and category again and clear search and amount filters.",
                )
    if not overview_filters:
        st.caption(f"Showing {filtered_count:,} of {catalog["display_count"]:,} saved transactions")
    previous_range = AnalyticsService.previous_period(start_date, end_date)
    history_start = max(min_date, max_date - timedelta(days=366))
    history_filters = {**query_filters, "start": history_start.isoformat(), "end": max_date.isoformat()}
    history = repo.get_filtered(history_filters) if overview_filters and st.session_state.get("overview_view", "Summary") in ("Summary", "Recurring & income", "Balances") else []
    if overview_filters:
        st.caption("Recurring charges, income cadence, and forecasts use up to 12 months of matching history.")
    return {
        "filtered": filtered, "start_date": start_date, "end_date": end_date,
        "previous_range": previous_range,
        "previous_transactions": repo.get_filtered({**query_filters, "start": previous_range[0].isoformat(), "end": previous_range[1].isoformat()}) if overview_filters else [],
        "include_transfers": include_transfers, "all_transactions": history, "history_transactions": history,
        "summary": repo.get_summary({**query_filters, "include_transfers": include_transfers}) if overview_filters else None,
    }


PAGES = ["Overview", "Transactions", "Plan", "Accounts", "Compare", "Settings"]
PAGE_INTROS = {
    "Overview": "Your spending at a glance.",
    "Transactions": "Search, review, and correct individual activity.",
}

try:
    catalog = repo.get_catalog()
except SupabaseError:
    st.error("Couldn't load your transactions. Check your connection and try again.")
    if st.button("Try again", key="retry_transactions"):
        st.rerun()
    st.stop()
needs_review = catalog["needs_review"]
user_metadata = user.get("user_metadata") or {}
account_label = str(
    user_metadata.get("full_name")
    or user_metadata.get("name")
    or user.get("email")
    or "Your account"
)

with st.sidebar:
    st.markdown(
        '<div class="fb-brand"><span class="fb-mark" aria-hidden="true">F</span>'
        '<span class="fb-brand-name">FinanceBuddy</span></div>',
        unsafe_allow_html=True,
    )
    with st.container(key="nav_menu"):
        def menu_label(name: str) -> str:
            return f"{name} ({needs_review} to review)" if name == "Transactions" and needs_review else name

        keep_choice("nav_page", PAGES, menu_label)
        page = st.radio(
            "Menu",
            PAGES,
            key="nav_page",
            label_visibility="collapsed",
            format_func=menu_label,
            on_change=close_dialog,
        )
    st.markdown('<p class="fb-menu-label">Add data</p>', unsafe_allow_html=True)
    menu_actions = st.container(key="menu_actions")
    menu_actions.button(
        "Connect a bank",
        key="menu_connect_bank",
        icon=":material/add_link:",
        type="tertiary",
        on_click=navigate_to,
        args=("Accounts", "Bank connections"),
    )
    menu_actions.button(
        "Upload a statement",
        key="menu_upload_statement",
        icon=":material/upload_file:",
        type="tertiary",
        on_click=navigate_to,
        args=("Accounts", "Import statement"),
    )
    st.divider()
    if st.button(
        "How it works",
        key="menu_guide",
        icon=":material/help:",
        type="tertiary",
        help="Open the getting-started guide and a short explanation of the main workflow.",
    ):
        st.session_state.show_onboarding = True
    st.caption(f"Signed in as {account_label}")
    if st.button(
        "Log out",
        icon=":material/logout:",
        help="End this browser's FinanceBuddy login session.",
    ):
        try:
            auth.sign_out(session["access_token"])
        except SupabaseError:
            pass
        _clear_supabase_session()
        st.rerun()
    st.caption("Light or dark follows your device. Switch it anytime under ⋮ → Settings.")

show_onboarding = (
    repo.get_setting("onboarding_complete", "false") != "true"
    or st.session_state.get("show_onboarding", False)
)
if show_onboarding:
    render_onboarding()
render_sample_banner()

if page in ("Overview", "Transactions"):
    render_page_header(page, PAGE_INTROS[page])
    if not catalog["count"]:
        render_start_actions()
    else:
        try:
            overview_view = render_filter_bar(catalog)
            if page == "Overview":
                render_overview(overview_view["filtered"], overview_view)
            else:
                render_transactions(overview_view["filtered"])
        except SupabaseError:
            st.error("Couldn't load the selected transactions. Check your connection and try again.")
            if st.button("Try again", key="retry_filtered_transactions"):
                st.rerun()

elif page == "Plan":
    plan_end = date.fromisoformat(catalog["max_date"]) if catalog["max_date"] else date.today()
    render_plan(repo.get_filtered({"start": (plan_end - timedelta(days=366)).isoformat(), "end": plan_end.isoformat()}))
elif page == "Compare":
    render_compare()
elif page == "Accounts":
    render_accounts(catalog)
else:
    render_settings(catalog)
