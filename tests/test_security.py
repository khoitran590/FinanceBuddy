import json
from unittest.mock import Mock

import httpx
import pytest
from pydantic import ValidationError

from src.domain.models import Budget, SavingsGoal, Transaction
from src.repositories.supabase_repo import SupabaseTransactionRepository
from src.services.parser import BankStatementParser
from src.services.plaid_service import PlaidService
from src.services.security import MAX_UPLOAD_BYTES, bind_session_owner, clear_private_session, read_backup
from src.services.supabase import SupabaseConfig, SupabaseDataClient, SupabaseError


def test_logout_removes_financial_data_credentials_and_widget_state():
    state = {'supabase_session': {'access_token': 'secret'}, 'undo_transactions': ['private'],
             'statement_import': b'private', 'plaid_link_token': 'secret', 'password': 'secret'}
    clear_private_session(state)
    assert state == {}


def test_account_switch_discards_previous_account_data_but_keeps_new_session():
    state = {'session_owner': 'alice', 'supabase_session': {'access_token': 'bob-token'},
             'undo_transactions': ['alice-private'], 'plaid_update_item': 'alice-bank'}
    bind_session_owner(state, 'bob')
    assert state == {'session_owner': 'bob', 'supabase_session': {'access_token': 'bob-token'}}
    state['dashboard_search'] = 'coffee'
    bind_session_owner(state, 'bob')
    assert state['dashboard_search'] == 'coffee'


@pytest.mark.parametrize('payload', [[], None, {'version': 1}, {'version': 1, 'transactions': None}])
def test_incomplete_backups_cannot_trigger_deletion(payload):
    client = Mock()
    repo = SupabaseTransactionRepository(client, 'alice')
    with pytest.raises(ValueError):
        repo.restore_backup(json.dumps(payload).encode())
    assert not client.mock_calls


def test_invalid_financial_records_are_rejected_before_deletion():
    client = Mock()
    backup = {'version': 1, 'transactions': [], 'budgets': [{'category': 'Food', 'monthly_limit': -1}],
              'goals': [], 'category_rules': []}
    with pytest.raises(ValueError):
        SupabaseTransactionRepository(client, 'alice').restore_backup(json.dumps(backup).encode())
    assert not client.mock_calls


@pytest.mark.parametrize('amount', [float('nan'), float('inf'), -float('inf')])
def test_nonfinite_financial_values_are_rejected(amount):
    with pytest.raises(ValidationError):
        Transaction(id='x', date='2026-09-01', description='test', amount=amount, account_name='test')
    with pytest.raises(ValidationError):
        Budget(category='Food', monthly_limit=amount)
    with pytest.raises(ValidationError):
        SavingsGoal(name='Goal', target_amount=amount)


def test_oversized_inputs_rejected_before_parsing():
    raw = b'x' * (MAX_UPLOAD_BYTES + 1)
    for parse in (BankStatementParser.parse, BankStatementParser.parse_pdf):
        rows, metrics = parse(raw, 'Checking')
        assert not rows and not metrics.is_valid
        assert '10 MB' in metrics.errors[0]
    with pytest.raises(ValueError, match='10 MB'):
        read_backup(raw)


def test_excess_csv_rows_are_rejected():
    raw = b'Date,Description,Amount\n' + b'2026-09-01,Coffee,-4\n' * 50_001
    rows, metrics = BankStatementParser.parse(raw, 'Checking')
    assert not rows and not metrics.is_valid
    assert '50,000' in metrics.errors[0]


def test_oversized_csv_field_is_a_friendly_validation_error():
    raw = b'Date,Description,Amount\n2026-09-01,' + b'x' * 150_000 + b',-1'
    rows, metrics = BankStatementParser.parse(raw, 'Checking')
    assert not rows and not metrics.is_valid
    assert 'CSV field' in metrics.errors[0]


def test_backend_error_never_displays_financial_data():
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json={'message': 'private-row secret-token'}))
    client = SupabaseDataClient(SupabaseConfig('https://test.supabase.co', 'test'), 'jwt', transport=transport)
    with pytest.raises(SupabaseError) as error:
        client.select('transactions')
    assert 'private-row' not in str(error.value)
    assert 'secret-token' not in str(error.value)


def test_plaid_error_never_displays_raw_exception_or_provider_message():
    error = RuntimeError('secret-token')
    error.body = json.dumps({'error_code': 'UNKNOWN', 'error_message': 'private-row', 'display_message': 'secret-token'})
    assert PlaidService._friendly_error(error) == 'The bank request could not be completed. Please try again later.'
    error.body = json.dumps({'error_code': 'ITEM_LOGIN_REQUIRED'})
    assert 'Reconnect' in PlaidService._friendly_error(error)


@pytest.mark.parametrize('payload', [{'message': 'secret-password private-email'}, ['secret'], 'secret'])
def test_auth_errors_are_safe_even_with_unexpected_provider_responses(payload):
    from src.services.supabase import SupabaseAuth
    transport = httpx.MockTransport(lambda request: httpx.Response(400, json=payload))
    auth = SupabaseAuth(SupabaseConfig('https://test.supabase.co', 'test'), transport=transport)
    with pytest.raises(SupabaseError) as error:
        auth.sign_in('test@example.com', 'test-password')
    assert 'secret' not in str(error.value)
    assert 'private-email' not in str(error.value)
