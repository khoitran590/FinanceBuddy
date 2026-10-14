import csv
import io
import hashlib
import json
import math
import re
from datetime import date, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5
from typing import Dict, List, Optional, Tuple

from charset_normalizer import from_bytes

from src.domain.models import ParseMetrics, Transaction
from src.services.security import MAX_UPLOAD_BYTES
from src.services.imports import normalize_text
from src.services.categorizer import auto_categorize


COLUMN_SYNONYMS = {
    "date": ["date", "transaction date", "trans date", "posting date", "txn date"],
    "description": ["description", "memo", "payee", "merchant", "details", "narrative"],
    "amount": ["amount", "amt", "value", "total"],
    "debit": ["debit", "withdrawal", "charge", "outflow"],
    "credit": ["credit", "deposit", "inflow"],
    "reference": ["transaction id", "transaction reference", "reference", "reference number", "bank reference", "fitid"],
}


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _parse_amount(raw_value: str, number_format: str = "decimal_dot") -> float:
    value = raw_value.strip()
    if not value:
        raise ValueError("empty amount")
    value = re.sub(r"^(USD|EUR|GBP|CAD|AUD)\s*|\s*(USD|EUR|GBP|CAD|AUD)$", "", value, flags=re.I)
    value = re.sub(r"[$€£¥\s]", "", value)
    negative = (value.startswith("(") and value.endswith(")")) or value.endswith("-")
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    elif value.endswith("-"):
        value = value[:-1]
    if negative and value.startswith(("-", "+")):
        raise ValueError("conflicting amount signs")
    if number_format == "decimal_dot":
        pattern = r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d{1,2})?"
        normalized = value.replace(",", "")
    elif number_format == "decimal_comma":
        pattern = r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d{1,2})?"
        normalized = value.replace(".", "").replace(",", ".")
    else:
        raise ValueError("unsupported number format")
    if not re.fullmatch(pattern, value):
        raise ValueError("amount does not match the selected number format")
    amount = float(Decimal(normalized))
    return -abs(amount) if negative else amount


def _parse_date(raw_value: str, default_year: Optional[int] = None, date_order: str = "month_first"):
    value = raw_value.strip()
    if date_order not in ("month_first", "day_first"):
        raise ValueError("unsupported date order")
    local_formats = ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y") if date_order == "month_first" else ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", *local_formats):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    for separator in ("/", "-"):
        parts = value.split(separator)
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            if default_year is None:
                raise ValueError(f"year missing from date: {raw_value!r}")
            try:
                month, day = (parts if date_order == "month_first" else parts[::-1])
                return datetime(default_year, int(month), int(day)).date()
            except ValueError:
                break
    raise ValueError(f"unsupported date: {raw_value!r}")


def _normalize_amount(amount: float, sign_convention: str) -> float:
    if sign_convention == "outflow_negative":
        return amount
    if sign_convention == "outflow_positive":
        return -amount
    raise ValueError("Choose whether money out is negative or positive for the Amount column.")


def _file_id(raw: bytes, account_name: str, account_type: str, file_type: str, options: dict) -> str:
    context = json.dumps(["statement-v2", normalize_text(account_name), normalize_text(account_type), file_type,
                          options.get("sign_convention"), options.get("date_order", "month_first"),
                          options.get("number_format", "decimal_dot"), options.get("use_references", True), options.get("statement_year")], separators=(",", ":"))
    return hashlib.sha256(hashlib.sha256(raw).digest() + context.encode()).hexdigest()


