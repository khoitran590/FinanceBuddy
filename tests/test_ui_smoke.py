from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_app_stops_at_authentication_boundary_without_exceptions():
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(app_path).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "💸 FinanceBuddy"
    messages = [item.value for item in app.error] + [item.value for item in app.header]
    assert any(
        expected in message
        for expected in (
            "Account security must be configured",
            "Supabase must be configured",
            "Your finances stay private to your account",
        )
        for message in messages
    )


def test_transaction_ui_edits_allocations_and_resplits_the_source(tmp_path, monkeypatch):
    import time
    from datetime import date
    from src.domain.models import Transaction
    from src.repositories.transaction_repo import TransactionRepository
    import src.repositories.supabase_repo as cloud_repos
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    repo = TransactionRepository(str(tmp_path / "allocations-ui.db"))
    source = Transaction(id="plaid:source", date=date(2026, 9, 1), description="Store", amount=-100,
                         category="Shopping", account_name="Card")
    repo.insert_many([source])
    repo.split_transaction(source.id, "Groceries", 60, "Shopping", 40)
    repo.set_setting("onboarding_complete", "true")
    monkeypatch.setenv("FINANCEBUDDY_ENV", "development")
    monkeypatch.setattr(SupabaseConfig, "from_sources", classmethod(lambda cls, secrets: SupabaseConfig("https://test.supabase.co", "test")))
    monkeypatch.setattr(SupabaseAuth, "get_user", lambda self, token: {"id": "alice", "email": "alice@example.com", "email_confirmed_at": "2026-09-01"})
    monkeypatch.setattr(cloud_repos, "SupabaseTransactionRepository", lambda client, user_id: repo)
    monkeypatch.setattr(PlaidConfig, "from_sources", classmethod(lambda cls, secrets: PlaidConfig("", "")))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["supabase_session"] = {"access_token": "test", "refresh_token": "test", "expires_at": time.time() + 3600}
    app.session_state["session_owner"] = "alice"
    app.session_state["nav_page"] = "Transactions"
    app.run(timeout=20)
    assert not app.exception
    next(button for button in app.button if button.label == "Edit selected transaction").click().run()
    assert not app.exception
    assert app.session_state["active_dialog"]["id"].startswith("allocation:")
    # Rule options start unchecked even though the phrase is pre-filled.
    assert next(box for box in app.text_input if box.label == "Merchant name or phrase").value == "store"
    next(box for box in app.selectbox if box.label == "Category").set_value("Travel")
    next(button for button in app.button if button.label == "Save category").click().run()
    assert not app.exception
    assert "Travel" in {row.category for row in repo.get_all()}
    assert repo.get_canonical_all()[0].category == "Shopping"
    repo.upsert_many([source.model_copy(update={"amount": -110})])
    app.run()
    assert not app.exception
    assert any("changed the amount of a split" in item.value for item in app.warning)

    def split_first_row():
        next(button for button in app.button if button.label == "Edit selected transaction").click().run()
        app.segmented_control(key="transaction_editor_tab").set_value("Split").run()
        assert not app.exception
        next(button for button in app.button if button.label == "Split transaction").click().run()
        assert not app.exception
        assert "active_dialog" not in app.session_state

    split_first_row()
    assert len(repo.get_canonical_all()) == 1
    assert len(repo.get_all()) == 2
    assert sum(item.amount for item in repo.get_all()) == -110
    assert not any(item.split_needs_review for item in repo.get_all())
    # Re-splitting from one part replaces both parts of the source purchase.
    split_first_row()
    assert len(repo.get_canonical_all()) == 1 and sum(item.amount for item in repo.get_all()) == -110
    # A provider reversal can change direction; the existing split remains
    # reviewable even though the source is now an inflow.
    repo.upsert_many([source.model_copy(update={"amount": 110})])
    app.run()
    assert not app.exception
    split_first_row()
    assert not app.exception
    assert sum(item.amount for item in repo.get_all()) == 110
    assert not any(item.split_needs_review for item in repo.get_all())


def test_statement_ui_previews_repeated_rows_saves_retries_and_undoes(tmp_path, monkeypatch):
    import time
    from io import BytesIO
    import streamlit as st
    import src.repositories.supabase_repo as cloud_repos
    from src.repositories.transaction_repo import TransactionRepository
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    repo = TransactionRepository(str(tmp_path / "imports-ui.db"))
    repo.set_setting("onboarding_complete", "true")
    upload = BytesIO(b"Date,Description,Amount\n2026-10-01,Coffee,-4.50\n2026-10-01,Coffee,-4.50\n")
    upload.name = "checking.csv"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload if kwargs.get("key") == "statement_import" else None)
    monkeypatch.setenv("FINANCEBUDDY_ENV", "development")
    monkeypatch.setattr(SupabaseConfig, "from_sources", classmethod(lambda cls, secrets: SupabaseConfig("https://test.supabase.co", "test")))
    monkeypatch.setattr(SupabaseAuth, "get_user", lambda self, token: {"id": "alice", "email": "alice@example.com", "email_confirmed_at": "2026-09-01"})
    monkeypatch.setattr(cloud_repos, "SupabaseTransactionRepository", lambda client, user_id: repo)
    monkeypatch.setattr(PlaidConfig, "from_sources", classmethod(lambda cls, secrets: PlaidConfig("", "")))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["supabase_session"] = {"access_token": "test", "refresh_token": "test", "expires_at": time.time() + 3600}
    app.session_state["session_owner"] = "alice"
    app.session_state["nav_page"] = "Accounts"
    app.session_state["accounts_view"] = "Import statement"
    app.run(timeout=20)
    assert not app.exception
    assert app.dataframe[0].value["Save action"].tolist() == ["New purchase", "New purchase"]
    next(box for box in app.checkbox if box.label.startswith("I reviewed the account")).set_value(True).run()
    next(button for button in app.button if button.label == "Save statement").click().run()
    assert not app.exception
    assert len(repo.get_all()) == 2 and sum(row.amount for row in repo.get_all()) == -9
    assert app.dataframe[0].value["Save action"].tolist() == ["Already saved", "Already saved"]
    next(button for button in app.button if button.label == "Undo last import").click().run()
    assert not app.exception and repo.get_all() == []
    assert any("Removed 2 imported" in item.value for item in app.success)
    next(button for button in app.button if button.label == "Save statement").click().run()
    next(button for button in app.button if button.label == "Save statement").click().run()
    assert not app.exception and len(repo.get_all()) == 2


