from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_app_stops_at_authentication_boundary_without_exceptions():
    app_path = Path(__file__).parents[1] / "app.py"
    app = AppTest.from_file(app_path).run(timeout=20)

    assert not app.exception
    assert app.title[0].value == "💸 FinanceBuddy"
    messages = [item.value for item in app.error] + [item.value for item in app.subheader]
    assert any(
        expected in message
        for expected in (
            "Account security must be configured",
            "Supabase must be configured",
            "Your finances stay private to your account",
        )
        for message in messages
    )


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
    app.selectbox(key='nav_page').set_value('Plan').run()
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
    assert any(item.value == 'Import a statement' for item in app.subheader)
    assert not any(item.value == 'Saved accounts' for item in app.subheader)


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
            return sorted(rows, key=lambda item: (item.date, item.id), reverse=True)

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

        def get_balance_history(self):
            return [
                {'account_id': 'c', 'as_of': '2026-09-01', 'type': 'depository', 'current_balance': 3500.0},
                {'account_id': 'c', 'as_of': '2026-09-10', 'type': 'depository', 'current_balance': 4000.0},
            ]

    repo = MemoryRepo(tmp_path / 'insights.db')
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
    assert [tab.label for tab in app.tabs] == ['Summary', 'Trends', 'Habits', 'Recurring & income', 'Balances']
    metrics = {metric.label: metric for metric in app.metric}
    assert metrics['Spending'].value == '$11,155.44'
    assert metrics['Moved between accounts'].value == '$3,600.00'
    assert metrics['Net worth'].value == '$3,500.00'
    assert metrics['Credit utilization'].value == '25%'
    assert any('Netflix went up from $15.49 to $17.99' in item.value for item in app.markdown)

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

    app.selectbox(key='nav_page').set_value('Plan').run()
    assert not app.exception
    assert any('Over budget by $15.00' in item.value for item in app.error)
    app.radio(key='plan_view').set_value('Savings goals').run()
    assert not app.exception
    assert any('projected to finish around' in item.value for item in app.markdown)
