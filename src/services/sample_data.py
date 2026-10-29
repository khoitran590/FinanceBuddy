"""Six months of realistic, repeatable activity for exploring FinanceBuddy.

Sample rows live in two reserved statement accounts so clearing them never
touches real data. The names cannot collide with a connected bank's accounts.
"""
from datetime import date
from decimal import Decimal

from src.domain.models import Budget, SavingsGoal

SAMPLE_SETTING = "sample_data"
CHECKING = ("Sample Checking · demo", "Checking")
CARD = ("Sample Card · demo", "Credit Card")
SAMPLE_BUDGETS = [
    Budget(category="Groceries", monthly_limit=350),
    Budget(category="Dining", monthly_limit=200),
    Budget(category="Transportation", monthly_limit=150),
]
SAMPLE_GOALS = [SavingsGoal(name="Emergency fund (sample)", target_amount=5000, current_amount=1200)]

# (day, description, amount, per-month change). Money out is negative.
CHECKING_ROWS = [
    (1, "ACME CORP PAYROLL", "2450.00", "0"),
    (15, "ACME CORP PAYROLL", "2450.00", "0"),
    (1, "MAPLE PROPERTY MANAGEMENT RENT", "-1650.00", "0"),
    (3, "STREAMFLIX", "-15.49", "0"),
    (8, "IRONWORKS GYM", "-45.00", "0"),
    (12, "CITY ELECTRIC", "-78.40", "-3.15"),
    (22, "CREDIT CARD PAYMENT", "-900.00", "0"),
]
CARD_ROWS = [
    (2, "TRADER JOE'S #552", "-64.12", "-1.37"),
    (9, "TRADER JOE'S #552", "-82.37", "2.11"),
    (16, "TRADER JOE'S #552", "-57.90", "-0.85"),
    (23, "TRADER JOE'S #552", "-91.45", "1.40"),
    (5, "BLUE BOTTLE COFFEE", "-5.75", "0"),
    (12, "BLUE BOTTLE COFFEE", "-5.75", "0"),
    (6, "CHIPOTLE 1234", "-13.85", "0"),
    (19, "CHIPOTLE 1234", "-13.85", "-0.40"),
    (10, "SQ *SUNSET NOODLE HOUSE", "-31.40", "-2.05"),
    (14, "SHELL OIL 57410", "-41.20", "-1.75"),
    (18, "AMZN Mktp US*2K3L91", "-27.99", "-5.00"),
    (27, "NEIGHBORHOOD HARDWARE CO", "-23.50", "-4.10"),
    (22, "PAYMENT THANK YOU", "900.00", "0"),
]


def _months(today: date, count: int = 6) -> list[date]:
    index = today.year * 12 + today.month - 1
    return [date((index - offset) // 12, (index - offset) % 12 + 1, 1) for offset in range(count - 1, -1, -1)]


def _statement(rows, today: date) -> bytes:
    lines = []
    for number, month in enumerate(_months(today)):
        for day, description, amount, change in rows:
            when = month.replace(day=day)
            if when <= today:
                lines.append((when, description, Decimal(amount) + Decimal(change) * number))
    lines.sort()
    return ("Date,Description,Amount\n" + "".join(
        f"{when.isoformat()},{description},{amount}\n" for when, description, amount in lines)).encode()


def sample_statements(today: date) -> list[tuple[str, str, bytes]]:
    """(account name, account type, CSV bytes) for each sample account, ending today."""
    return [(*CHECKING, _statement(CHECKING_ROWS, today)), (*CARD, _statement(CARD_ROWS, today))]