def test_plaid_link_component_mounts_without_invalid_defaults():
    app = AppTest.from_string(
        """
import importlib
import src.ui.plaid_link as plaid_link
plaid_link = importlib.reload(plaid_link)
plaid_link.render_plaid_link("link-production-test-token")
"""
    ).run(timeout=20)

    assert not app.exception


def test_authenticated_budget_uses_all_spending_and_logout_clears_private_state(tmp_path, monkeypatch):
    import time
    from datetime import date
    from src.domain.models import Transaction, Budget
    from src.repositories.transaction_repo import TransactionRepository
    import src.repositories.supabase_repo as cloud_repos
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    repo = TransactionRepository(tmp_path / 'ui-test.db')
    repo.insert_many([
        Transaction(id='coffee', date=date(2026, 9, 1), description='Coffee', amount=-20, category='Dining', account_name='Checking'),
        Transaction(id='dinner', date=date(2026, 9, 20), description='Dinner', amount=-100, category='Dining', account_name='Checking'),
    ])
    repo.upsert_budget(Budget(category='Dining', monthly_limit=100))
    repo.set_setting('onboarding_complete', 'true')
    monkeypatch.setenv('FINANCEBUDDY_ENV', 'development')
    monkeypatch.setattr(SupabaseConfig, 'from_sources', classmethod(lambda cls, secrets: SupabaseConfig('https://test.supabase.co', 'test')))
    monkeypatch.setattr(SupabaseAuth, 'get_user', lambda self, token: {'id': 'alice', 'email': 'alice@example.com', 'email_confirmed_at': '2026-09-01'})
    monkeypatch.setattr(SupabaseAuth, 'sign_out', lambda self, token: None)
    monkeypatch.setattr(cloud_repos, 'SupabaseTransactionRepository', lambda client, user_id: repo)
    monkeypatch.setattr(PlaidConfig, 'from_sources', classmethod(lambda cls, secrets: PlaidConfig('', '')))
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py')
    app.session_state['supabase_session'] = {'access_token': 'test', 'refresh_token': 'test', 'expires_at': time.time() + 3600}
    app.session_state['session_owner'] = 'alice'
    app.session_state['undo_transactions'] = repo.get_all()
    app.run(timeout=20)
    assert not app.exception
    app.text_input(key='dashboard_search').set_value('Coffee').run()
    assert not app.exception
    assert any('Showing 1 of 2 saved transactions' in item.value for item in app.get('caption'))
    app.radio(key='nav_page').set_value('Plan').run()
    assert not app.exception
    assert any('Over budget by $20.00' in item.value for item in app.error)
    next(button for button in app.button if button.label == 'Log out').click().run()
    assert not app.exception
    assert 'supabase_session' not in app.session_state
    assert 'undo_transactions' not in app.session_state


def test_inactive_session_is_cleared_before_financial_access(monkeypatch):
    import time
    from src.services.supabase import SupabaseAuth, SupabaseConfig

    monkeypatch.setenv('FINANCEBUDDY_ENV', 'development')
    monkeypatch.setattr(SupabaseConfig, 'from_sources', classmethod(lambda cls, secrets: SupabaseConfig('https://test.supabase.co', 'test')))
    monkeypatch.setattr(SupabaseAuth, 'get_user', lambda self, token: (_ for _ in ()).throw(AssertionError('Expired session reached Auth')))
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py')
    app.session_state['supabase_session'] = {'access_token': 'stale', 'refresh_token': 'stale', 'expires_at': time.time() + 3600}
    app.session_state['last_activity_at'] = time.time() - 1900
    app.session_state['undo_import_rows'] = ['private']
    app.run(timeout=20)
    assert not app.exception
    assert 'supabase_session' not in app.session_state
    assert 'undo_import_rows' not in app.session_state
    assert any('session expired' in info.value.lower() for info in app.info)


