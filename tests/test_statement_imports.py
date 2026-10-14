from decimal import Decimal
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO
from unittest.mock import Mock

import pytest
from reportlab.pdfgen import canvas

from src.domain.models import Transaction
from src.repositories.supabase_repo import SupabaseTransactionRepository
from src.repositories.transaction_repo import TransactionRepository
from src.repositories.plaid_repo import PlaidRepository
from src.services.plaid_service import PlaidService, PlaidConfig, TokenCipher
from src.services.parser import BankStatementParser


def parse(body, **options):
    rows, metrics = BankStatementParser.parse(body.encode(), "Checking", **options)
    assert metrics.is_valid, metrics.errors
    return rows


def repeated(count=2, extra="", account="Checking"):
    data = "Date,Description,Amount\n" + "2026-10-01,Coffee,-4.50\n" * count + extra
    rows, metrics = BankStatementParser.parse(data.encode(), account)
    assert metrics.is_valid
    return rows


def test_identical_purchases_have_distinct_stable_file_row_ids(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    rows = repeated()
    assert len({row.id for row in rows}) == 2
    assert [row.id for row in repeated()] == [row.id for row in rows]
    result = repo.append_statement(rows)
    assert result["inserted"] == 2
    assert sum(row.amount for row in repo.get_all()) == -9
    assert repo.append_statement(repeated())["inserted"] == 0
    assert repo.append_statement(repeated(), keep_matching=True)["inserted"] == 0


def test_overlapping_statement_consumes_occurrences_once(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.append_statement(repeated())
    overlap = repeated(3, "2026-10-02,Store,-10\n")
    preview = repo.preview_statement_import(overlap)
    committed = repo.append_statement(overlap)
    assert preview["matched"] == committed["matched"] == 2
    assert committed["inserted"] == 2
    assert len(repo.get_all()) == 4
    assert repo.append_statement(overlap)["inserted"] == 0


def test_separate_purchase_override_does_not_disable_exact_file_retry(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.append_statement(repeated(1))
    different = repeated(1, "2026-10-02,Store,-10\n")
    assert repo.preview_statement_import(different)["matched"] == 1
    assert repo.append_statement(different, keep_matching=True)["inserted"] == 2
    assert repo.append_statement(different, keep_matching=True)["inserted"] == 0
    assert len(repo.get_all()) == 3


def test_matching_legacy_and_bank_records_preserves_categories_and_splits(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    legacy = Transaction(id="legacy-hash", date=date(2026, 10, 1), description="Coffee", amount=-4.5,
                         category="Travel", account_name="Checking")
    bank = legacy.model_copy(update={"id": "plaid:bank", "plaid_item_id": "bank"})
    repo.insert_many([legacy, bank])
    repo.split_transaction(bank.id, "Groceries", 2, "Dining", 2.5)
    result = repo.append_statement(repeated(3))
    assert result["matched"] == 2 and result["inserted"] == 1
    canonical = {row.id: row for row in repo.get_canonical_all()}
    assert canonical[legacy.id].category == "Travel"
    assert len(canonical[bank.id].allocations) == 2
    assert sum(row.amount for row in repo.get_all()) == -13.5


def test_account_scope_is_respected(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.append_statement(repeated(1))
    assert repo.append_statement(repeated(1, account="Other account"))["inserted"] == 1


def test_references_preserve_genuine_identical_purchases_and_detect_conflicts(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    rows = parse("Date,Description,Amount,Transaction ID\n2026-10-01,Coffee,-4.50,one\n2026-10-01,Coffee,-4.50,two\n")
    assert repo.append_statement(rows)["inserted"] == 2
    retry = parse("Date,Description,Amount,Transaction ID\n2026-10-01,Coffee,-4.50,two\n2026-10-01,Coffee,-4.50,one\n")
    assert repo.append_statement(retry, keep_matching=True)["inserted"] == 0
    changed = parse("Date,Description,Amount,Transaction ID\n2026-10-01,Coffee,-8.50,one\n")
    with pytest.raises(ValueError, match="reference"):
        repo.append_statement(changed)
    assert len(repo.get_all()) == 2


def test_duplicate_reference_is_one_record_and_conflicting_file_is_rejected(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    rows = parse("Date,Description,Amount,Reference\n2026-10-01,Coffee,-4.50,one\n2026-10-01,Coffee,-4.50,one\n")
    assert repo.append_statement(rows)["inserted"] == 1
    rows, metrics = BankStatementParser.parse(
        b"Date,Description,Amount,Reference\n2026-10-01,Coffee,-4.50,one\n2026-10-01,Coffee,-8.50,one\n", "Checking")
    assert not metrics.is_valid and not rows


def test_known_reference_is_reserved_before_content_matching(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    repo.append_statement(parse("Date,Description,Amount,Reference\n2026-10-01,Coffee,-4.50,one\n"))
    incoming = parse("Date,Description,Amount,Reference\n2026-10-01,Coffee,-4.50,\n2026-10-01,Coffee,-4.50,one\n")
    assert repo.preview_statement_import(incoming)["inserted"] == 1
    assert repo.append_statement(incoming)["inserted"] == 1


def test_receipts_keep_retry_idempotent_after_a_matched_bank_row_changes_and_backup(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    bank = Transaction(id="plaid:bank", date=date(2026, 10, 1), description="Coffee", amount=-4.5,
                       category="Dining", account_name="Checking")
    repo.insert_many([bank])
    assert repo.append_statement(repeated(1))["inserted"] == 0
    repo.upsert_many([bank.model_copy(update={"amount": -5})])
    assert repo.append_statement(repeated(1))["inserted"] == 0
    backup = repo.export_backup()
    restored = TransactionRepository(str(tmp_path / "restored.db"))
    restored.restore_backup(backup.encode())
    assert restored.append_statement(repeated(1))["inserted"] == 0
    assert restored.get_all()[0].amount == -5


def test_undo_only_removes_new_occurrences_and_keeps_existing_match(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    first = repo.append_statement(repeated(1))
    overlap = repo.append_statement(repeated(2))
    assert overlap["inserted"] == 1
    assert repo.undo_append(overlap["inserted_rows"]) == 1
    assert repo.get_existing_ids() == {first["inserted_rows"][0].id}
    assert repo.append_statement(repeated(2))["inserted"] == 1


def test_concurrent_imports_keep_the_maximum_occurrence_count(tmp_path):
    repo = TransactionRepository(str(tmp_path / "finance.db"))
    a, b = repeated(2), repeated(2, "2026-10-02,Store,-10\n")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(repo.append_statement, [a, b]))
    assert sum(result["inserted"] for result in results) == 3
    assert len(repo.get_all()) == 3


def test_cloud_append_uses_one_atomic_rpc_and_returns_authoritative_undo_rows():
    client = Mock()
    rows = repeated()
    client.rpc.return_value = {"inserted": 1, "matched": 1, "inserted_rows": [rows[1].model_dump(mode="json")]}
    result = SupabaseTransactionRepository(client, "owner").append_statement(rows)
    client.rpc.assert_called_once()
    assert client.rpc.call_args.args[0] == "fb_execute_operation"
    assert client.rpc.call_args.args[1]["p_kind"] == "append"
    assert result["inserted_rows"] == [rows[1]]
    assert not client.select.called


@pytest.mark.parametrize("convention", [None, "outflow_negative", "outflow_positive"])
def test_explicit_debit_credit_wins_over_description_and_amount_column(convention):
    rows, metrics = BankStatementParser.parse(
        b"Date,Description,Amount,Debit,Credit\n2026-10-01,PAYMENT TO STORE,25,25,\n2026-10-02,PROMOTIONAL BONUS,-10,,10\n",
        "Card", "Credit Card", sign_convention=convention)
    assert metrics.is_valid
    assert [row.amount for row in rows] == [-25, 10]


def test_credit_card_amount_requires_a_convention_and_never_uses_keywords():
    raw = b"Date,Description,Amount\n2026-10-01,PAYMENT TO STORE,-25\n2026-10-02,REFUND,10\n"
    rows, metrics = BankStatementParser.parse(raw, "Card", "Credit Card")
    assert not rows and not metrics.is_valid
    assert "sign convention" in metrics.errors[0]
    for convention, expected in [("outflow_negative", [-25, 10]), ("outflow_positive", [25, -10])]:
        rows, metrics = BankStatementParser.parse(raw, "Card", "Credit Card", sign_convention=convention)
        assert metrics.is_valid and [row.amount for row in rows] == expected


def test_both_direction_columns_nonzero_reject_the_entire_file():
    raw = b"Date,Description,Debit,Credit\n" + b"2026-10-01,Store,25,\n" * 9 + b"2026-10-02,Bad,20,10\n"
    rows, metrics = BankStatementParser.parse(raw, "Card", "Credit Card")
    assert not rows and not metrics.is_valid
    assert metrics.accuracy_rate == 0.9


def test_locale_profiles_and_wrong_number_format_rejection():
    raw = b"Date;Description;Amount\n01/02/2026;Shop;-1.234,56\n"
    rows = parse(raw.decode(), date_order="day_first", number_format="decimal_comma")
    assert rows[0].date == date(2026, 2, 1) and rows[0].amount == Decimal("-1234.56")
    rows, metrics = BankStatementParser.parse(raw, "Checking")
    assert not rows and not metrics.is_valid
    date_raw = b"Date,Description,Amount\n01/02/2026,Coffee,-4.50\n"
    assert BankStatementParser.parse(date_raw, "Checking", date_order="month_first")[0][0].date == date(2026, 1, 2)
    assert BankStatementParser.parse(date_raw, "Checking", date_order="day_first")[0][0].date == date(2026, 2, 1)


@pytest.mark.parametrize("amount", ["1,23", "1.234,56", "money12", "1,234,56", "(+-12)", "1.234"])
def test_malformed_amounts_are_not_sanitized_into_different_money(amount):
    raw = f'Date,Description,Amount\n2026-10-01,Shop,"{amount}"\n'.encode()
    rows, metrics = BankStatementParser.parse(raw, "Checking")
    assert not rows and not metrics.is_valid


def test_expected_total_blocks_incomplete_or_wrong_direction_statement():
    raw = b"Date,Description,Amount\n2026-10-01,Coffee,-4.50\n2026-10-02,Shop,-10\n"
    assert BankStatementParser.parse(raw, "Checking", expected_total=-14.5)[1].is_valid
    rows, metrics = BankStatementParser.parse(raw, "Checking", expected_total=14.5)
    assert not rows and not metrics.is_valid
    assert "expected statement total" in metrics.errors[0]


@pytest.mark.parametrize("total", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_statement_totals_are_rejected(total):
    rows, metrics = BankStatementParser.parse(b"Date,Description,Amount\n2026-10-01,Coffee,-4.50\n",
                                             "Checking", expected_total=total)
    assert not rows and not metrics.is_valid


def test_shared_reference_labels_can_be_ignored_explicitly():
    raw = b"Date,Description,Amount,Reference\n2026-10-01,Coffee,-4.50,ACH\n2026-10-02,Shop,-10,ACH\n"
    assert not BankStatementParser.parse(raw, "Checking")[1].is_valid
    rows, metrics = BankStatementParser.parse(raw, "Checking", use_references=False)
    assert metrics.is_valid and len({row.id for row in rows}) == 2
    assert all(row.statement_reference is None for row in rows)


def test_pdf_worker_propagates_direction_date_and_number_profile():
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(72, 760, "Card Statement 2026")
    pdf.drawString(72, 730, "01/02 Shop 1.234,56")
    pdf.save()
    rows, metrics = BankStatementParser.parse_file(output.getvalue(), "statement.pdf", "Card", "Credit Card",
        sign_convention="outflow_positive", date_order="day_first", number_format="decimal_comma", expected_total=-1234.56)
    assert metrics.is_valid, metrics.errors
    assert rows[0].amount == Decimal("-1234.56") and rows[0].date == date(2026, 2, 1)


def test_pdf_worker_does_not_import_streamlit_main_module(monkeypatch):
    import sys
    from pathlib import Path

    # Streamlit assigns app.py to __main__; multiprocessing spawn used to
    # execute its login/UI side effects before the PDF worker could start.
    monkeypatch.setattr(sys.modules["__main__"], "__file__",
                        str(Path(__file__).parents[1] / "app.py"), raising=False)
    output = BytesIO()
    pdf = canvas.Canvas(output)
    pdf.drawString(72, 760, "Checking Statement 2026")
    pdf.drawString(72, 730, "01/02 Coffee -4.50")
    pdf.save()
    rows, metrics = BankStatementParser.parse_file(output.getvalue(), "checking.pdf", "Checking")
    assert metrics.is_valid and len(rows) == 1


def pdf_statement(lines):
    output = BytesIO()
    pdf = canvas.Canvas(output)
    for index, line in enumerate(lines):
        pdf.drawString(72, 760 - index * 20, line)
    pdf.save()
    return output.getvalue()


def test_pdf_amounts_do_not_decide_the_year():
    raw = pdf_statement(["Checking Statement 2026", "01/02 Payroll 2025.00"])
    rows, metrics = BankStatementParser.parse_pdf(raw, "Checking")
    assert metrics.is_valid and rows[0].date == date(2026, 1, 2)
    raw = pdf_statement(["Checking account", "01/02 Statement Fee 2025"])
    assert not BankStatementParser.parse_pdf(raw, "Checking")[1].is_valid


def test_statement_period_resolves_year_rollover():
    raw = pdf_statement(["Statement period 12/15/2025 to 01/15/2026", "12/31 Coffee -4.50", "01/02 Shop -10"])
    rows, metrics = BankStatementParser.parse_pdf(raw, "Checking")
    assert metrics.is_valid, metrics.errors
    assert [row.date for row in rows] == [date(2025, 12, 31), date(2026, 1, 2)]


def test_explicit_year_supports_yearless_csv_and_pdf():
    raw = b"Date,Description,Amount\n01/02,Coffee,-4.50\n"
    assert not BankStatementParser.parse(raw, "Checking")[1].is_valid
    rows, metrics = BankStatementParser.parse(raw, "Checking", statement_year=2026)
    assert metrics.is_valid and rows[0].date == date(2026, 1, 2)
    pdf = pdf_statement(["Checking account", "01/02 Coffee -4.50"])
    rows, metrics = BankStatementParser.parse_file(pdf, "statement.pdf", "Checking", statement_year=2026)
    assert metrics.is_valid and rows[0].date == date(2026, 1, 2)


class BankClient:
    def transactions_sync(self, request):
        return {"added": [{"transaction_id": f"bank{index}", "account_id": "account",
                           "date": "2026-10-01", "name": "Coffee", "amount": 4.5} for index in range(2)],
                "modified": [], "removed": [], "next_cursor": "current", "has_more": False}

    def accounts_get(self, request):
        return {"accounts": [{"account_id": "account", "name": "Checking", "type": "depository"}]}


def connect(path):
    bank = PlaidRepository(str(path))
    config = PlaidConfig("client", "secret", token_encryption_key="key")
    bank.save_item("item", TokenCipher(config).encrypt("access"), "Bank")
    bank.save_accounts("item", [{"account_id": "account", "name": "Checking", "type": "depository"}])
    return bank, config


def test_statement_first_bank_sync_merges_occurrences_and_preserves_splits(tmp_path):
    path = tmp_path / "finance.db"
    repo = TransactionRepository(str(path))
    result = repo.append_statement(repeated())
    source_id = result["inserted_rows"][0].id
    repo.update_category(source_id, "Travel")
    repo.split_transaction(source_id, "Groceries", 2, "Dining", 2.5)
    bank, config = connect(path)
    sync = PlaidService(config, bank, repo, BankClient()).sync_item("item")
    assert sync["reconciled_imports"] == 2
    assert repo.get_existing_ids() == {"plaid:bank0", "plaid:bank1"}
    assert sum(row.amount for row in repo.get_all()) == -9
    assert any(len(row.allocations) == 2 and row.category == "Travel" for row in repo.get_canonical_all())
    assert repo.append_statement(repeated())["inserted"] == 0
    with pytest.raises(ValueError):
        repo.undo_append(result["inserted_rows"])


def test_explicitly_separate_imports_are_not_merged_by_later_bank_sync(tmp_path):
    path = tmp_path / "finance.db"
    repo = TransactionRepository(str(path))
    repo.append_statement(repeated(), keep_matching=True)
    bank, config = connect(path)
    sync = PlaidService(config, bank, repo, BankClient()).sync_item("item")
    assert sync["reconciled_imports"] == 0
    assert len(repo.get_canonical_all()) == 4


def test_reviewed_legacy_import_can_reconcile_when_bank_is_connected_later(tmp_path):
    path = tmp_path / "finance.db"
    repo = TransactionRepository(str(path))
    repo.insert_many([Transaction(id="legacy-hash", date=date(2026, 10, 1), description="Coffee", amount=-4.5,
                                 category="Travel", account_name="Checking")])
    assert repo.append_statement(repeated(1))["inserted"] == 0
    bank, config = connect(path)
    sync = PlaidService(config, bank, repo, BankClient()).sync_item("item")
    assert sync["reconciled_imports"] == 1
    assert len(repo.get_all()) == 2
    assert "Travel" in {row.category for row in repo.get_all()}


def test_connected_bank_replacement_is_blocked_to_avoid_recreating_duplicate_history(tmp_path):
    path = tmp_path / "finance.db"
    repo = TransactionRepository(str(path))
    connect(path)
    assert repo.is_connected_account("Checking")
    with pytest.raises(ValueError, match="Append"):
        repo.replace_account("Checking", repeated())
    assert repo.get_all() == []


def test_mixed_bulk_payloads_keep_boolean_metadata_nonnull():
    repo = SupabaseTransactionRepository(Mock(), "owner")
    source = repeated(1)[0]
    plain = Transaction(id="plain", date=date(2026, 10, 1), description="Plain", amount=-1, account_name="Checking")
    rows = repo._transaction_rows([source, plain])
    assert rows[0]["statement_match_bank"] is True
    assert rows[1]["statement_match_bank"] is False
