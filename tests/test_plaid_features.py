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