def test_first_run_actions_open_the_selected_account_flow(tmp_path, monkeypatch):
    import time
    import src.repositories.supabase_repo as cloud_repos
    from src.repositories.transaction_repo import TransactionRepository
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    repo = TransactionRepository(tmp_path / 'first-run.db')
    repo.set_setting('onboarding_complete', 'true')
    monkeypatch.setenv('FINANCEBUDDY_ENV', 'development')
    monkeypatch.setattr(SupabaseConfig, 'from_sources', classmethod(lambda cls, secrets: SupabaseConfig('https://test.supabase.co', 'test')))
    monkeypatch.setattr(SupabaseAuth, 'get_user', lambda self, token: {'id': 'alice', 'email': 'alice@example.com', 'email_confirmed_at': '2026-09-01'})
    monkeypatch.setattr(cloud_repos, 'SupabaseTransactionRepository', lambda client, user_id: repo)
    monkeypatch.setattr(PlaidConfig, 'from_sources', classmethod(lambda cls, secrets: PlaidConfig('', '')))
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py')
    app.session_state['supabase_session'] = {'access_token': 'test', 'refresh_token': 'test', 'expires_at': time.time() + 3600}
    app.run(timeout=20)
    assert not app.exception
    assert any(button.label == 'Connect a bank' for button in app.button)
    assert any(button.label == 'Upload a statement' for button in app.button)
    next(button for button in app.button if button.label == 'Upload a statement').click().run()
    assert not app.exception
    assert app.session_state['nav_page'] == 'Accounts'
    assert app.session_state['accounts_view'] == 'Import statement'
    assert any(item.value == 'Import a statement' for item in app.header)
    assert not any(item.value == 'Saved accounts' for item in app.header)


def test_mobile_history_can_reach_rows_after_first_page():
    app = AppTest.from_string('''
from datetime import date
from src.domain.models import Transaction
from src.ui.components import render_transaction_table
rows = [Transaction(id=str(i), date=date(2026, 9, 1), description=f"Row {i}", amount=-1, category="Dining", account_name="Checking") for i in range(51)]
render_transaction_table(rows)
''').run(timeout=20)
    assert not app.exception
    assert app.selectbox(key='mobile_history_page').value == 1
    app.selectbox(key='mobile_history_page').set_value(2).run()
    assert not app.exception
    assert any('Row 50' in item.value for item in app.markdown)


def test_auth_stage_stays_on_verification_after_signup(monkeypatch):
    from src.services.supabase import SupabaseAuth, SupabaseConfig

    monkeypatch.setenv('FINANCEBUDDY_ENV', 'development')
    monkeypatch.setattr(SupabaseConfig, 'from_sources', classmethod(lambda cls, secrets: SupabaseConfig('https://test.supabase.co', 'test')))
    monkeypatch.setattr(SupabaseAuth, 'sign_up', lambda self, email, password: {})
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py').run(timeout=20)
    app.radio(key='auth_stage').set_value('Create account').run()
    app.text_input(key='signup_email').set_value('alice@example.com')
    app.text_input(key='signup_password').set_value('long-secure-password')
    app.text_input(key='signup_confirmation').set_value('long-secure-password')
    next(button for button in app.button if button.label == 'Create secure account').click().run()
    assert not app.exception
    assert app.radio(key='auth_stage').value == 'Create account'
    assert app.session_state['pending_verification_email'] == 'alice@example.com'
    assert any(button.label == 'Verify and continue' for button in app.button)
    app.run()
    assert any(button.label == 'Verify and continue' for button in app.button)


