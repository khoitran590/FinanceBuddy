from datetime import date

from src.domain.models import Transaction
from src.repositories.plaid_repo import PlaidRepository
from src.repositories.transaction_repo import TransactionRepository
from src.services.plaid_service import PlaidConfig, PlaidService, TokenCipher


def test_plaid_repository_round_trips_item_accounts_and_sync_state(tmp_path):
    repo = PlaidRepository(str(tmp_path / "finance.db"))
    repo.save_item("item-1", "encrypted", "Trial Bank")
    repo.save_accounts(
        "item-1",
        [
            {
                "account_id": "account-1",
                "name": "Checking",
                "official_name": "Everyday Checking",
                "type": "depository",
                "subtype": "checking",
                "mask": "1234",
            }
        ],
    )
    repo.update_sync("item-1", "cursor-1", "2026-09-20T12:00:00+00:00")

    item = repo.get_item("item-1")
    assert item is not None
    assert item.cursor == "cursor-1"
    assert repo.get_accounts("item-1")[0]["official_name"] == "Everyday Checking"


def test_token_cipher_round_trip_and_config_sources():
    config = PlaidConfig(
        client_id="client", secret="secret", token_encryption_key="stable-private-key"
    )
    cipher = TokenCipher(config)
    encrypted = cipher.encrypt("access-production-123")

    assert encrypted != "access-production-123"
    assert cipher.decrypt(encrypted) == "access-production-123"


def test_synced_transaction_upsert_preserves_manual_category(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    original = Transaction(
        id="plaid:transaction-1",
        date=date(2026, 9, 1),
        description="Coffee shop",
        amount=-5.0,
        category="Dining",
        account_name="Checking ••••1234",
    )
    repo.insert_many([original])
    repo.update_category(original.id, "Entertainment")

    repo.upsert_many(
        [original.model_copy(update={"amount": -6.0, "category": "Dining"})]
    )

    updated = repo.get_all()[0]
    assert updated.amount == -6.0
    assert updated.category == "Entertainment"


def test_plaid_transaction_normalization_maps_sign_account_and_category():
    transaction = PlaidService._to_transaction(
        {
            "transaction_id": "transaction-1",
            "account_id": "account-1",
            "date": "2026-09-20",
            "name": "Neighborhood Market",
            "merchant_name": "Neighborhood Market",
            "amount": 42.5,
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_GROCERIES",
            },
        },
        {
            "account-1": {
                "name": "Checking",
                "official_name": "Everyday Checking",
                "type": "depository",
                "mask": "1234",
            }
        },
    )

    assert transaction.id == "plaid:transaction-1"
    assert transaction.amount == -42.5
    assert transaction.category == "Groceries"
    assert transaction.account_name == "Everyday Checking ••••1234"


def test_plaid_transaction_keeps_provider_detail_and_card_payments_are_transfers():
    transaction = PlaidService._to_transaction(
        {
            "transaction_id": "transaction-2",
            "account_id": "account-1",
            "date": "2026-09-21",
            "name": "SQ *BLUE BOTTLE",
            "merchant_name": "Blue Bottle",
            "amount": 5.25,
            "pending": True,
            "payment_channel": "in store",
            "location": {"city": "Oakland", "region": "CA", "address": None},
            "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE"},
        },
        {"account-1": {"name": "Card", "type": "credit", "mask": "9999"}},
    )

    assert transaction.merchant_name == "Blue Bottle"
    assert transaction.subcategory == "Coffee"
    assert transaction.payment_channel == "in store"
    assert transaction.location == "Oakland, CA"
    assert transaction.pending is True
    assert transaction.account_type == "Credit Card"
    assert PlaidService._map_category(
        {"personal_finance_category": {"primary": "LOAN_PAYMENTS", "detailed": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"}}
    ) == "Credit Card Payments"


def test_plaid_repository_stores_balances_and_daily_history(tmp_path):
    repo = PlaidRepository(str(tmp_path / "finance.db"))
    repo.save_item("item-1", "encrypted", "Trial Bank")
    account = {
        "account_id": "card-1",
        "name": "Card",
        "type": "credit",
        "balances": {"current": 420.5, "available": 1579.5, "limit": 2000, "iso_currency_code": "USD"},
    }
    repo.save_accounts("item-1", [account])
    account["balances"]["current"] = 500.0
    repo.save_accounts("item-1", [account, {"account_id": "no-balance", "name": "X", "type": "depository"}])

    stored = {row["account_id"]: row for row in repo.get_accounts("item-1")}
    assert stored["card-1"]["current_balance"] == 500.0
    assert stored["card-1"]["credit_limit"] == 2000.0
    assert stored["no-balance"]["current_balance"] is None
    history = repo.get_balance_history()
    assert [(row["account_id"], row["current_balance"]) for row in history] == [("card-1", 500.0)]


def test_sync_refreshes_balances_without_failing_when_balances_are_unavailable(tmp_path):
    class FakeClient:
        def __init__(self, balances_fail=False):
            self.balances_fail = balances_fail

        def transactions_sync(self, request):
            return {"added": [], "modified": [], "removed": [], "next_cursor": "c1", "has_more": False}

        def accounts_get(self, request):
            if self.balances_fail:
                raise RuntimeError("unavailable")
            return {"accounts": [{"account_id": "a", "name": "Checking", "type": "depository",
                                  "balances": {"current": 10.0, "available": 9.0}}]}

    config = PlaidConfig(client_id="client", secret="secret", token_encryption_key="key")
    plaid_repo = PlaidRepository(str(tmp_path / "finance.db"))
    transactions = TransactionRepository(str(tmp_path / "finance.db"))
    plaid_repo.save_item("item-1", TokenCipher(config).encrypt("access"), "Bank")

    result = PlaidService(config, plaid_repo, transactions, client=FakeClient()).sync_item("item-1")
    assert result["balances_updated"] is True
    assert plaid_repo.get_accounts("item-1")[0]["available_balance"] == 9.0

    failed = PlaidService(config, plaid_repo, transactions, client=FakeClient(True)).sync_item("item-1")
    assert failed["balances_updated"] is False
    assert plaid_repo.get_item("item-1").cursor == "c1"
