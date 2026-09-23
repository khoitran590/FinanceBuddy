from datetime import date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


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
