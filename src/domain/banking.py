"""Provider-neutral bank connection records and USD balance validation."""
from dataclasses import dataclass
from typing import Optional
from src.domain.money import money, require_usd


@dataclass(frozen=True)
class PlaidItem:
    item_id: str
    access_token: str
    institution_name: str
    cursor: Optional[str]
    status: str
    last_synced_at: Optional[str]
    error_message: Optional[str]
    sync_generation: int = 0
    needs_reconciliation: bool = False
    disconnect_remove_transactions: bool = False


def account_balances(account: dict) -> dict:
    balances = account.get('balances') or account
    currency = require_usd(balances.get('iso_currency_code'), balances.get('unofficial_currency_code'))
    def number(key):
        value = balances.get(key)
        return str(money(value)) if value is not None else None
    return {'current_balance': number('current'), 'available_balance': number('available'),
            'credit_limit': number('limit'), 'iso_currency_code': currency}
