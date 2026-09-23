from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

from src.domain.models import Transaction
from src.repositories.plaid_repo import PlaidItem, PlaidRepository
from src.repositories.transaction_repo import TransactionRepository
from src.services.categorizer import auto_categorize


class PlaidConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class PlaidConfig:
    client_id: str
    secret: str
    environment: str = "production"
    country_codes: tuple[str, ...] = ("US",)
    redirect_uri: Optional[str] = None
    webhook_url: Optional[str] = None
    token_encryption_key: Optional[str] = None

    @classmethod
    def from_sources(cls, secrets: Optional[Mapping[str, Any]] = None) -> "PlaidConfig":
        values: dict[str, Any] = {}
        if secrets:
            nested = secrets.get("plaid", {}) if hasattr(secrets, "get") else {}
            for key in (
                "client_id",
                "secret",
                "environment",
                "country_codes",
                "redirect_uri",
                "webhook_url",
                "token_encryption_key",
            ):
                value = nested.get(key) if hasattr(nested, "get") else None
                if value not in (None, ""):
                    values[key] = value

        aliases = {
            "client_id": "PLAID_CLIENT_ID",
            "secret": "PLAID_SECRET",
            "environment": "PLAID_ENV",
            "country_codes": "PLAID_COUNTRY_CODES",
            "redirect_uri": "PLAID_REDIRECT_URI",
            "webhook_url": "PLAID_WEBHOOK_URL",
            "token_encryption_key": "PLAID_TOKEN_ENCRYPTION_KEY",
        }
        for key, env_name in aliases.items():
            if os.getenv(env_name):
                values[key] = os.environ[env_name]

        countries = values.get("country_codes", "US")
        if isinstance(countries, str):
            countries = tuple(value.strip().upper() for value in countries.split(",") if value.strip())
        else:
            countries = tuple(str(value).upper() for value in countries)
        return cls(
            client_id=str(values.get("client_id", "")),
            secret=str(values.get("secret", "")),
            environment=str(values.get("environment", "production")).lower(),
            country_codes=countries or ("US",),
            redirect_uri=values.get("redirect_uri") or None,
            webhook_url=values.get("webhook_url") or None,
            token_encryption_key=values.get("token_encryption_key") or None,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.secret)

    def validate(self) -> None:
        if not self.is_configured:
            raise PlaidConfigurationError("PLAID_CLIENT_ID and PLAID_SECRET are required.")
        if self.environment not in {"sandbox", "production"}:
            raise PlaidConfigurationError("PLAID_ENV must be sandbox or production.")


class TokenCipher:
    """Encrypt Plaid access tokens before they are stored in Supabase."""

    def __init__(self, config: PlaidConfig) -> None:
        material = config.token_encryption_key or f"{config.client_id}:{config.secret}"
        digest = hashlib.sha256(material.encode("utf-8")).digest()
        self._key = base64.urlsafe_b64encode(digest)

    def encrypt(self, value: str) -> str:
        from cryptography.fernet import Fernet

        return Fernet(self._key).encrypt(value.encode("utf-8")).decode("utf-8")

    def decrypt(self, value: str) -> str:
        from cryptography.fernet import Fernet, InvalidToken

        try:
            return Fernet(self._key).decrypt(value.encode("utf-8")).decode("utf-8")
        except InvalidToken as error:
            raise PlaidConfigurationError(
                "The Plaid token encryption key changed. Restore the previous key or reconnect the bank."
            ) from error


