from datetime import date
from typing import List, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TransactionAllocation(BaseModel):
    """A user category allocation; the source transaction remains authoritative."""

    model_config = ConfigDict(validate_assignment=True, allow_inf_nan=False)

    id: UUID = Field(default_factory=uuid4)
    amount: float
    category: str = Field(min_length=1, max_length=128)

    @field_validator("amount")
    @classmethod
    def nonzero_amount(cls, value: float) -> float:
        if value == 0:
            raise ValueError("Allocation amount must be nonzero.")
        return value


class StatementImportRow(BaseModel):
    file_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    row_number: int = Field(ge=1, le=50_000)
    transaction_id: str = Field(min_length=1, max_length=256)


class Transaction(BaseModel):
    """A normalized transaction record shared by storage, analytics, and UI.

    The fields intentionally map cleanly to the Swift ``Transaction`` model
    described in the project guide.
    """

    model_config = ConfigDict(validate_assignment=True, allow_inf_nan=False)

    id: str = Field(min_length=1, max_length=256)
    date: date
    description: str = Field(min_length=1, max_length=2048)
    amount: float
    category: str = Field(default="Uncategorized", min_length=1, max_length=128)
    account_name: str = Field(min_length=1, max_length=256)
    account_type: str = Field(default="Checking", min_length=1, max_length=64)
    # Optional provider detail. Bank connections supply these; statement imports
    # leave them empty, so analytics must treat every one of them as optional.
    merchant_name: Optional[str] = Field(default=None, max_length=256)
    subcategory: Optional[str] = Field(default=None, max_length=128)
    payment_channel: Optional[str] = Field(default=None, max_length=32)
    location: Optional[str] = Field(default=None, max_length=256)
    pending: bool = False
    plaid_item_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    plaid_account_id: Optional[str] = Field(default=None, min_length=1, max_length=256)
    allocations: List[TransactionAllocation] = Field(default_factory=list, max_length=100)
    statement_file_id: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    statement_row: Optional[int] = Field(default=None, ge=1, le=50_000)
    statement_reference: Optional[str] = Field(default=None, min_length=1, max_length=256)
    statement_match_bank: bool = False
    # Read projection only. Never written into a backup or source record.
    source_transaction_id: Optional[str] = Field(default=None, exclude=True)
    source_amount: Optional[float] = Field(default=None, exclude=True)
    split_needs_review: bool = Field(default=False, exclude=True)


class Budget(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    category: str = Field(min_length=1, max_length=128)
    monthly_limit: float = Field(ge=0)


class SavingsGoal(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    id: Optional[int] = None
    name: str = Field(min_length=1, max_length=256)
    target_amount: float = Field(gt=0)
    current_amount: float = Field(default=0.0, ge=0)
    target_date: Optional[date] = None


class CategoryRule(BaseModel):
    keyword: str = Field(min_length=1, max_length=256)
    category: str = Field(min_length=1, max_length=128)


class ParseMetrics(BaseModel):
    total_rows: int
    valid_rows: int
    skipped_rows: int
    accuracy_rate: float
    errors: List[str] = Field(default_factory=list)
    is_valid: bool
