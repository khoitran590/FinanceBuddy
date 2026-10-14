import json
from datetime import date
from decimal import Decimal
from uuid import uuid4
from unittest.mock import Mock

import httpx
import pytest

from src.domain.models import Transaction, TransactionAllocation, CategoryRule
from src.domain.money import money, rounded_money
from src.domain.allocations import allocations_match
from src.services.analytics import AnalyticsService
from src.services.backups import serialize_backup, validate_backup
from src.services.categorization import categorize
from src.services.data_service import DataService
from src.services.operations import OperationService
from src.services.parser import BankStatementParser
from src.services.plaid_service import PlaidService
from src.services.supabase import SupabaseConfig, SupabaseDataClient, SupabaseError
from src.repositories.supabase_repo import SupabaseTransactionRepository, SupabasePlaidRepository
from src.repositories.transaction_repo import TransactionRepository
from src.domain.banking import account_balances


def parse(raw, account):
    return BankStatementParser.parse(raw.encode("utf-8"), account)


def tx(**values):
    return Transaction(id='one', date=date(2026,10,1), description='Coffee', amount='-0.30',
                       account_name='Checking', **values)


@pytest.mark.parametrize('value', ['0.001', '-1.005', 'NaN', 'Infinity', '1000000000000', True])
def test_new_money_rejects_fractional_cents_and_nonfinite_or_excessive_values(value):
    with pytest.raises(ValueError):
        money(value)


def test_money_exact_sums_serialization_and_legacy_rounding_policy():
    assert money(0.1)+money(0.2) == Decimal('0.30')
    assert money(0.1+0.2) == Decimal('0.30')
    assert rounded_money('-1.005') == Decimal('-1.01')
    source=tx(allocations=[TransactionAllocation(amount='-0.10', category='Dining'),
                          TransactionAllocation(amount='-0.20', category='Shopping')])
    assert allocations_match(source)
    assert source.model_dump(mode='json')['amount'] == '-0.30'
    summary=AnalyticsService.calculate_summary([source]*1000)
    assert summary['outflow'] == Decimal('300.00')
    assert isinstance(summary['outflow'], Decimal)


@pytest.mark.parametrize('currency', ['EUR','CAD','GBP'])
def test_foreign_currency_is_rejected_by_provider_balances_and_transactions(currency):
    with pytest.raises(ValueError):
        account_balances({'balances': {'current': 1, 'iso_currency_code': currency}})
    with pytest.raises(ValueError):
        PlaidService._to_transaction({'transaction_id':'one','account_id':'a','date':'2026-10-01',
                                     'amount':1,'iso_currency_code':currency}, {'a':{'name':'Bank'}})
    with pytest.raises(ValueError):
        Transaction.model_validate({**tx().model_dump(mode='json'),'currency':currency})


@pytest.mark.parametrize('foreign_row', ['2026-10-01,Shop,-1.00,EUR','2026-10-01,Shop,€1.00,USD'])
def test_one_foreign_row_blocks_the_entire_statement_even_above_parse_accuracy_gate(foreign_row):
    raw='Date,Description,Amount,Currency\n'+('2026-10-01,Shop,-1.00,USD\n'*9)+foreign_row+'\n'
    rows, metrics=parse(raw, 'Checking')
    assert not metrics.is_valid and not rows
    assert any('USD' in error for error in metrics.errors)


def test_shared_category_policy_auto_updates_and_manual_or_legacy_choices_are_preserved():
    source=tx(category='Dining', category_origin='provider', provider_category='Dining')
    custom=categorize(source, [CategoryRule(keyword='coffee', category='Groceries', version=2)])
    assert (custom.category,custom.category_origin,custom.category_rule,custom.category_rule_version)==('Groceries','rule','coffee',2)
    assert categorize(custom, []).category=='Dining'
    changed=source.model_copy(update={'provider_category':'Shopping'})
    assert categorize(changed, []).category=='Shopping'
    manual=changed.model_copy(update={'category_override':'Entertainment'})
    assert categorize(manual,[CategoryRule(keyword='coffee',category='Groceries')]).category=='Entertainment'
    assert categorize(tx(category='Travel'),[CategoryRule(keyword='coffee',category='Groceries')]).category=='Travel'


def test_pending_lineage_is_captured_from_provider():
    source=PlaidService._to_transaction({'transaction_id':'posted','pending_transaction_id':'pending',
                                        'date':'2026-10-01','amount':1,'iso_currency_code':'USD'}, {})
    assert source.pending_transaction_id=='plaid:pending'
    assert source.amount==Decimal('-1.00')


def test_selected_account_identity_scopes_identical_physical_files():
    rows,_=parse('Date,Description,Amount\n2026-10-01,Shop,-1.00\n','Same name')
    first=DataService.bind_statement_account(rows,uuid4())
    second=DataService.bind_statement_account(rows,uuid4())
    assert first[0].id != second[0].id and first[0].statement_file_id != second[0].statement_file_id
    assert first[0].account_id != second[0].account_id


