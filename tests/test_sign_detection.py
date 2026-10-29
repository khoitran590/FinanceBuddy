from src.services.parser import BankStatementParser
from src.services.sign_detection import sign_evidence

CHECKING = b"Date,Description,Amount\n2026-10-01,ACME PAYROLL,2500\n2026-10-02,Grocery,-80.25\n2026-10-03,Cafe,-4.75\n"
CARD = b"Date,Description,Amount\n2026-10-01,PAYMENT THANK YOU,-300\n2026-10-02,Grocery,80.25\n"


def evidence(raw, account_type, convention):
    rows, metrics = BankStatementParser.parse(raw, "Account", account_type, sign_convention=convention)
    assert metrics.is_valid and not metrics.direction_columns
    return sign_evidence(rows, account_type)


def test_payroll_as_money_in_confirms_and_as_money_out_looks_reversed():
    right = evidence(CHECKING, "Checking", "outflow_negative")
    assert (right["money_out"], str(right["out_total"]), right["money_in"], str(right["in_total"])) == (2, "85.00", 1, "2500.00")
    assert right["confirmed"] and not right["looks_reversed"]
    wrong = evidence(CHECKING, "Checking", "outflow_positive")
    assert wrong["looks_reversed"] and not wrong["confirmed"]


def test_card_payments_are_money_in_for_credit_cards():
    assert evidence(CARD, "Credit Card", "outflow_positive")["confirmed"]
    assert evidence(CARD, "Credit Card", "outflow_negative")["looks_reversed"]


def test_no_direction_words_gives_no_opinion():
    result = evidence(b"Date,Description,Amount\n2026-10-01,Store,-5\n", "Checking", "outflow_negative")
    assert not result["confirmed"] and not result["looks_reversed"]


def test_debit_credit_columns_are_reported():
    _, metrics = BankStatementParser.parse(b"Date,Description,Debit,Credit\n2026-10-01,Store,5,\n", "Card", "Credit Card")
    assert metrics.is_valid and metrics.direction_columns
