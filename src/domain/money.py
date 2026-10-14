"""USD money is exact to a cent; rounding is explicit at migration/estimate boundaries."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

CENT = Decimal("0.01")
MAX_MONEY = Decimal("999999999999.99")


def money(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) > MAX_MONEY:
            raise ValueError("Money must be finite and within the supported range.")
        rounded = result.quantize(CENT, rounding=ROUND_HALF_UP)
        # Permit only binary float noise from UI widgets, never a fractional cent.
        if result != rounded and not (isinstance(value, float) and abs(result - rounded) < Decimal("1e-9")):
            raise ValueError("USD amounts must have at most two decimal places.")
        return rounded
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Invalid USD amount.") from error


def rounded_money(value) -> Decimal:
    """Use only for explicitly rounded estimates or existing legacy values."""
    try:
        return money(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError) as error:
        raise ValueError("Invalid USD amount.") from error


def require_usd(currency="USD", unofficial=None) -> str:
    if unofficial or str(currency or "USD").upper() != "USD":
        raise ValueError("FinanceBuddy supports USD only. This currency cannot be imported or synced.")
    return "USD"


Money = Annotated[Decimal, BeforeValidator(money), PlainSerializer(str, when_used="json")]