def test_overview_tabs_balances_presets_and_budget_pace(tmp_path, monkeypatch):
    import time
    from datetime import date, timedelta
    from src.domain.models import Budget, SavingsGoal, Transaction
    from src.repositories.transaction_repo import TransactionRepository
    import src.repositories.supabase_repo as cloud_repos
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    rows = []
    for offset in range(6):
        start = date(2026, 4 + offset, 1)
        rows += [
            Transaction(id=f'pay{offset}', date=start, description='ACME PAYROLL', amount=5000, category='Salary/Income', account_name='Checking'),
            Transaction(id=f'rent{offset}', date=start + timedelta(days=1), description='Rent', amount=-1800, category='Housing', account_name='Checking'),
            Transaction(id=f'tv{offset}', date=start + timedelta(days=4), description='NETFLIX.COM', amount=-15.49 if offset < 3 else -17.99,
                        category='Subscriptions', account_name='Card', account_type='Credit Card', merchant_name='Netflix'),
            Transaction(id=f'cardpay{offset}', date=start + timedelta(days=20), description='Payment to Chase card', amount=-600, category='Credit Card Payments', account_name='Checking'),
            Transaction(id=f'food{offset}', date=start + timedelta(days=8), description='SQ *CAFE 1234', amount=-40 - offset, category='Dining',
                        account_name='Card', account_type='Credit Card', subcategory='Coffee', payment_channel='in store', pending=offset == 5),
        ]

    class MemoryRepo(TransactionRepository):
        # Serves the in-memory rows (which keep bank-connection detail) and mirrors
        # category edits into them, while SQLite still records every write.
        def get_all(self):
            return super().get_all()

        def get_all_saved(self):
            return TransactionRepository.get_all(self)

        def update_category(self, transaction_id, category):
            super().update_category(transaction_id, category)
            for item in rows:
                if item.id == transaction_id:
                    item.category = category

    class FakePlaidRepo:
        def __init__(self, client, user_id):
            pass

        def get_accounts(self, item_id=None):
            return [
                {'account_id': 'c', 'type': 'depository', 'name': 'Checking', 'current_balance': 4000.0, 'available_balance': 3900.0},
                {'account_id': 'k', 'type': 'credit', 'name': 'Card', 'current_balance': 500.0, 'credit_limit': 2000.0},
            ]

        def get_balance_history(self, since=None):
            return [
                {'account_id': 'c', 'as_of': '2026-09-01', 'type': 'depository', 'current_balance': 3500.0},
                {'account_id': 'c', 'as_of': '2026-09-10', 'type': 'depository', 'current_balance': 4000.0},
            ]

    repo = MemoryRepo(tmp_path / 'insights.db')
    repo.insert_many(rows)
    repo.set_setting('onboarding_complete', 'true')
    repo.upsert_budget(Budget(category='Dining', monthly_limit=30))
    repo.save_goal(SavingsGoal(name='Emergency fund', target_amount=10000, current_amount=1000, target_date=date(2027, 9, 1)))
    monkeypatch.setenv('FINANCEBUDDY_ENV', 'development')
    monkeypatch.setattr(SupabaseConfig, 'from_sources', classmethod(lambda cls, secrets: SupabaseConfig('https://test.supabase.co', 'test')))
    monkeypatch.setattr(SupabaseAuth, 'get_user', lambda self, token: {'id': 'alice', 'email': 'alice@example.com', 'email_confirmed_at': '2026-09-01'})
    monkeypatch.setattr(cloud_repos, 'SupabaseTransactionRepository', lambda client, user_id: repo)
    monkeypatch.setattr(cloud_repos, 'SupabasePlaidRepository', FakePlaidRepo)
    monkeypatch.setattr(PlaidConfig, 'from_sources', classmethod(lambda cls, secrets: PlaidConfig('client', 'secret')))
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py', default_timeout=30)
    app.session_state['supabase_session'] = {'access_token': 'test', 'refresh_token': 'test', 'expires_at': time.time() + 3600}
    app.run()
    assert not app.exception
    assert app.segmented_control(key='overview_view').options == ['Summary', 'Trends', 'Habits', 'Recurring & income', 'Balances']
    app.selectbox(key='dashboard_date_preset').set_value('All time').run()
    metrics = {metric.label: metric for metric in app.metric}
    assert [metric.label for metric in app.metric][:3] == ['Spending', 'Money in', 'Net cash flow']
    assert any(expander.label == 'More spending details' for expander in app.expander)
    assert any(expander.label == 'Filters' for expander in app.expander)
    assert any(button.label == 'View all transactions' for button in app.button)
    metrics = {metric.label: metric for metric in app.metric}
    assert metrics['Spending'].value == '$11,155.44'
    assert metrics['Moved between accounts'].value == '$3,600.00'
    assert 'Net worth' not in metrics  # Unselected views are not computed.
    app.segmented_control(key='overview_view').set_value('Balances').run()
    assert not app.exception
    metrics = {metric.label: metric for metric in app.metric}
    assert metrics['Net worth'].value == '$3,500.00'
    assert metrics['Credit utilization'].value == '25%'
    app.segmented_control(key='overview_view').set_value('Habits').run()
    assert not app.exception
    app.segmented_control(key='overview_view').set_value('Recurring & income').run()
    assert not app.exception
    assert any(r'Netflix went up from \$15.49 to \$17.99' in item.value for item in app.warning)
    app.segmented_control(key='overview_view').set_value('Summary').run()

    # A main-chart selection must coexist with the deeper category explorer.
    import streamlit as st
    original_plotly_chart = st.plotly_chart

    def select_dining(*args, **kwargs):
        event = original_plotly_chart(*args, **kwargs)
        if kwargs.get('key') == 'summary_top_categories':
            return {'selection': {'points': [{'y': 'Dining'}]}}
        return event

    monkeypatch.setattr(st, 'plotly_chart', select_dining)
    app.run()
    assert not app.exception
    selected_frame = next(frame.value for frame in app.dataframe if list(frame.value.columns) ==
                          ['Date', 'Amount', 'Category', 'Description', 'Account', 'Type'])
    assert len(selected_frame) == 6
    assert set(selected_frame['Category']) == {'Dining'}
    monkeypatch.setattr(st, 'plotly_chart', original_plotly_chart)

    app.selectbox(key='dashboard_date_preset').set_value('Last month').run()
    assert not app.exception
    assert app.session_state['dashboard_dates'] == (date(2026, 8, 1), date(2026, 8, 31))
    spending = next(metric for metric in app.metric if metric.label == 'Spending')
    assert spending.delta == '+$1.00'
    assert any('Changes compare with Jul 1, 2026–Jul 31, 2026' in item.value for item in app.caption)

    app.toggle(key='dashboard_include_transfers').set_value(True).run()
    assert not app.exception
    assert next(metric for metric in app.metric if metric.label == 'Spending').value == '$2,461.99'

    app.selectbox(key='drilldown_category').set_value('Dining').run()
    assert not app.exception

    rows.append(Transaction(id='legacy', date=date(2026, 9, 21), description='Payment to Chase card',
                            amount=-50, category='Debt Payments', account_name='Checking'))
    repo.insert_many([rows[-1]])
    app.run()
    assert any('look like credit-card payments' in item.value for item in app.warning)
    next(button for button in app.button if button.label == 'Move them to Credit Card Payments').click().run()
    assert not app.exception
    assert next(item for item in repo.get_all_saved() if item.id == 'legacy').category == 'Credit Card Payments'

    for key, style in (('chart_style_cash_flow', 'Net'), ('chart_style_cash_flow', 'Lines'),
                       ('chart_style_categories', 'Map'), ('chart_style_categories', 'Bars'),
                       ('chart_style_fixed_flexible', 'Side by side'), ('chart_style_category_trend', 'Lines')):
        if key in ('chart_style_fixed_flexible', 'chart_style_category_trend'):
            app.segmented_control(key='overview_view').set_value('Trends').run()
        app.segmented_control(key=key).set_value(style).run()
        assert not app.exception
    assert repo.get_setting('chart_preferences') is not None

    app.radio(key='nav_page').set_value('Transactions').run()
    assert not app.exception
    assert app.selectbox(key='dashboard_date_preset').value == 'Last month'
    app.segmented_control(key='tx_view').set_value('Pending').run()
    assert not app.exception
    # The only pending purchase is outside the 'Last month' range chosen above.
    assert any('Nothing is pending' in item.value for item in app.success)
    assert not app.dataframe
    app.button(key='tx_empty_all').click().run()
    assert not app.exception
    assert app.segmented_control(key='tx_view').value == 'All'
    app.button(key='mobile_edit_transactions_table_All_tv4').click().run()
    assert not app.exception
    assert app.session_state['active_dialog']['id'] == 'tv4'
    app.button(key='mobile_edit_transactions_table_All_food4').click().run()
    assert not app.exception
    assert app.session_state['active_dialog']['id'] == 'food4'
    next(box for box in app.selectbox if box.label == 'Category').set_value('Entertainment')
    from src.services.supabase import SupabaseError
    original_update = repo.correct_category

    def fail_save(*args, **kwargs):
        raise SupabaseError('private server error details')

    monkeypatch.setattr(repo, 'correct_category', fail_save)
    next(button for button in app.button if button.label == 'Save category').click().run()
    assert not app.exception
    assert any("Couldn't confirm the category save" in item.value for item in app.error)
    assert not any('private server error details' in item.value for item in app.error)
    assert next(box for box in app.selectbox if box.label == 'Category').value == 'Entertainment'
    assert next(item for item in repo.get_all() if item.id == 'food4').category == 'Dining'
    monkeypatch.setattr(repo, 'correct_category', original_update)
    next(button for button in app.button if button.label == 'Save category').click().run()
    assert not app.exception
    assert next(item for item in repo.get_all() if item.id == 'food4').category == 'Entertainment'
    assert 'active_dialog' not in app.session_state
    app.button(key='mobile_edit_transactions_table_All_food4').click().run()
    assert not app.exception
    assert app.session_state['active_dialog']['id'] == 'food4'
    next(button for button in app.button if button.label == 'Cancel').click().run()
    assert not app.exception
    assert 'active_dialog' not in app.session_state
    # Restore the category so the subsequent budget check uses the original spend.
    repo.update_category('food4', 'Dining')

    app.radio(key='nav_page').set_value('Plan').run()
    assert not app.exception
    assert any('Over budget by $15.00' in item.value for item in app.error)
    app.segmented_control(key='plan_view').set_value('Savings goals').run()
    assert not app.exception
    assert any('projected to finish around' in item.value for item in app.markdown)

    app.radio(key='nav_page').set_value('Overview').run()
    app.session_state['dashboard_date_preset'] = 'Custom'
    app.session_state['dashboard_dates'] = (date(2020, 1, 1), date(2020, 1, 31))
    app.run()
    assert not app.exception
    assert any('No transactions match' in item.value for item in app.info)
    assert not app.metric
    app.button(key='overview_empty_reset').click().run()
    assert not app.exception
    assert any(metric.label == 'Spending' for metric in app.metric)

    original_get_all = repo.get_catalog
    monkeypatch.setattr(repo, 'get_catalog', fail_save)
    app.run()
    assert not app.exception
    assert any("Couldn't load your transactions" in item.value for item in app.error)
    assert not any('private server error details' in item.value for item in app.error)
    monkeypatch.setattr(repo, 'get_catalog', original_get_all)
    app.button(key='retry_transactions').click().run()
    assert not app.exception
    assert any(metric.label == 'Spending' for metric in app.metric)