def test_write_timeout_after_commit_recovers_original_result_without_duplicate_write():
    requests=[]; committed={}
    def handler(request):
        requests.append(request)
        payload=json.loads(request.content)
        if request.url.path.endswith('fb_execute_operation'):
            committed[payload['p_operation_id']]={'result':{'saved':True}}
            raise httpx.ReadTimeout('lost response',request=request)
        return httpx.Response(200,json=committed.get(payload['p_operation_id']))
    client=SupabaseDataClient(SupabaseConfig('https://test.supabase.co','public'),'jwt',transport=httpx.MockTransport(handler))
    assert OperationService(client).execute('split',{'id':'one'})=={'saved':True}
    assert len(requests)==2 and len(committed)==1
    assert requests[0].headers['authorization']=='Bearer jwt'
    assert json.loads(requests[0].content)['p_operation_id']==json.loads(requests[1].content)['p_operation_id']


def test_write_timeout_before_commit_retries_the_same_operation_id():
    calls=[]
    def handler(request):
        payload=json.loads(request.content);calls.append(payload)
        if len(calls)==1:
            raise httpx.ReadTimeout('before commit',request=request)
        return httpx.Response(200,json=None if len(calls)==2 else {'saved':True})
    client=SupabaseDataClient(SupabaseConfig('https://test.supabase.co','public'),'jwt',transport=httpx.MockTransport(handler))
    assert OperationService(client).execute('split',{'id':'one'})=={'saved':True}
    assert len(calls)==3 and len({call['p_operation_id'] for call in calls})==1


def test_unresolved_operation_reuses_session_id_on_next_attempt():
    client=Mock(); pending={}
    client.rpc.side_effect=SupabaseError('uncertain',uncertain=True)
    service=OperationService(client,pending)
    with pytest.raises(SupabaseError) as caught:
        service.execute('split',{'id':'one'})
    identifier=client.rpc.call_args.args[1]['p_operation_id']
    assert caught.value.operation_id==identifier
    client.rpc.side_effect=None; client.rpc.return_value={'saved':True}
    assert OperationService(client,pending).execute('split',{'id':'one'})=={'saved':True}
    assert client.rpc.call_args.args[1]['p_operation_id']==identifier and pending=={}


def test_safe_error_codes_and_correlation_ids_do_not_expose_database_payloads():
    def handler(request):
        return httpx.Response(400,json={'code':'23514','message':'Private account and transaction details'})
    client=SupabaseDataClient(SupabaseConfig('https://test.supabase.co','public'),'jwt',transport=httpx.MockTransport(handler))
    with pytest.raises(SupabaseError) as caught:
        client.rpc('fb_execute_operation',{})
    assert caught.value.code=='23514' and caught.value.correlation_id
    assert 'Private' not in str(caught.value) and not caught.value.uncertain


def test_scalar_rpc_keeps_json_arrays_and_null_results():
    responses=iter([['a','b'],[],None])
    client=SupabaseDataClient(SupabaseConfig('https://test.supabase.co','public'),'jwt',transport=httpx.MockTransport(lambda request:httpx.Response(200,json=next(responses))))
    assert client.rpc('fb_import_batches',{})==['a','b']
    assert client.rpc('fb_import_batches',{})==[]
    assert client.rpc('fb_operation_result',{}) is None


def test_filtered_page_transfers_only_the_page_in_one_request():
    calls=[]
    def handler(request):
        payload=json.loads(request.content);calls.append(payload)
        assert payload['p_limit']==200 and payload['p_filters']['search']=='Coffee'
        return httpx.Response(200,json={'rows':[tx().model_dump(mode='json')]*200,'count':50_000,
                                       'next_cursor':{'date':'2026-10-01','id':'one'}})
    client=SupabaseDataClient(SupabaseConfig('https://test.supabase.co','public'),'jwt',transport=httpx.MockTransport(handler))
    page=SupabaseTransactionRepository(client,'owner').get_page({'search':'Coffee'})
    assert len(page['rows'])==200 and page['count']==50_000 and len(calls)==1


def test_statement_preview_requests_only_matching_candidates_and_preserves_metadata():
    rows,_=parse('Date,Description,Amount\n2026-10-01,Coffee,-1.00\n','Checking')
    client=Mock();client.rpc.side_effect=[{'accounts':[]}, {'transactions':[], 'mappings':[]}]
    plan=SupabaseTransactionRepository(client,'owner').preview_statement_import(rows)
    assert plan['inserted']==1
    assert client.rpc.call_args.args[0]=='fb_statement_candidates'
    client.select.assert_not_called()