class PlaidService:
    def __init__(
        self,
        config: PlaidConfig,
        plaid_repo: PlaidRepository,
        transaction_repo: TransactionRepository,
        client: Any = None,
    ) -> None:
        config.validate()
        self.config = config
        self.plaid_repo = plaid_repo
        self.transaction_repo = transaction_repo
        self.cipher = TokenCipher(config)
        self.client = client or self._build_client()

    def _build_client(self):
        import plaid
        from plaid.api import plaid_api

        hosts = {
            "sandbox": plaid.Environment.Sandbox,
            "production": plaid.Environment.Production,
        }
        configuration = plaid.Configuration(
            host=hosts[self.config.environment],
            api_key={"clientId": self.config.client_id, "secret": self.config.secret},
        )
        return plaid_api.PlaidApi(plaid.ApiClient(configuration))

    @staticmethod
    def _serialized(value: Any) -> dict:
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_dict"):
            return value.to_dict()
        return json.loads(json.dumps(value, default=lambda item: item.__dict__))

    def create_link_token(self, client_user_id: str, access_token: str | None = None) -> str:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.products import Products

        request_values: dict[str, Any] = {
            "user": LinkTokenCreateRequestUser(client_user_id=client_user_id),
            "client_name": "FinanceBuddy",
            "country_codes": [CountryCode(code) for code in self.config.country_codes],
            "language": "en",
        }
        if access_token:
            request_values["access_token"] = access_token
        else:
            request_values["products"] = [Products("transactions")]
        if self.config.redirect_uri:
            request_values["redirect_uri"] = self.config.redirect_uri
        if self.config.webhook_url:
            request_values["webhook"] = self.config.webhook_url
        response = self.client.link_token_create(LinkTokenCreateRequest(**request_values))
        return self._serialized(response)["link_token"]

    def exchange_public_token(self, public_token: str, metadata: Optional[dict] = None) -> PlaidItem:
        from plaid.model.accounts_get_request import AccountsGetRequest
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

        exchange = self._serialized(
            self.client.item_public_token_exchange(
                ItemPublicTokenExchangeRequest(public_token=public_token)
            )
        )
        access_token = exchange["access_token"]
        item_id = exchange["item_id"]
        institution = (metadata or {}).get("institution") or {}
        institution_name = institution.get("name") or "Connected institution"
        self.plaid_repo.save_item(item_id, self.cipher.encrypt(access_token), institution_name)

        accounts_response = self._serialized(
            self.client.accounts_get(AccountsGetRequest(access_token=access_token))
        )
        self.plaid_repo.save_accounts(item_id, accounts_response.get("accounts", []))
        return self.plaid_repo.get_item(item_id)  # type: ignore[return-value]

    def sync_item(self, item_id: str) -> dict[str, int]:
        from plaid.model.transactions_sync_request import TransactionsSyncRequest

        item = self.plaid_repo.get_item(item_id)
        if not item:
            raise ValueError("Connected institution was not found.")
        access_token = self.cipher.decrypt(item.access_token)
        starting_cursor = item.cursor
        cursor = starting_cursor
        added: list[dict] = []
        modified: list[dict] = []
        removed: list[dict] = []
        pagination_restarts = 0

        while True:
            try:
                request_args: dict[str, Any] = {"access_token": access_token}
                if cursor:
                    request_args["cursor"] = cursor
                response = self._serialized(
                    self.client.transactions_sync(TransactionsSyncRequest(**request_args))
                )
            except Exception as error:
                if self._error_code(error) == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                    pagination_restarts += 1
                    if pagination_restarts > 3:
                        self.plaid_repo.set_error(
                            item_id, "Plaid transaction data kept changing during sync. Try again shortly."
                        )
                        raise
                    cursor = starting_cursor
                    added, modified, removed = [], [], []
                    continue
                self.plaid_repo.set_error(item_id, self._friendly_error(error))
                raise
            added.extend(response.get("added", []))
            modified.extend(response.get("modified", []))
            removed.extend(response.get("removed", []))
            cursor = response["next_cursor"]
            if not response.get("has_more", False):
                break

        account_lookup = {
            account["account_id"]: account for account in self.plaid_repo.get_accounts(item_id)
        }
        upserts = [self._to_transaction(value, account_lookup) for value in added + modified]
        removed_ids = [f"plaid:{value['transaction_id']}" for value in removed]
        synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if hasattr(self.transaction_repo, "apply_plaid_sync"):
            result = self.transaction_repo.apply_plaid_sync(
                item_id, starting_cursor, cursor, upserts, removed_ids, synced_at
            )
            removed_count = result["removed"]
        else:
            # The legacy SQLite repository already wraps each individual write
            # but remains a local-only development path.
            self.transaction_repo.upsert_many(upserts)
            removed_count = self.transaction_repo.delete_many(removed_ids)
            self.plaid_repo.update_sync(item_id, cursor, synced_at)
        return {
            "added": len(added),
            "modified": len(modified),
            "removed": removed_count,
            "balances_updated": self.refresh_balances(item_id, access_token),
        }

    def refresh_balances(self, item_id: str, access_token: str) -> bool:
        """Store the latest cached balances Plaid returns with account metadata."""
        from plaid.model.accounts_get_request import AccountsGetRequest

        try:
            response = self._serialized(
                self.client.accounts_get(AccountsGetRequest(access_token=access_token))
            )
            self.plaid_repo.save_accounts(item_id, response.get("accounts", []))
        except Exception:
            # Transactions are already committed; stale balances should not fail the sync.
            return False
        return True

    def remove_item(self, item_id: str, remove_transactions: bool = False) -> int:
        from plaid.model.item_remove_request import ItemRemoveRequest

        item = self.plaid_repo.get_item(item_id)
        if not item:
            return 0
        account_names = [
            self._account_name(value) for value in self.plaid_repo.get_accounts(item_id)
        ]
        self.client.item_remove(
            ItemRemoveRequest(access_token=self.cipher.decrypt(item.access_token))
        )
        deleted = 0
        if remove_transactions:
            for account_name in account_names:
                deleted += self.transaction_repo.delete_account(account_name)
        self.plaid_repo.delete_item(item_id)
        return deleted

    def update_link_token(self, item_id: str, client_user_id: str) -> str:
        item = self.plaid_repo.get_item(item_id)
        if not item:
            raise ValueError("Connected institution was not found.")
        return self.create_link_token(client_user_id, self.cipher.decrypt(item.access_token))

    @staticmethod
    def _error_code(error: Exception) -> str:
        body = getattr(error, "body", "")
        try:
            payload = json.loads(body)
            return payload.get("error_code", "") if isinstance(payload, dict) else ""
        except (TypeError, json.JSONDecodeError):
            return ""

    @classmethod
    def _friendly_error(cls, error: Exception) -> str:
        messages = {
            "ITEM_LOGIN_REQUIRED": "Reconnect this bank to continue syncing.",
            "INSTITUTION_DOWN": "Your bank is temporarily unavailable. Please try again later.",
            "RATE_LIMIT_EXCEEDED": "Too many requests. Wait a moment before trying again.",
        }
        return messages.get(cls._error_code(error), "The bank request could not be completed. Please try again later.")

    @staticmethod
    def _account_name(account: dict) -> str:
        label = account.get("official_name") or account.get("name") or "Connected account"
        return f"{label} ••••{account['mask']}" if account.get("mask") else label

    @classmethod
    def _to_transaction(cls, value: dict, accounts: dict[str, dict]) -> Transaction:
        account = accounts.get(value.get("account_id"), {})
        description = value.get("merchant_name") or value.get("name") or "Transaction"
        raw_date = value.get("authorized_date") or value.get("date")
        if isinstance(raw_date, date):
            transaction_date = raw_date
        else:
            transaction_date = date.fromisoformat(str(raw_date))
        plaid_type = str(account.get("type", ""))
        account_type = "Credit Card" if plaid_type == "credit" else "Checking"
        category = cls._map_category(value) or auto_categorize(description)
        location = value.get("location") or {}
        place = ", ".join(
            str(part) for part in (location.get("city"), location.get("region")) if part
        )
        return Transaction(
            id=f"plaid:{value['transaction_id']}",
            date=transaction_date,
            description=description,
            amount=-float(value.get("amount", 0.0)),
            category=category,
            account_name=cls._account_name(account),
            account_type=account_type,
            merchant_name=(value.get("merchant_name") or None),
            subcategory=cls._subcategory(value),
            payment_channel=(str(value.get("payment_channel") or "")[:32] or None),
            location=place[:256] or None,
            pending=bool(value.get("pending", False)),
        )

    @staticmethod
    def _subcategory(value: dict) -> Optional[str]:
        """Turn Plaid's detailed category code into a short readable label."""
        pfc = value.get("personal_finance_category") or {}
        primary = str(pfc.get("primary") or "").upper()
        detailed = str(pfc.get("detailed") or "").upper()
        if not detailed:
            return None
        if primary and detailed.startswith(primary + "_"):
            detailed = detailed[len(primary) + 1:]
        label = detailed.replace("_", " ").strip().capitalize()
        return label[:128] or None

    @staticmethod
    def _map_category(value: dict) -> Optional[str]:
        pfc = value.get("personal_finance_category") or {}
        primary = str(pfc.get("primary") or "").upper()
        mapping = {
            "INCOME": "Salary/Income",
            "RENT_AND_UTILITIES": "Housing",
            "LOAN_PAYMENTS": "Debt Payments",
            "FOOD_AND_DRINK": "Dining",
            "TRANSPORTATION": "Transportation",
            "MEDICAL": "Health & Wellness",
            "GENERAL_MERCHANDISE": "Shopping",
            "ENTERTAINMENT": "Entertainment",
            "TRAVEL": "Travel",
            "GOVERNMENT_AND_NON_PROFIT": "Gifts & Donations",
            "PERSONAL_CARE": "Health & Wellness",
            "BANK_FEES": "Fees & Interest",
            "CASH_ADVANCE": "Cash & ATM",
            "TRANSFER_IN": "Transfers",
            "TRANSFER_OUT": "Transfers",
        }
        detailed = str(pfc.get("detailed") or "").upper()
        if "CREDIT_CARD_PAYMENT" in detailed:
            return "Credit Card Payments"
        if "GROCER" in detailed:
            return "Groceries"
        return mapping.get(primary)
