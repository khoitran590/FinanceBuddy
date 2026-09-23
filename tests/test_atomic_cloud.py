import json
from datetime import date
from unittest.mock import Mock

import httpx
import pytest

from src.domain.models import Transaction
from src.repositories.supabase_repo import SupabasePlaidRepository, SupabaseTransactionRepository
from src.services.supabase import SupabaseConfig, SupabaseDataClient


def sample_transaction(identifier='a', account='Checking'):
    return Transaction(id=identifier, date=date(2026, 9, 1), description='Coffee',
                       amount=-4.5, category='Dining', account_name=account)


def test_rpc_posts_once_with_user_jwt():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=2)
    client = SupabaseDataClient(SupabaseConfig('https://project.supabase.co', 'sb_publishable_test'),
                                'user-jwt', transport=httpx.MockTransport(handler))
    assert client.rpc('fb_replace_transactions', {'p_account': None, 'p_rows': []}) == 2
    assert len(requests) == 1
    assert requests[0].method == 'POST'
    assert requests[0].url.path.endswith('/rest/v1/rpc/fb_replace_transactions')
    assert requests[0].headers['authorization'] == 'Bearer user-jwt'
    assert json.loads(requests[0].content)['p_rows'] == []


def test_replacements_and_split_use_single_atomic_rpc():
    client = Mock()
    client.rpc.return_value = 1
    repo = SupabaseTransactionRepository(client, 'alice')
    assert repo.replace_account('Checking', [sample_transaction()]) == 1
    assert client.rpc.call_args.args[0] == 'fb_replace_transactions'
    assert client.rpc.call_args.args[1]['p_rows'][0]['user_id'] == 'alice'
    client.reset_mock()
    with pytest.raises(ValueError):
        repo.replace_account('Checking', [sample_transaction(account='Other')])
    client.assert_not_called()
    repo.replace_all([sample_transaction()])
    assert client.rpc.call_args.args[1]['p_account'] is None
    client.reset_mock()
    repo.split_transaction('a', 'Food', 2.0, 'Dining', 2.5)
    client.rpc.assert_called_once()
    assert client.rpc.call_args.args[0] == 'fb_split_transaction'


def test_restore_and_plaid_accounts_never_issue_partial_client_writes():
    client = Mock()
    client.rpc.return_value = {'transactions': 1, 'budgets': 0, 'goals': 0, 'category_rules': 0}
    repo = SupabaseTransactionRepository(client, 'alice')
    backup = json.dumps({'version': 1, 'transactions': [sample_transaction().model_dump(mode='json')],
                         'budgets': [], 'goals': [], 'category_rules': []}).encode()
    assert repo.restore_backup(backup)['transactions'] == 1
    client.rpc.assert_called_once()
    assert client.rpc.call_args.args[0] == 'fb_restore_backup'
    client.reset_mock()
    SupabasePlaidRepository(client, 'alice').save_accounts('bank-1', [{'account_id': 'acct-1', 'name': 'Checking'}])
    client.rpc.assert_called_once()
    assert client.rpc.call_args.args[0] == 'fb_replace_plaid_accounts'


def test_undo_append_only_sends_imported_rows():
    client = Mock()
    client.rpc.return_value = 1
    repo = SupabaseTransactionRepository(client, 'alice')
    assert repo.undo_append([sample_transaction()]) == 1
    assert client.rpc.call_args.args[0] == 'fb_undo_append'
    assert [row['id'] for row in client.rpc.call_args.args[1]['p_rows']] == ['a']


def test_plaid_repository_uses_private_token_rpc():
    client = Mock()
    client.select.return_value = [{'item_id': 'bank-1', 'institution_name': 'Bank', 'cursor': None,
                                   'status': 'connected', 'last_synced_at': None, 'error_message': None}]
    client.rpc.return_value = 'encrypted-token'
    repo = SupabasePlaidRepository(client, 'alice')
    repo.save_item('bank-1', 'encrypted-token', 'Bank')
    assert client.rpc.call_args.args[0] == 'fb_save_plaid_item'
    assert repo.get_items()[0].access_token == 'encrypted-token'
    assert client.rpc.call_args.args == ('fb_plaid_token', {'p_item_id': 'bank-1'})


def test_plaid_sync_is_one_rpc_with_expected_cursor():
    client = Mock()
    client.rpc.return_value = {'upserted': 1, 'removed': 0}
    result = SupabaseTransactionRepository(client, 'alice').apply_plaid_sync(
        'item-1', 'previous', 'next', [sample_transaction('plaid:1')], [], '2026-09-23T00:00:00Z'
    )
    assert result == {'upserted': 1, 'removed': 0}
    client.rpc.assert_called_once()
    assert client.rpc.call_args.args[0] == 'fb_apply_plaid_sync'
    assert client.rpc.call_args.args[1]['p_expected_cursor'] == 'previous'
