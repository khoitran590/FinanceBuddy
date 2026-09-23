from datetime import date

from src.domain.models import Transaction
from src.ui.components import transactions_to_csv


def test_csv_export_does_not_expose_spreadsheet_formulas():
    transaction = Transaction(
        id="test-1",
        date=date(2026, 9, 21),
        description="=HYPERLINK(\"https://example.com\")",
        amount=-12.50,
        category=" @malicious",
        account_name="Checking",
    )

    exported = transactions_to_csv([transaction]).decode("utf-8")

    assert "'=HYPERLINK" in exported
    assert "' @malicious" in exported
    assert "-12.5" in exported
