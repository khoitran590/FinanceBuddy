import pytest

from src.services.analytics import clean_merchant
from src.services.merchants import keyword_warning, rule_keyword


@pytest.mark.parametrize("description, expected", [
    ("AMZN Mktp US*2K3L91", "amzn mktp us"),
    ("TST* JOE'S PIZZA 0423 NEW YORK NY", "joe's pizza"),
    ("POS DEBIT COSTCO WHSE #0421 SAN JOSE CA", "costco whse"),
    ("BLUE BOTTLE COFFEE OAKLAND CA", "blue bottle coffee"),
])
def test_rule_keyword_is_always_a_literal_substring(description, expected):
    keyword = rule_keyword([description])
    assert keyword == expected
    assert keyword in description.lower()


def test_cleaned_display_names_are_unchanged():
    assert clean_merchant("AMZN Mktp US*2K3L91") == "Amazon"
    assert clean_merchant("TST* JOE'S PIZZA 0423 NEW YORK NY") == "JOE'S PIZZA NEW YORK NY"


def test_rule_keyword_covers_every_description_or_returns_none():
    assert rule_keyword(["BLUE BOTTLE COFFEE OAKLAND CA", "BLUE BOTTLE COFFEE SF CA"]) == "blue bottle coffee"
    assert rule_keyword(["PAYPAL *12345678", "VENMO PAYMENT 99887766"]) is None
    assert rule_keyword(["Starbucks"], merchant="Starbucks") == "starbucks"
    # A provider merchant name that is not in the memo cannot become a rule.
    assert rule_keyword(["SBUX 1234 SEATTLE"], merchant="Starbucks") == "sbux"


def test_keyword_warning_flags_processors_and_broad_phrases():
    assert "payment method" in keyword_warning("PayPal", 1, 100)
    assert "matches 30 of 100" in keyword_warning("coffee", 30, 100)
    assert keyword_warning("coffee", 3, 4) is None
    assert keyword_warning("blue bottle", 5, 100) is None


def test_uncategorized_groups_keep_branches_together_by_rule_phrase():
    from datetime import date
    from decimal import Decimal
    from src.domain.models import Transaction
    from src.services.merchants import uncategorized_groups

    def row(identifier, description, amount, category="Uncategorized"):
        return Transaction(id=identifier, date=date(2026, 10, 1), description=description, amount=amount,
                           category=category, account_name="Card")

    groups = uncategorized_groups([
        row("a", "BLUE BOTTLE COFFEE OAKLAND CA", "-4.50"), row("b", "BLUE BOTTLE COFFEE #12345 SF", "-5.25"),
        row("c", "CORNER STORE", "-3.00"), row("d", "BLUE BOTTLE COFFEE OAKLAND CA", "-9.00", "Dining"),
        row("e", "PAYPAL *99887766", "-1.00"),
    ])
    assert [(group["merchant"], group["keyword"], group["ids"], group["total"]) for group in groups] == [
        ("BLUE BOTTLE COFFEE", "blue bottle coffee", ["a", "b"], Decimal("-9.75")),
        ("CORNER STORE", "corner store", ["c"], Decimal("-3.00")),
        ("PAYPAL *99887766", None, ["e"], Decimal("-1.00")),
    ]