def test_mobile_history_page_recovers_from_stale_label(monkeypatch):
    from src.ui import components

    captured = {}

    class FakeState(dict):
        __getattr__ = dict.get

        def __setattr__(self, key, value):
            self[key] = value

    state = FakeState(mobile_history_page="Page 1 of 2")
    monkeypatch.setattr(components.st, "session_state", state)

    def fake_selectbox(label, options, format_func, key):
        captured["value"] = state[key]
        captured["labels"] = [format_func(option) for option in options]
        return state[key]

    monkeypatch.setattr(components.st, "selectbox", fake_selectbox)
    monkeypatch.setattr(components.st, "dataframe", lambda *args, **kwargs: None)
    components.render_transaction_table([])
    assert state.mobile_history_page == 1
    assert not captured  # No page selector is needed for a single page.


def test_navigation_and_transaction_pages_use_catalog_and_explicit_reads(monkeypatch):
    import time
    from datetime import date
    from uuid import UUID
    from src.domain.models import Transaction
    import src.repositories.supabase_repo as cloud_repos
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    class ReadRepo:
        pages = 0
        exports = 0
        account_id = '00000000-0000-0000-0000-000000000010'
        def get_catalog(self):
            return {'count': 50000, 'display_count': 50000, 'min_date': '2020-01-01', 'max_date': '2026-10-01',
                    'needs_review': 0, 'categories': ['Dining'],
                    'accounts': [{'id': self.account_id, 'name': 'Checking', 'account_type': 'Checking',
                                  'transactions': 50000, 'first_date': '2020-01-01', 'latest_date': '2026-10-01', 'connected': False}]}
        def get_setting(self, key, default=None):
            return 'true' if key == 'onboarding_complete' else default
        def get_category_rules(self):
            return []
        def get_all(self):
            raise AssertionError('Navigation must not load all transactions')
        def get_filtered(self, filters=None):
            raise AssertionError('This page must not load an unbounded history')
        def get_page(self, filters=None, *, limit=200, after=None):
            self.pages += 1
            assert limit == 200
            rows = [Transaction(id=f'row:{index}', date=date(2026,10,1), description=f'Store {index}', amount='-1.00',
                                category='Dining', account_name='Checking', account_id=UUID(self.account_id)) for index in range(200)]
            return {'rows': rows, 'count': 50000, 'next_cursor': {'date': '2026-10-01', 'id': rows[-1].id}}
        def export_backup(self):
            self.exports += 1
            return '{}'

    repo = ReadRepo()
    monkeypatch.setenv('FINANCEBUDDY_ENV', 'development')
    monkeypatch.setattr(SupabaseConfig, 'from_sources', classmethod(lambda cls, secrets: SupabaseConfig('https://test.supabase.co', 'test')))
    monkeypatch.setattr(SupabaseAuth, 'get_user', lambda self, token: {'id': 'alice', 'email': 'alice@example.com', 'email_confirmed_at': '2026-09-01'})
    monkeypatch.setattr(cloud_repos, 'SupabaseTransactionRepository', lambda client, user_id: repo)
    monkeypatch.setattr(PlaidConfig, 'from_sources', classmethod(lambda cls, secrets: PlaidConfig('', '')))
    app = AppTest.from_file(Path(__file__).parents[1] / 'app.py', default_timeout=30)
    app.session_state['supabase_session'] = {'access_token': 'test', 'refresh_token': 'test', 'expires_at': time.time() + 3600}
    app.session_state['session_owner'] = 'alice'
    app.session_state['nav_page'] = 'Accounts'
    app.session_state['accounts_view'] = 'Saved accounts'
    app.run()
    assert not app.exception and repo.pages == 0
    app.radio(key='nav_page').set_value('Settings').run()
    assert not app.exception and repo.exports == 0
    next(button for button in app.button if button.label == 'Prepare full JSON backup').click().run()
    assert not app.exception and repo.exports == 1
    app.radio(key='nav_page').set_value('Transactions').run()
    assert not app.exception and repo.pages == 1
    assert len(app.dataframe[0].value) == 200


