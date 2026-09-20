from datetime import date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Transaction(BaseModel):
    """A normalized transaction record shared by storage, analytics, and UI.

    The fields intentionally map cleanly to the Swift ``Transaction`` model
    described in the project guide.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: str
    date: date
    description: str
    amount: float
    category: str = "Uncategorized"
    account_name: str
    account_type: str = "Checking"


class Budget(BaseModel):
    category: str
    monthly_limit: float


class SavingsGoal(BaseModel):
    id: Optional[int] = None
    name: str
    target_amount: float
    current_amount: float = 0.0
    target_date: Optional[date] = None


class CategoryRule(BaseModel):
    keyword: str
    category: str


class ParseMetrics(BaseModel):
    total_rows: int
    valid_rows: int
    skipped_rows: int
    accuracy_rate: float
    errors: List[str] = Field(default_factory=list)
    is_valid: bool
