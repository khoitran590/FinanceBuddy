"""Versioned portable snapshots share validation and limits on both directions."""
import json
from datetime import date
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field
from src.domain.models import Transaction, Budget, SavingsGoal, CategoryRule, StatementImportRow
from src.domain.money import Money, rounded_money
from src.domain.allocations import recover_legacy_splits
from src.services.security import read_backup, MAX_UPLOAD_BYTES


class BackupAccount(BaseModel):
    id: UUID
    name: str = Field(min_length=1, max_length=256)
    account_type: str = Field(min_length=1, max_length=64)
    currency: Literal['USD'] = 'USD'
    institution_name: str | None = Field(default=None, min_length=1, max_length=256)
    plaid_item_id: str | None = Field(default=None, max_length=256)
    plaid_account_id: str | None = Field(default=None, max_length=256)
    statement_key: str | None = Field(default=None, max_length=512)


class BalanceSnapshot(BaseModel):
    account_id: UUID
    as_of: date
    type: str = Field(min_length=1, max_length=64)
    current_balance: Money
    available_balance: Money | None = None
    credit_limit: Money | None = None
    currency: Literal['USD'] = 'USD'


class BackupSetting(BaseModel):
    key: str = Field(min_length=1, max_length=256)
    value: str = Field(max_length=16_384)


def validate_backup(raw: bytes) -> dict:
    data = read_backup(raw)
    legacy = data['version'] < 3
    if data.get('currency', 'USD') != 'USD':
        raise ValueError('Only USD backups are supported.')
    # Legacy float exports have an explicit HALF_UP conversion, never silent loss.
    if legacy:
        for collection, fields in [('transactions', ['amount']), ('budgets', ['monthly_limit']),
                                   ('goals', ['target_amount', 'current_amount'])]:
            for row in data[collection]:
                for field in fields:
                    if field in row:
                        row[field] = str(rounded_money(row[field]))
                for allocation in row.get('allocations', []):
                    allocation['amount'] = str(rounded_money(allocation['amount']))
    transactions = [Transaction.model_validate(row) for row in data['transactions']]
    if legacy:
        transactions = [item.model_copy(update={'account_id': None}) for item in transactions]
    if data['version'] == 1:
        transactions = recover_legacy_splits(transactions)
    result = {'version': 2 if legacy else 3, 'currency': 'USD',
              'transactions': [item.model_dump(mode='json') for item in transactions]}
    for collection, model in [('budgets', Budget), ('goals', SavingsGoal), ('category_rules', CategoryRule),
                              ('statement_import_rows', StatementImportRow)]:
        result[collection] = [model.model_validate(row).model_dump(mode='json') for row in data.get(collection, [])]
    if not legacy:
        for collection, model in [('accounts', BackupAccount), ('balance_snapshots', BalanceSnapshot), ('settings', BackupSetting)]:
            result[collection] = [model.model_validate(row).model_dump(mode='json') for row in data[collection]]
        accounts = {row['id'] for row in result['accounts']}
        if len(accounts) != len(result['accounts']) or any(str(item.account_id) not in accounts for item in transactions):
            raise ValueError('Backup transactions must reference distinct owned accounts.')
        if any(row['account_id'] not in accounts for row in result['balance_snapshots']):
            raise ValueError('Backup balance history refers to an unknown account.')
    if not legacy:
        # Preserve the portable snapshot shape and decimal strings, so expanding
        # model defaults cannot make a valid exported file exceed server limits.
        result = data
    serialize_backup(result)
    return result


def serialize_backup(data) -> str:
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'), default=str)
    read_backup(text.encode('utf-8'))
    if len(text.encode('utf-8')) > MAX_UPLOAD_BYTES:
        raise ValueError('Backup exceeds the 10 MB restore limit.')
    return text