def _signed_in_app(monkeypatch, repo, page, **state):
    import time
    import src.repositories.supabase_repo as cloud_repos
    from src.services.supabase import SupabaseAuth, SupabaseConfig
    from src.services.plaid_service import PlaidConfig

    repo.set_setting("onboarding_complete", "true")
    monkeypatch.setenv("FINANCEBUDDY_ENV", "development")
    monkeypatch.setattr(SupabaseConfig, "from_sources", classmethod(lambda cls, secrets: SupabaseConfig("https://test.supabase.co", "test")))
    monkeypatch.setattr(SupabaseAuth, "get_user", lambda self, token: {"id": "alice", "email": "alice@example.com", "email_confirmed_at": "2026-09-01"})
    monkeypatch.setattr(cloud_repos, "SupabaseTransactionRepository", lambda client, user_id: repo)
    monkeypatch.setattr(PlaidConfig, "from_sources", classmethod(lambda cls, secrets: PlaidConfig("", "")))
    app = AppTest.from_file(Path(__file__).parents[1] / "app.py")
    app.session_state["supabase_session"] = {"access_token": "test", "refresh_token": "test", "expires_at": time.time() + 3600}
    app.session_state["session_owner"] = "alice"
    app.session_state["nav_page"] = page
    for key, value in state.items():
        app.session_state[key] = value
    return app.run(timeout=20)


def test_triage_queue_categorizes_a_merchant_and_saves_its_rule(tmp_path, monkeypatch):
    from datetime import date
    from src.domain.models import CategoryRule, Transaction
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "triage-ui.db"))
    repo.insert_many([
        Transaction(id=f"bb{day}", date=date(2026, 9, day), description=f"BLUE BOTTLE COFFEE #{day}0042 OAKLAND",
                    amount=-5, category="Uncategorized", account_name="Card")
        for day in (1, 2, 3)
    ] + [Transaction(id="other", date=date(2026, 9, 4), description="CORNER STORE", amount=-3,
                     category="Uncategorized", account_name="Card")])
    app = _signed_in_app(monkeypatch, repo, "Transactions", tx_view="Needs a category")
    assert not app.exception
    assert any("Sort by merchant" in item.value for item in app.subheader)
    box = next(item for item in app.selectbox if item.label == "Category for BLUE BOTTLE COFFEE")
    box.set_value("Dining").run()
    key = box.key.removeprefix("triage_category_")
    app.button(key=f"triage_apply_{key}").click().run()
    assert not app.exception
    saved = {item.id: item for item in repo.get_all()}
    assert {saved[f"bb{day}"].category_origin for day in (1, 2, 3)} == {"rule"}
    assert saved["other"].category == "Uncategorized"
    assert CategoryRule(keyword="blue bottle coffee", category="Dining") in repo.get_category_rules()


def test_active_filters_show_outside_the_closed_panel_and_reset(tmp_path, monkeypatch):
    from datetime import date
    from src.domain.models import Transaction
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "filters-ui.db"))
    repo.insert_many([Transaction(id=name, date=date(2026, 9, 1), description=name, amount=-5,
                                  category="Dining", account_name="Card") for name in ("Costco", "Cafe")])
    app = _signed_in_app(monkeypatch, repo, "Transactions")
    assert not app.exception
    assert not any(button.key == "reset_filters" for button in app.button)
    app.text_input(key="dashboard_search").set_value("costco").run()
    assert not app.exception
    chips = [item.value for item in app.markdown if "fb-chip" in item.value]
    assert any("Merchant: costco" in chip for chip in chips)
    panel = next(item for item in app.expander if item.label.startswith("Filters"))
    assert not any("Merchant: costco" in item.value for item in panel.markdown)
    app.button(key="reset_filters").click().run()
    assert not app.exception
    assert app.text_input(key="dashboard_search").value == ""


