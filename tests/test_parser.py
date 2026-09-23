from io import BytesIO

from reportlab.pdfgen import canvas

from src.services.parser import BankStatementParser


def test_parser_accepts_common_csv_and_categorizes_transactions():
    csv_data = (
        "Posting Date,Description,Amount\n"
        "2026-01-01,Payroll ACME,2500.00\n"
        "01/02/2026,Trader Joe's,-85.40\n"
        "2026-01-03,Internet,-65.00\n"
    ).encode()

    transactions, metrics = BankStatementParser.parse(csv_data, "Checking")

    assert metrics.is_valid
    assert metrics.accuracy_rate == 1.0
    assert len(transactions) == 3
    assert transactions[1].category == "Groceries"
    assert transactions[2].category == "Utilities"


def test_parser_rejects_below_eighty_percent_accuracy():
    csv_data = (
        "Date,Memo,Amount\n"
        "2026-01-01,Valid,10\n"
        "not-a-date,Broken,20\n"
        "2026-01-03,Also broken,not-money\n"
        "2026-01-04,Another broken,30\n"
        "2026-01-05,Valid enough,40\n"
    ).encode()

    transactions, metrics = BankStatementParser.parse(csv_data, "Checking")

    assert not metrics.is_valid
    assert transactions == []
    assert metrics.valid_rows == 3
    assert metrics.accuracy_rate < 0.80


def test_parser_supports_semicolon_debit_credit_exports():
    csv_data = (
        "Date;Merchant;Debit;Credit\n"
        "01/10/2026;Rent;1200.00;\n"
        "01/15/2026;Payroll;;2500.00\n"
    ).encode()

    transactions, metrics = BankStatementParser.parse(csv_data, "Checking")

    assert metrics.is_valid
    assert [transaction.amount for transaction in transactions] == [-1200.0, 2500.0]


def test_credit_card_transactions_are_normalized_for_analytics():
    csv_data = (
        "Date,Description,Amount\n"
        "2026-01-01,Payment Thank You,-500.00\n"
        "2026-01-02,ONLINE RETAILER,125.00\n"
    ).encode()

    transactions, metrics = BankStatementParser.parse(
        csv_data,
        "Rewards Card",
        "Credit Card",
    )

    assert metrics.is_valid
    assert [transaction.amount for transaction in transactions] == [500.0, -125.0]
    assert all(transaction.account_type == "Credit Card" for transaction in transactions)


def test_pdf_parser_extracts_text_statement_rows():
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 760, "Personal Checking Statement 2026")
    document.drawString(72, 730, "01/01 Payroll ACME 2500.00")
    document.drawString(72, 710, "01/02 Trader Joe's -85.40")
    document.drawString(72, 690, "01/03 Internet -65.00")
    document.drawString(72, 670, "01/04 Deposit Source 100.00 200.00")
    document.save()

    transactions, metrics = BankStatementParser.parse_pdf(output.getvalue(), "Checking")

    assert metrics.is_valid
    assert len(transactions) == 4
    assert transactions[0].amount == 2500.0
    assert transactions[1].category == "Groceries"
    assert transactions[2].category == "Utilities"
    assert transactions[3].amount == 100.0


def test_pdf_upload_is_parsed_in_bounded_worker():
    output = BytesIO()
    document = canvas.Canvas(output)
    document.drawString(72, 760, "Checking Statement 2026")
    document.drawString(72, 730, "01/01 Payroll 2500.00")
    document.save()
    rows, metrics = BankStatementParser.parse_file(output.getvalue(), "statement.pdf", "Checking")
    assert metrics.is_valid
    assert len(rows) == 1