class BankStatementParser:
    """Parse common bank CSV variants with an 80% validity gate."""

    @staticmethod
    def _rejected(message: str):
        return [], ParseMetrics(total_rows=0, valid_rows=0, skipped_rows=0, accuracy_rate=0.0, is_valid=False, errors=[message])

    @staticmethod
    def detect_encoding(raw_bytes: bytes) -> str:
        best = from_bytes(raw_bytes).best()
        return best.encoding if best and best.encoding else "utf-8-sig"

    @staticmethod
    def detect_dialect(sample_text: str) -> csv.Dialect:
        try:
            return csv.Sniffer().sniff(sample_text, delimiters=[",", ";", "\t", "|"])
        except csv.Error:
            class FallbackDialect(csv.Dialect):
                delimiter = ","
                quotechar = '"'
                doublequote = True
                skipinitialspace = True
                lineterminator = "\r\n"
                quoting = csv.QUOTE_MINIMAL

            return FallbackDialect()

    @staticmethod
    def match_columns(raw_headers: List[str]) -> Dict[str, str]:
        normalized = {
            _normalize_header(header): header
            for header in raw_headers
        }
        mapping: Dict[str, str] = {}
        for target, synonyms in COLUMN_SYNONYMS.items():
            for synonym in synonyms:
                if _normalize_header(synonym) in normalized:
                    mapping[target] = normalized[_normalize_header(synonym)]
                    break
        return mapping

    @classmethod
    def _parse_rows(
        cls,
        raw_rows: List[List[str]],
        account_name: str,
        default_year: Optional[int] = None,
        account_type: str = "Checking",
        *, sign_convention: Optional[str] = None, date_order: str = "month_first",
        number_format: str = "decimal_dot", expected_total: Optional[float] = None,
        use_references: bool = True, statement_year: Optional[int] = None,
        file_id: Optional[str] = None,
        period_start: Optional[date] = None, period_end: Optional[date] = None,
    ) -> Tuple[List[Transaction], ParseMetrics]:
        """Normalize rows from CSV or PDF extraction through one validation path."""
        if statement_year is not None and (not isinstance(statement_year, int) or not 1 <= statement_year <= 9999):
            return cls._rejected("Statement year must be between 1 and 9999.")
        if expected_total is not None and not math.isfinite(expected_total):
            return cls._rejected("Expected statement total must be finite.")
        raw_rows = [
            ["" if cell is None else str(cell).replace("\n", " ").strip() for cell in row]
            for row in raw_rows
            if row and any(cell is not None and str(cell).strip() for cell in row)
        ]
        if not raw_rows:
            return [], ParseMetrics(
                total_rows=0,
                valid_rows=0,
                skipped_rows=0,
                accuracy_rate=0.0,
                is_valid=False,
                errors=["Empty CSV content."],
            )

        header_index: Optional[int] = None
        column_map: Dict[str, str] = {}
        for index, row in enumerate(raw_rows):
            candidate_map = cls.match_columns(row)
            has_amount = "amount" in candidate_map or {
                "debit",
                "credit",
            }.issubset(candidate_map)
            if {"date", "description"}.issubset(candidate_map) and has_amount:
                header_index = index
                column_map = candidate_map
                break

        if header_index is None:
            headers = raw_rows[0]
            total_rows = max(len(raw_rows) - 1, 0)
            return [], ParseMetrics(
                total_rows=total_rows,
                valid_rows=0,
                skipped_rows=total_rows,
                accuracy_rate=0.0,
                is_valid=False,
                errors=[f"Missing required headers. Detected: {headers}"],
            )

        headers = raw_rows[header_index]
        data_rows = [
            row
            for row in raw_rows[header_index + 1 :]
            if not cls._is_header_row(row)
        ]
        total_rows = len(data_rows)
        if total_rows == 0:
            return [], ParseMetrics(
                total_rows=0,
                valid_rows=0,
                skipped_rows=0,
                accuracy_rate=0.0,
                is_valid=False,
                errors=["No data rows found below headers."],
            )

        has_direction_columns = {"debit", "credit"}.issubset(column_map)
        if not has_direction_columns and sign_convention is None:
            if account_type == "Credit Card":
                return cls._rejected("Choose the Amount column's sign convention for this credit-card statement. Debit/Credit columns always determine direction when present.")
            sign_convention = "outflow_negative"
        if file_id is None:
            file_id = _file_id(json.dumps(raw_rows).encode(), account_name, account_type, "rows", {
                "sign_convention": sign_convention, "date_order": date_order, "number_format": number_format, "use_references": use_references, "statement_year": statement_year})
        indexes = {name: headers.index(column) for name, column in column_map.items()}
        valid_transactions: List[Transaction] = []
        errors: List[str] = []
        skipped_rows = 0
        references = {}
        conflicting_reference = False
        conflicting_direction = False

        for source_row, row in enumerate(data_rows, start=1):
            row_number = source_row + header_index + 1
            try:
                if any(index >= len(row) for index in indexes.values()):
                    raise ValueError("row has fewer columns than the header")

                raw_date = row[indexes["date"]]
                if statement_year is None and period_start and period_end and re.fullmatch(r"\d{1,2}[/-]\d{1,2}", raw_date):
                    candidates = []
                    for year in range(period_start.year, period_end.year + 1):
                        try:
                            candidate = _parse_date(raw_date, default_year=year, date_order=date_order)
                        except ValueError:
                            continue
                        if period_start <= candidate <= period_end:
                            candidates.append(candidate)
                    if len(candidates) != 1:
                        raise ValueError("yearless date is ambiguous or outside the statement period")
                    parsed_date = candidates[0]
                else:
                    parsed_date = _parse_date(raw_date, default_year=statement_year or default_year, date_order=date_order)
                description = row[indexes["description"]].strip()
                if not description:
                    raise ValueError("missing description")

                if has_direction_columns:
                    if not row[indexes["debit"]].strip() and not row[indexes["credit"]].strip():
                        raise ValueError("missing Debit and Credit amounts")
                    debit = _parse_amount(row[indexes["debit"]], number_format) if row[indexes["debit"]].strip() else 0.0
                    credit = _parse_amount(row[indexes["credit"]], number_format) if row[indexes["credit"]].strip() else 0.0
                    if debit and credit:
                        conflicting_direction = True
                        raise ValueError("both Debit and Credit are populated for one transaction")
                    amount = abs(credit) - abs(debit)
                else:
                    amount = _normalize_amount(_parse_amount(row[indexes["amount"]], number_format), sign_convention)
                reference = row[indexes["reference"]].strip() if use_references and "reference" in indexes else None
                reference = reference or None
                if reference:
                    details = (parsed_date, amount, normalize_text(description))
                    if reference in references and references[reference] != details:
                        conflicting_reference = True
                        raise ValueError("one bank reference has conflicting transaction details")
                    references[reference] = details
                transaction_id = "statement:" + uuid5(NAMESPACE_URL, f"{file_id}:{source_row}").hex
                valid_transactions.append(
                    Transaction(
                        id=transaction_id,
                        date=parsed_date,
                        description=description,
                        amount=amount,
                        category=auto_categorize(description),
                        account_name=account_name.strip() or "Primary Checking",
                        account_type=account_type,
                        statement_file_id=file_id, statement_row=source_row,
                        statement_reference=reference,
                        statement_match_bank=True,
                    )
                )
            except (IndexError, TypeError, ValueError) as error:
                skipped_rows += 1
                if len(errors) < 5:
                    errors.append(f"Row {row_number}: {error}")

        valid_rows = len(valid_transactions)
        accuracy_rate = valid_rows / total_rows
        is_valid = accuracy_rate >= 0.80 and not conflicting_reference and not conflicting_direction
        if expected_total is not None and abs(sum((Decimal(str(item.amount)) for item in valid_transactions), Decimal(0)) - Decimal(str(expected_total))) > Decimal("0.005"):
            is_valid = False
            errors.insert(0, "Parsed money in minus money out does not match the expected statement total.")
        if accuracy_rate < 0.80:
            errors.insert(
                0,
                f"Read accuracy failed: only {valid_rows}/{total_rows} rows parsed "
                f"({accuracy_rate * 100:.1f}%). Minimum required threshold is 80.0%.",
            )

        metrics = ParseMetrics(
            total_rows=total_rows,
            valid_rows=valid_rows,
            skipped_rows=skipped_rows,
            accuracy_rate=accuracy_rate,
            errors=errors,
            is_valid=is_valid,
        )
        return (valid_transactions if is_valid else []), metrics

    @classmethod
    def _is_header_row(cls, row: List[str]) -> bool:
        mapping = cls.match_columns(row)
        has_amount = "amount" in mapping or {"debit", "credit"}.issubset(mapping)
        return {"date", "description"}.issubset(mapping) and has_amount

    @staticmethod
    def _extract_text_rows(text: str) -> List[List[str]]:
        """Extract simple date-description-amount lines from text-based PDFs."""
        line_pattern = re.compile(
            r"^\s*"
            r"(?P<date>\d{1,4}[/-]\d{1,2}(?:[/-]\d{2,4})?)\s+"
            r"(?P<description>.+?)\s+"
            r"(?P<amount>\(?[-+]?[$€£¥]?\s*\d[\d.,]*\)?-?)"
            r"(?:\s+(?P<balance>\(?[-+]?[$€£¥]?\s*\d[\d.,]*\)?-?))?"
            r"\s*$"
        )
        rows: List[List[str]] = []
        for line in text.splitlines():
            match = line_pattern.match(line)
            if match:
                rows.append(
                    [
                        match.group("date"),
                        match.group("description"),
                        match.group("amount"),
                    ]
                )
        return rows

    @classmethod
    def parse(
        cls,
        raw_bytes: bytes,
        account_name: str,
        account_type: str = "Checking",
        *, sign_convention: Optional[str] = None, date_order: str = "month_first",
        number_format: str = "decimal_dot", expected_total: Optional[float] = None,
        use_references: bool = True, statement_year: Optional[int] = None,
    ) -> Tuple[List[Transaction], ParseMetrics]:
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            return cls._rejected("Statement exceeds the 10 MB limit.")
        encoding = cls.detect_encoding(raw_bytes)
        try:
            text = raw_bytes.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            text = raw_bytes.decode("latin-1", errors="replace")

        raw_rows = []
        try:
            for row in csv.reader(io.StringIO(text), dialect=cls.detect_dialect(text[:65536])):
                if any(cell.strip() for cell in row):
                    raw_rows.append(row)
                if len(raw_rows) > 50_001:
                    return cls._rejected("Statement exceeds the 50,000-row limit.")
        except csv.Error:
            return cls._rejected("The statement contains an invalid or oversized CSV field.")
        options = dict(sign_convention=sign_convention, date_order=date_order, number_format=number_format, expected_total=expected_total, use_references=use_references, statement_year=statement_year)
        return cls._parse_rows(raw_rows, account_name, account_type=account_type,
                               file_id=_file_id(raw_bytes, account_name, account_type, "csv", options), **options)

    @classmethod
    def parse_pdf(
        cls,
        raw_bytes: bytes,
        account_name: str,
        account_type: str = "Checking",
        *, sign_convention: Optional[str] = None, date_order: str = "month_first",
        number_format: str = "decimal_dot", expected_total: Optional[float] = None,
        use_references: bool = True, statement_year: Optional[int] = None,
    ) -> Tuple[List[Transaction], ParseMetrics]:
        """Extract tables or text rows from a PDF statement.

        Image-only/scanned PDFs are reported as unreadable instead of being
        partially imported. OCR can be added later without changing the
        repository or analytics layers.
        """
        if len(raw_bytes) > MAX_UPLOAD_BYTES:
            return cls._rejected("Statement exceeds the 10 MB limit.")
        try:
            import pdfplumber
        except ImportError:
            return [], ParseMetrics(
                total_rows=0,
                valid_rows=0,
                skipped_rows=0,
                accuracy_rate=0.0,
                is_valid=False,
                errors=["PDF support requires the pdfplumber dependency."],
            )

        options = dict(sign_convention=sign_convention, date_order=date_order, number_format=number_format, expected_total=expected_total, use_references=use_references, statement_year=statement_year)
        file_id = _file_id(raw_bytes, account_name, account_type, "pdf", options)
        table_rows: List[List[str]] = []
        text_parts: List[str] = []
        try:
            with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
                if len(pdf.pages) > 100:
                    return cls._rejected("PDF exceeds the 100-page limit. Split it into smaller statements.")
                for page in pdf.pages:
                    try:
                        for table in page.extract_tables() or []:
                            for row in table or []:
                                if row:
                                    table_rows.append(row)
                    except Exception:
                        # Continue to text extraction if a PDF table has an
                        # unusual layout that pdfplumber cannot model.
                        pass

                    try:
                        page_text = page.extract_text() or ""
                        if page_text:
                            text_parts.append(page_text)
                    except Exception:
                        pass
        except Exception as error:
            return [], ParseMetrics(
                total_rows=0,
                valid_rows=0,
                skipped_rows=0,
                accuracy_rate=0.0,
                is_valid=False,
                errors=["Could not read this PDF. Use a text-based PDF or export a CSV from your bank."],
            )

        full_text = "\n".join(text_parts)
        # Amounts such as 2025.00 must never decide the transaction year.
        header_lines = [line for line in text_parts for line in line.splitlines()
                        if re.search(r"\b(statement|period|year)\b", line, re.I)
                        and not re.match(r"^\s*\d{1,4}[/-]\d{1,2}", line)]
        header_years = set(re.findall(r"(?<![$€£¥\d])\b((?:19|20)\d{2})\b(?![.,]\d)", "\n".join(header_lines)))
        default_year = int(next(iter(header_years))) if len(header_years) == 1 else None
        period_start = period_end = None
        date_pattern = r"\b(?:\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b"
        for line in header_lines:
            tokens = re.findall(date_pattern, line)
            if len(tokens) == 2:
                try:
                    bounds = [_parse_date(token, date_order=date_order) for token in tokens]
                except ValueError:
                    continue
                period_start, period_end = min(bounds), max(bounds)
                break
        options.update(period_start=period_start, period_end=period_end)
        table_result = (
            cls._parse_rows(table_rows, account_name, default_year=default_year,
                            account_type=account_type, file_id=file_id, **options)
            if table_rows else None
        )
        text_rows = cls._extract_text_rows(full_text)
        text_result = (
            cls._parse_rows(
                [["Date", "Description", "Amount"], *text_rows],
                account_name,
                default_year=default_year,
                account_type=account_type, file_id=file_id, **options,
            )
            if text_rows
            else None
        )

        if table_result and table_result[1].is_valid:
            return table_result
        if text_result and (
            text_result[1].is_valid
            or not table_result
            or text_result[1].valid_rows > table_result[1].valid_rows
        ):
            return text_result
        if table_result:
            return table_result

        return [], ParseMetrics(
            total_rows=0,
            valid_rows=0,
            skipped_rows=0,
            accuracy_rate=0.0,
            is_valid=False,
            errors=[
                "No readable transaction table or text rows found in the PDF. "
                "Image-only scans are not supported yet."
            ],
        )

    @classmethod
    def parse_file(
        cls,
        raw_bytes: bytes,
        filename: str,
        account_name: str,
        account_type: str = "Checking",
        *, sign_convention: Optional[str] = None, date_order: str = "month_first",
        number_format: str = "decimal_dot", expected_total: Optional[float] = None,
        use_references: bool = True, statement_year: Optional[int] = None,
    ) -> Tuple[List[Transaction], ParseMetrics]:
        """Dispatch an uploaded statement to the matching document parser."""
        if filename.lower().endswith(".pdf"):
            from src.services.pdf_worker import parse_pdf_bounded

            return parse_pdf_bounded(raw_bytes, account_name, account_type,
                                     sign_convention=sign_convention, date_order=date_order,
                                     number_format=number_format, expected_total=expected_total, use_references=use_references, statement_year=statement_year)
        return cls.parse(raw_bytes, account_name, account_type, sign_convention=sign_convention,
                         date_order=date_order, number_format=number_format, expected_total=expected_total, use_references=use_references, statement_year=statement_year)
