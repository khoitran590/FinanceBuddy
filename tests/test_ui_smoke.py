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
