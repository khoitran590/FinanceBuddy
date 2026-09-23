from __future__ import annotations

import json
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
from src.services.categorizer import CATEGORIES, categorize_with_custom_rules
from src.services.parser import BankStatementParser
from src.services.plaid_service import PlaidConfig, PlaidConfigurationError, PlaidService
from src.services.production import validate_production_configuration
from src.services.supabase import (
    SupabaseAuth,
    SupabaseConfig,
    SupabaseDataClient,
    SupabaseError,
    has_verified_email,
    normalized_session,
)
from src.ui.charts import (
    make_category_donut_chart,
    make_category_heatmap,
    make_comparison_category_chart,
    make_comparison_delta_chart,
    make_comparison_summary_chart,
    make_monthly_bar_chart,
    make_profit_loss_line_chart,
)
from src.ui.components import (
    format_currency,
    inject_app_styles,
    period_label,
    render_feature_intro,
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
    st.session_state.pop("supabase_session", None)


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
    st.title("💸 FinanceBuddy")
    st.subheader("Your finances stay private to your account")
    st.write(
        "Log in or create an account with Supabase Auth. FinanceBuddy never stores your password, "
        "and PostgreSQL row-level security keeps every financial record tied to your account."
    )
    if st.session_state.pop("auth_link_error", None):
        st.error("This email link could not be used. Request a fresh email and try again.")
    if st.session_state.pop("unverified_email_error", None):
        st.warning("Verify your email address before accessing financial data.")
    recovery_link_session = st.session_state.get("recovery_link_session")
    if recovery_link_session:
        st.info("Email verified. Choose a new password to finish recovering your account.")
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
    login_tab, signup_tab, recovery_tab = st.tabs(
        ["Log in", "Create account", "Reset password"]
    )
    with login_tab:
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

    with signup_tab:
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
            resend_email = st.text_input("Account email", key="resend_email")
            if st.button("Resend verification email", width="stretch"):
                try:
                    auth.resend_signup_email(resend_email.strip().lower())
                    st.success(
                        "If this account still needs verification, check for a new email. "
                        "Already verified? Use Log in instead."
                    )
                except SupabaseError as error:
                    st.error(str(error))

    with recovery_tab:
        st.caption("Request a recovery code, then enter the code and a new password below.")
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
                reset_submitted = st.form_submit_button(
                    "Set new password", type="primary", width="stretch"
                )
            if reset_submitted:
                if len(new_password) < 12:
                    st.error("Use at least 12 characters for your password.")
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
        "Authentication is provided by Supabase. Session tokens stay in this server-side Streamlit session."
    )


_consume_auth_link()
session = st.session_state.get("supabase_session")
user = None
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

data_client = SupabaseDataClient(supabase_config, session["access_token"])
repo = SupabaseTransactionRepository(data_client, user_id)
plaid_repo = SupabasePlaidRepository(data_client, user_id)


def apply_custom_rules(transactions):
    rules = repo.get_category_rules()
    for transaction in transactions:
        transaction.category = categorize_with_custom_rules(transaction.description, rules)
    return transactions


def parse_statement(upload, account_name: str, account_type: str):
    parsed, metrics = BankStatementParser.parse_file(
        upload.getvalue(), upload.name, account_name, account_type
    )
    return apply_custom_rules(parsed), metrics


def sync_import_account_name() -> None:
    defaults = {"Checking": "Primary Checking", "Credit Card": "Primary Credit Card"}
    current = st.session_state.get("import_account_name", "")
    if current in defaults.values() or not current.strip():
        st.session_state.import_account_name = defaults[st.session_state.import_account_type]


def clear_account_filter() -> None:
    st.session_state.dashboard_accounts = []


def select_all_accounts(account_options: list[str]) -> None:
    st.session_state.dashboard_accounts = account_options


def reset_dashboard_filters() -> None:
    for key in (
        "dashboard_dates",
        "dashboard_accounts",
        "dashboard_categories",
        "dashboard_transaction_types",
        "dashboard_search",
        "dashboard_minimum_amount",
    ):
        st.session_state.pop(key, None)


def render_onboarding() -> None:
    with st.container(border=True):
        heading, close = st.columns([5, 1])
        heading.subheader("Welcome to FinanceBuddy 👋")
        heading.caption("Your financial data is isolated in the database assigned to your signed-in account.")
        if close.button(
            "Hide guide",
            help="Dismiss this guide. You can reopen it anytime with “How FinanceBuddy works.”",
            width="stretch",
        ):
            repo.set_setting("onboarding_complete", "true")
            st.session_state.show_onboarding = False
            st.rerun()

        first, second, third, fourth = st.columns(4)
        first.markdown("**1 · Import**")
        first.caption("Open **Import & data**, choose the account type, then preview a CSV or PDF before saving it.")
        second.markdown("**2 · Review**")
        second.caption("Use **Transactions** to correct categories, teach merchant rules, or split a purchase.")
        third.markdown("**3 · Understand**")
        third.caption("Use **Overview** and the sidebar filters to explore cash flow, trends, and unusual expenses.")
        fourth.markdown("**4 · Plan**")
        fourth.caption("Create limits and targets in **Budgets & goals**, or compare statement periods.")
        st.info(
            "Best first step: import one statement with **Append new transactions**. "
            "Exact duplicates are ignored, and nothing is saved until you confirm the preview."
        )