def test_compare_saved_months_without_uploading(tmp_path, monkeypatch):
    from datetime import date
    from src.domain.models import Transaction
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "compare-ui.db"))
    repo.insert_many([
        Transaction(id=f"t{month}", date=date(2026, month, 15), description="Groceries", amount=-10 * month,
                    category="Groceries", account_name="Card")
        for month in (7, 8, 9)
    ])
    app = _signed_in_app(monkeypatch, repo, "Compare")
    assert not app.exception
    assert app.segmented_control(key="compare_source").value == "Saved history"
    assert app.multiselect(key="compare_periods_Month").value == ["Aug 2026", "Sep 2026"]
    table = app.dataframe[0].value
    assert table["Period"].tolist() == ["Aug 2026", "Sep 2026 (partial)"]
    assert table["Money out"].tolist() == [80, 90]
    app.segmented_control(key="compare_granularity").set_value("Quarter").run()
    assert not app.exception
    assert any("Choose two to six" in item.value for item in app.info)


def test_plan_dialogs_suggest_limits_add_funds_and_confirm_removal(tmp_path, monkeypatch):
    from datetime import date
    from decimal import Decimal
    from src.domain.models import Budget, SavingsGoal, Transaction
    from src.repositories.transaction_repo import TransactionRepository
    from src.services.supabase import SupabaseError

    repo = TransactionRepository(str(tmp_path / "plan-ui.db"))
    repo.insert_many([
        Transaction(id=f"{category}{month}", date=date(2026, month, day), description=category, amount=amount,
                    category=category, account_name="Card")
        for month in (7, 8, 9) for day, category, amount in ((1, "Dining", -40.25), (28, "Shopping", -60))
    ] + [Transaction(id="end", date=date(2026, 9, 30), description="Rent", amount=-1, category="Rent", account_name="Card")])
    repo.upsert_budget(Budget(category="Dining", monthly_limit=100))
    goal_id = repo.save_goal(SavingsGoal(name="Trip", target_amount=1000, current_amount=100))
    app = _signed_in_app(monkeypatch, repo, "Plan")
    assert not app.exception
    assert any("Unbudgeted spending: $61.00" in item.value for item in app.markdown)

    # "Set budget" opens the dialog for that category with its 3-month average.
    next(button for button in app.button if button.label == "Set budget" and "Shopping" in button.help).click().run()
    assert app.selectbox(key="budget_dialog_category").value == "Shopping"
    app.button(key="budget_use_average").click().run()
    assert next(item for item in app.number_input if item.label == "Monthly limit").value == 60.0
    next(button for button in app.button if button.label == "Save budget").click().run()
    assert not app.exception
    assert Budget(category="Shopping", monthly_limit=60) in repo.get_budgets()

    # A failed save keeps the dialog open with a friendly message.
    original_delete = repo.delete_budget
    monkeypatch.setattr(repo, "delete_budget", lambda category: (_ for _ in ()).throw(SupabaseError("details")))
    app.button(key=next(button.key for button in app.button if button.key and button.key.startswith("budget_remove_"))).click().run()
    next(button for button in app.button if button.label == "Remove budget").click().run()
    assert not app.exception
    assert any("Couldn't confirm the save" in item.value for item in app.error)
    assert app.session_state["active_dialog"]["name"] == "remove_budget"
    monkeypatch.setattr(repo, "delete_budget", original_delete)
    next(button for button in app.button if button.label == "Remove budget").click().run()
    assert [item.category for item in repo.get_budgets()] == ["Shopping"]

    app.segmented_control(key="plan_view").set_value("Savings goals").run()
    # Open Edit first so its inputs hold the old saved amount.
    app.button(key=f"goal_edit_{goal_id}").click().run()
    assert next(item for item in app.number_input if item.label == "Already saved").value == 100.0
    app.button(key=f"goal_add_funds_{goal_id}").click().run()
    next(item for item in app.number_input if item.label == "Amount to add").set_value(12.34)
    app.button(key="goal_add_confirm").click().run()
    assert not app.exception
    assert repo.get_goals()[0].current_amount == Decimal("112.34")
    # Editing after adding funds starts from the saved amount, not an earlier dialog's value.
    app.button(key=f"goal_edit_{goal_id}").click().run()
    assert next(item for item in app.number_input if item.label == "Already saved").value == 112.34


def test_import_asks_for_card_signs_with_evidence_and_can_flip(tmp_path, monkeypatch):
    from io import BytesIO
    import streamlit as st
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "signs-ui.db"))
    upload = BytesIO(b"Date,Description,Amount\n2026-10-01,PAYMENT THANK YOU,-300\n2026-10-02,Grocery,80.25\n")
    upload.name = "card.csv"
    monkeypatch.setattr(st, "file_uploader", lambda *args, **kwargs: upload if kwargs.get("key") == "statement_import" else None)
    app = _signed_in_app(monkeypatch, repo, "Accounts", accounts_view="Import statement", import_account_type="Credit Card")
    assert not app.exception
    # Payments read as money out under "negative", so "positive" is suggested.
    positive = app.button(key="choose_outflow_positive")
    assert positive.proto.type == "primary"
    assert not any(button.label == "Save statement" for button in app.button)
    positive.click().run()
    assert not app.exception
    assert app.selectbox(key="statement_import_sign_Credit Card").value == "outflow_positive"
    assert any("signs look right" in item.value for item in app.caption)
    assert app.dataframe[0].value["Amount"].tolist() == [300.0, -80.25]
    app.button(key="flip_signs").click().run()
    assert not app.exception
    assert any("signs look reversed" in item.value for item in app.warning)