def test_import_batch_is_durable_and_undo_requires_an_unchanged_revision(tmp_path):
    repo=TransactionRepository(str(tmp_path/'batch.db'))
    rows,_=parse('Date,Description,Amount\n2026-10-01,Coffee,-1.00\n','Checking')
    identifier=str(uuid4())
    first=repo.append_statement(rows,operation_id=identifier)
    assert repo.append_statement(rows,operation_id=identifier)['batch_id']==first['batch_id']
    reopened=TransactionRepository(str(tmp_path/'batch.db'))
    assert reopened.get_import_batches()[0]['id']==identifier
    reopened.update_category(rows[0].id,'Shopping')
    reopened.update_category(rows[0].id,rows[0].category)
    with pytest.raises(ValueError,match='changed'):
        reopened.undo_import_batch(identifier)
    assert len(reopened.get_all())==1


def test_import_undo_does_not_remove_overlapping_rows_owned_by_another_batch(tmp_path):
    repo=TransactionRepository(str(tmp_path/'batches.db'))
    first,_=parse('Date,Description,Amount\n2026-10-01,Coffee,-1.00\n','Checking')
    second,_=parse('Date,Description,Amount\n2026-10-01,Coffee,-1.00\n2026-10-01,Coffee,-1.00\n','Checking')
    batch_a=repo.append_statement(first);batch_b=repo.append_statement(second)
    assert batch_b['inserted']==1 and repo.undo_import_batch(batch_b['batch_id'])==1
    assert repo.get_all()[0].id==first[0].id
    with pytest.raises(ValueError, match='changed'):
        repo.undo_import_batch(batch_a['batch_id'])  # A newer file claimed this row.


def backup():
    identifier=str(uuid4());source=tx(account_id=identifier)
    return {'version':3,'currency':'USD','transactions':[source.model_dump(mode='json')],
            'budgets':[],'goals':[],'category_rules':[],'statement_import_rows':[],
            'accounts':[{'id':identifier,'name':'Checking','account_type':'Checking','currency':'USD'}],
            'settings':[{'key':'onboarding_complete','value':'true'}],
            'balance_snapshots':[{'account_id':identifier,'as_of':'2026-10-01','type':'depository','current_balance':'12.34','currency':'USD'}]}


def test_backup_v3_money_accounts_settings_and_history_validate_together():
    restored=validate_backup(serialize_backup(backup()).encode())
    assert restored['settings'][0]['value']=='true'
    assert restored['balance_snapshots'][0]['current_balance']=='12.34'
    assert restored['transactions'][0]['amount']=='-0.30'
    invalid=backup();invalid['transactions'][0]['account_id']=str(uuid4())
    with pytest.raises(ValueError,match='owned accounts'):
        validate_backup(json.dumps(invalid).encode())


@pytest.mark.parametrize('collection',['transactions','statement_import_rows','settings','balance_snapshots'])
def test_export_and_restore_enforce_the_same_collection_limit(collection):
    data=backup();data[collection]=[{}]*50_001
    with pytest.raises(ValueError,match='too many'):
        serialize_backup(data)


def test_export_refuses_a_snapshot_larger_than_restore_upload_limit():
    data=backup();data['settings']=[{'key':'large','value':'x'*(10*1024*1024)}]
    with pytest.raises(ValueError,match='10 MB'):
        serialize_backup(data)


def test_bank_cards_do_not_fetch_tokens_and_specific_sync_fetches_one_bank():
    client=Mock();client.select.return_value=[{'item_id':'b','institution_name':'Bank','status':'connected'}]
    repo=SupabasePlaidRepository(client,'owner')
    assert repo.get_items()[0].access_token==''
    client.rpc.assert_not_called();client.reset_mock();client.rpc.return_value='encrypted'
    assert repo.get_item('b').access_token=='encrypted'
    assert client.select.call_args.kwargs['item_id']=='eq.b'
    client.rpc.assert_called_once_with('fb_plaid_token',{'p_item_id':'b'})


def test_http_requests_close_their_clients_and_keep_owner_credentials_separate():
    class Transport(httpx.MockTransport):
        closed = 0
        def close(self):
            self.closed += 1
    tokens = []
    def handler(request):
        tokens.append(request.headers['authorization'])
        return httpx.Response(200, json=[])
    transport = Transport(handler)
    config = SupabaseConfig('https://test.supabase.co', 'public')
    SupabaseDataClient(config, 'alice', transport).rpc('fb_import_batches', {})
    SupabaseDataClient(config, 'bob', transport).rpc('fb_import_batches', {})
    assert transport.closed == 2 and tokens == ['Bearer alice', 'Bearer bob']


def test_statement_total_reconciliation_requires_exact_cents():
    raw = b'Date,Description,Amount\n2026-10-01,Shop,0.00\n'
    rows, metrics = BankStatementParser.parse(raw, 'Checking', expected_total=0.005)
    assert not rows and not metrics.is_valid
    assert 'exact USD cents' in metrics.errors[0]


def test_legacy_backup_nonfinite_money_fails_with_a_safe_validation_error():
    data = {'version': 2, 'transactions': [tx().model_dump(mode='json')], 'budgets': [], 'goals': [], 'category_rules': []}
    data['transactions'][0]['amount'] = 'Infinity'
    with pytest.raises(ValueError, match='USD'):
        validate_backup(json.dumps(data).encode())