def render_overview(transactions):
    render_feature_intro(
        "Overview",
        "See account-appropriate totals, monthly cash flow, category spending, recurring charges, unusual expenses, and a simple next-month estimate. Sidebar filters update everything on this page.",
    )
    if not transactions:
        st.info("No transactions match the current filters. Adjust the filters in the sidebar.")
        return

    st.markdown(f'<p class="fb-period">{period_label(transactions)}</p>', unsafe_allow_html=True)
    render_metrics(AnalyticsService.dashboard_metrics(transactions))

    monthly = AnalyticsService.monthly_breakdown(transactions)
    categories = AnalyticsService.category_summary(transactions)
    left, right = st.columns(2)
    with left:
        st.plotly_chart(make_monthly_bar_chart(monthly), width="stretch")
    with right:
        st.plotly_chart(
            make_category_donut_chart(
                {item["category"]: item["amount"] for item in categories}
            ),
            width="stretch",
        )

    st.plotly_chart(
        make_profit_loss_line_chart(AnalyticsService.cumulative_cash_flow(transactions)),
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
            "Review them in the Transactions tab."
        )

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
    changes = AnalyticsService.category_changes(transactions)
    if changes:
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
        st.caption("Import at least two months of activity to see category changes.")

    recurring = AnalyticsService.recurring_expenses(transactions)
    unusual = AnalyticsService.unusual_expenses(transactions)
    forecast = AnalyticsService.cash_flow_forecast(transactions)
    first, second, third = st.columns(3)
    with first:
        st.markdown("#### Recurring charges")
        if recurring:
            for item in recurring[:5]:
                st.write(f"{item['merchant']} · {format_currency(item['amount'])}")
        else:
            st.caption("No repeated merchant-and-amount pattern across multiple months yet.")
    with second:
        st.markdown("#### Unusual expenses")
        if unusual:
            for transaction in unusual:
                st.write(f"{transaction.description} · {format_currency(abs(transaction.amount))}")
        else:
            st.caption("No unusually large expenses detected in this selection.")
    with third:
        st.markdown("#### Next-month estimate")
        if forecast["months"]:
            st.metric("Estimated money out", format_currency(forecast["outflow"]))
            st.caption(f"Based on the latest {forecast['months']} month(s); this is a simple average.")
        else:
            st.caption("More history is needed for an estimate.")

    month_count = len({item.date.strftime("%Y-%m") for item in transactions})
    with st.expander("Advanced monthly category trend", expanded=month_count >= 3):
        if month_count >= 2:
            st.plotly_chart(
                make_category_heatmap(AnalyticsService.monthly_category_expenses(transactions)),
                width="stretch",
            )
        else:
            st.caption("Import at least two months to view this trend.")


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
                repo.split_transaction(
                    split_id, category_one, amount_one, category_two, amount_two
                )
                st.success("Transaction split successfully.")
                st.rerun()
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


def render_budgets_and_goals(transactions):
    render_feature_intro(
        "Budgets & goals",
        "Budgets compare monthly category spending with a limit. Savings goals track progress toward a target amount and optional date.",
    )
    budget_tab, goal_tab = st.tabs(["Budgets", "Savings goals"])
    with budget_tab:
        st.subheader("Monthly budgets")
        budgets = repo.get_budgets()
        months = sorted({item.date.strftime("%Y-%m") for item in transactions})
        selected_month = st.selectbox("Budget month", months, index=len(months) - 1) if months else None
        month_transactions = [
            item for item in transactions if selected_month and item.date.strftime("%Y-%m") == selected_month
        ]
        spending = AnalyticsService.category_expenses(month_transactions)
        if budgets:
            for budget in budgets:
                actual = spending.get(budget.category, 0.0)
                render_progress(budget.category, actual, budget.monthly_limit)
                if actual > budget.monthly_limit:
                    st.error(f"Over budget by {format_currency(actual - budget.monthly_limit)}")
        else:
            st.info("Create a category budget to start tracking monthly limits.")

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

    with goal_tab:
        st.subheader("Savings goals")
        goals = repo.get_goals()
        for goal in goals:
            label = goal.name
            if goal.target_date:
                label += f" · target {goal.target_date.strftime('%b %-d, %Y')}"
            render_progress(label, goal.current_amount, goal.target_amount)
        if not goals:
            st.info("Create a goal for an emergency fund, trip, or major purchase.")

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
                    st.warning(item.error_message or "Reconnect this institution to continue syncing.")
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


