"""Check a single Amount column's sign convention against words that reveal direction.

This only advises. The parser never guesses a convention from keywords; the
person confirms or flips it before saving.
"""
from decimal import Decimal

from src.services.categorizer import CATEGORY_RULES

# Words that mean money arrived in this kind of account.
MONEY_IN_WORDS = {
    "Checking": tuple(CATEGORY_RULES["Salary/Income"]) + ("refund",),
    "Credit Card": tuple(CATEGORY_RULES["Credit Card Payments"]) + ("refund",),
}


def sign_evidence(transactions, account_type: str) -> dict:
    words = MONEY_IN_WORDS.get(account_type, MONEY_IN_WORDS["Checking"])
    agree = disagree = 0
    for item in transactions:
        if any(word in item.description.lower() for word in words):
            agree += item.amount > 0
            disagree += item.amount < 0
    money_out = [item.amount for item in transactions if item.amount < 0]
    money_in = [item.amount for item in transactions if item.amount > 0]
    return {
        "money_out": len(money_out), "out_total": -sum(money_out, Decimal(0)),
        "money_in": len(money_in), "in_total": sum(money_in, Decimal(0)),
        # Deposits/payments read as money out mean the signs are probably reversed.
        "looks_reversed": disagree > agree,
        "confirmed": agree > disagree,
    }