def test_recurring_charge_can_be_dismissed_and_restored(tmp_path, monkeypatch):
    from datetime import date
    from src.domain.models import Transaction
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "recurring-ui.db"))
    repo.insert_many([
        Transaction(id=f"{name}{month}", date=date(2026, month, day), description=name, amount=amount,
                    category="Health", account_name="Checking")
        for month in (6, 7, 8, 9) for name, day, amount in (("CITY GYM", 10, -40), ("STREAMFLIX", 3, -15.49))
    ])
    app = _signed_in_app(monkeypatch, repo, "Overview", overview_view="Recurring & income")
    assert not app.exception

    def recurring_names():
        table = next(frame.value for frame in app.dataframe if "Merchant" in frame.value.columns)
        return set(table["Merchant"])

    assert recurring_names() == {"CITY GYM", "STREAMFLIX"}
    app.selectbox(key="recurring_exclude_choice").set_value("CITY GYM")
    app.button(key="recurring_exclude").click().run()
    assert not app.exception
    assert repo.get_setting("recurring_exclusions") == '["city gym"]'
    assert recurring_names() == {"STREAMFLIX"}
    next(button for button in app.button if button.label == "Restore").click().run()
    assert not app.exception
    assert repo.get_setting("recurring_exclusions") == "[]"
    assert recurring_names() == {"CITY GYM", "STREAMFLIX"}


def test_duplicates_can_be_kept_or_removed(tmp_path, monkeypatch):
    from datetime import date
    from src.domain.models import Transaction
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "duplicates-ui.db"))

    def row(identifier, description):
        return Transaction(id=identifier, date=date(2026, 9, 1), description=description, amount=-12,
                           category="Dining", account_name="Card")

    repo.insert_many([row("statement:a", "Cafe"), row("statement:b", "Cafe"),
                      row("plaid:c", "Bakery"), row("statement:d", "Bakery")])
    app = _signed_in_app(monkeypatch, repo, "Transactions", tx_view="Possible duplicates")
    assert not app.exception
    # Only the statement copy of the bank row can be removed; the bank copy is kept.
    bakery = [button for button in app.button if button.label == "Remove this copy" and "Bakery" in button.help]
    assert len(bakery) == 1
    bakery[0].click().run()
    assert not app.exception
    assert {item.id for item in repo.get_all()} == {"statement:a", "statement:b", "plaid:c"}
    next(button for button in app.button if button.label == "Keep both").click().run()
    assert not app.exception
    assert repo.get_setting("duplicate_dismissals") == '["statement:a|statement:b"]'
    assert any("No exact duplicate" in item.value for item in app.success)


def test_sample_data_loads_from_first_run_and_clears(tmp_path, monkeypatch):
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "sample-ui.db"))
    app = _signed_in_app(monkeypatch, repo, "Overview")
    assert not app.exception
    next(button for button in app.button if button.label == "Explore with sample data").click().run()
    assert not app.exception
    assert {row.account_name for row in repo.get_all()} == {"Sample Checking · demo", "Sample Card · demo"}
    assert any("exploring sample data" in item.value for item in app.markdown)
    app.button(key="clear_sample").click().run()
    assert not app.exception
    app.button(key="clear_sample_confirm").click().run()
    assert not app.exception
    assert repo.get_all() == [] and repo.get_budgets() == [] and repo.get_goals() == []
    assert not any("exploring sample data" in item.value for item in app.markdown)


def test_manual_transaction_notes_tags_and_tag_filter(tmp_path, monkeypatch):
    from datetime import date
    from src.domain.models import Transaction
    from src.repositories.transaction_repo import TransactionRepository

    repo = TransactionRepository(str(tmp_path / "manual-ui.db"))
    repo.insert_many([Transaction(id="hotel", date=date.today(), description="Hotel", amount=-200,
                                  category="Travel", account_name="Card")])
    app = _signed_in_app(monkeypatch, repo, "Transactions")
    assert not app.exception
    next(button for button in app.button if button.label == "Add transaction").click().run()
    app.text_input(key="manual_description").set_value("Farmers market").run()
    app.number_input(key="manual_amount").set_value(12.5)
    app.selectbox(key="manual_category").set_value("Groceries")
    app.text_input(key="manual_tags").set_value("market, #weekend")
    next(button for button in app.button if button.label == "Save transaction").click().run()
    assert not app.exception
    manual = next(item for item in repo.get_canonical_all() if item.id.startswith("manual:"))
    assert (str(manual.amount), manual.account_name, manual.category_origin, manual.tags) == (
        "-12.50", "Cash", "manual", ["market", "weekend"])

    app.button(key="mobile_edit_transactions_table_All_hotel").click().run()
    app.segmented_control(key="transaction_editor_tab").set_value("Notes & tags").run()
    app.text_area(key="notes_hotel").set_value("Conference stay")
    app.text_input(key="tags_hotel").set_value("work")
    next(button for button in app.button if button.label == "Save notes").click().run()
    assert not app.exception
    assert next(item for item in repo.get_canonical_all() if item.id == "hotel").notes == "Conference stay"

    app.text_input(key="dashboard_tag").set_value("Work").run()
    assert not app.exception
    assert app.dataframe[0].value["Description"].tolist() == ["Hotel"]