def render_import_and_data(all_transactions):
    render_feature_intro(
        "Import & data",
        "Connect a bank with Plaid or add statement activity, create or restore a portable backup, manage saved accounts, and review custom merchant-category rules.",
    )
    bank_tab, import_tab, backup_tab, account_tab, rule_tab = st.tabs(
        ["Bank connections", "Import statement", "Backup & restore", "Accounts", "Category rules"]
    )
    with bank_tab:
        render_bank_connections()

    with import_tab:
        st.subheader("Import a statement")
        st.caption("Files stay local. Review parsed rows before saving anything.")
        if "import_account_type" not in st.session_state:
            st.session_state.import_account_type = "Checking"
        if "import_account_name" not in st.session_state:
            st.session_state.import_account_name = "Primary Checking"
        account_type = st.selectbox(
            "Statement type",
            ["Checking", "Credit Card"],
            key="import_account_type",
            on_change=sync_import_account_name,
            help="Checking and credit-card statements use different sign conventions and dashboard metrics.",
        )
        account_name = st.text_input("Account name", key="import_account_name")
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
            else:
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
                mode = st.radio(
                    "Save behavior",
                    ["Append new transactions", "Replace this account"],
                    help="Append ignores exact duplicates. Replace only clears history for the named account.",
                )
                confirmation = st.checkbox(
                    "I reviewed the account, statement type, period, and preview"
                )
                if mode == "Replace this account":
                    st.warning(f"This will replace saved history for “{account_name}” only.")
                if st.button(
                    "Save statement",
                    type="primary",
                    disabled=not confirmation or not account_name.strip(),
                    help="Append only new transactions, or replace history for the named account when that mode is selected.",
                ):
                    st.session_state.undo_transactions = repo.get_all()
                    if mode == "Append new transactions":
                        inserted = repo.insert_many(parsed)
                    else:
                        inserted = repo.replace_account(account_name.strip(), parsed)
                    st.session_state.import_success = f"Saved {inserted} new transaction(s)."
                    st.rerun()
        if "import_success" in st.session_state:
            st.success(st.session_state.pop("import_success"))
        if st.session_state.get("undo_transactions") is not None:
            if st.button(
                "Undo last import",
                help="Restore the transaction history captured immediately before the most recent import in this browser session.",
            ):
                repo.replace_all(st.session_state.pop("undo_transactions"))
                st.success("The previous transaction history was restored.")
                st.rerun()

    with backup_tab:
        st.subheader("Portable local backup")
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
                preview = json.loads(restore.getvalue().decode("utf-8"))
                st.write(
                    f"Backup contains {len(preview.get('transactions', []))} transactions, "
                    f"{len(preview.get('budgets', []))} budgets, and {len(preview.get('goals', []))} goals."
                )
                confirm_restore = st.checkbox("I understand restore replaces all current FinanceBuddy data")
                if st.button(
                    "Restore backup",
                    disabled=not confirm_restore,
                    help="Replace all current FinanceBuddy data with the contents of this validated backup.",
                ):
                    result = repo.restore_backup(restore.getvalue())
                    st.success(f"Restored {result['transactions']} transactions.")
                    st.rerun()
            except (ValueError, json.JSONDecodeError) as error:
                st.error(f"Invalid backup: {error}")

    with account_tab:
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

    with rule_tab:
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

all_transactions = repo.get_all()
if all_transactions:
    with st.sidebar:
        st.header("Dashboard filters")
        st.caption("These controls update the Overview, Transactions, and budget calculations together.")
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
        selected_dates = st.date_input(
            "Date range",
            value=(min_date, max_date),
            min_value=min_date,
            max_value=max_date,
            key="dashboard_dates",
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
    filtered_transactions = AnalyticsService.filter_transactions(
        all_transactions,
        start_date,
        end_date,
        selected_accounts,
        selected_categories,
        selected_types,
        search,
        minimum_amount,
    )
else:
    filtered_transactions = []

overview_tab, transactions_tab, budgets_tab, compare_tab, import_tab = st.tabs(
    ["Overview", "Transactions", "Budgets & goals", "Compare", "Import & data"]
)

with overview_tab:
    if not all_transactions:
        st.info("No transaction data yet. Open Import & data to preview and save a statement.")
    else:
        render_overview(filtered_transactions)

with transactions_tab:
    render_transactions(filtered_transactions)

with budgets_tab:
    render_budgets_and_goals(filtered_transactions)

with compare_tab:
    render_compare()

with import_tab:
    render_import_and_data(all_transactions)
