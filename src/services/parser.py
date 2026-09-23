import csv
import io
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from charset_normalizer import from_bytes

from src.domain.models import ParseMetrics, Transaction
from src.services.security import MAX_UPLOAD_BYTES
from src.services.categorizer import auto_categorize, generate_tx_id


COLUMN_SYNONYMS = {
    "date": ["date", "transaction date", "trans date", "posting date", "txn date"],
    "description": ["description", "memo", "payee", "merchant", "details", "narrative"],
    "amount": ["amount", "amt", "value", "total"],
    "debit": ["debit", "withdrawal", "charge", "outflow"],
    "credit": ["credit", "deposit", "inflow"],
}


def _normalize_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _parse_amount(raw_value: str) -> float:
    value = raw_value.strip()
    if not value:
        raise ValueError("empty amount")

    negative = (value.startswith("(") and value.endswith(")")) or value.endswith("-")
    value = value.replace("(", "").replace(")", "")
    if value.endswith("-"):
        value = value[:-1]
    value = value.replace(",", "")
    value = re.sub(r"[^0-9.\-+]", "", value)
    if not value or value in {"-", "+", "."}:
        raise ValueError(f"invalid amount: {raw_value!r}")

    amount = float(value)
    return -abs(amount) if negative else amount


def _parse_date(raw_value: str, default_year: Optional[int] = None):
    value = raw_value.strip()
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%m-%d-%Y",
        "%Y/%m/%d",
        "%m/%d/%y",
    ):
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
                return datetime(default_year, int(parts[0]), int(parts[1])).date()
            except ValueError:
                break
    raise ValueError(f"unsupported date: {raw_value!r}")


def _normalize_amount(amount: float, description: str, account_type: str) -> float:
    """Normalize statement signs so analytics work consistently across accounts."""
    if account_type != "Credit Card":
        return amount

    description_lower = description.lower()
    credit_keywords = (
        "payment",
        "credit",
        "refund",
        "return",
        "reversal",
        "adjustment",
    )
    if any(keyword in description_lower for keyword in credit_keywords):
        return abs(amount)
    return -abs(amount)


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
    ) -> Tuple[List[Transaction], ParseMetrics]:
        """Normalize rows from CSV or PDF extraction through one validation path."""
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

        indexes = {name: headers.index(column) for name, column in column_map.items()}
        valid_transactions: List[Transaction] = []
        errors: List[str] = []
        skipped_rows = 0

        for row_number, row in enumerate(data_rows, start=header_index + 2):
            try:
                if any(index >= len(row) for index in indexes.values()):
                    raise ValueError("row has fewer columns than the header")

                parsed_date = _parse_date(
                    row[indexes["date"]], default_year=default_year
                )
                description = row[indexes["description"]].strip()
                if not description:
                    raise ValueError("missing description")

                if "amount" in indexes:
                    amount = _parse_amount(row[indexes["amount"]])
                else:
                    debit = _parse_amount(row[indexes["debit"]]) if row[indexes["debit"]].strip() else 0.0
                    credit = _parse_amount(row[indexes["credit"]]) if row[indexes["credit"]].strip() else 0.0
                    amount = credit - abs(debit) if debit else credit
                amount = _normalize_amount(amount, description, account_type)

                transaction_id = generate_tx_id(
                    parsed_date.isoformat(),
                    amount,
                    description,
                    account_name,
                    account_type,
                )
                valid_transactions.append(
                    Transaction(
                        id=transaction_id,
                        date=parsed_date,
                        description=description,
                        amount=amount,
                        category=auto_categorize(description),
                        account_name=account_name.strip() or "Primary Checking",
                        account_type=account_type,
                    )
                )
            except (IndexError, TypeError, ValueError) as error:
                skipped_rows += 1
                if len(errors) < 5:
                    errors.append(f"Row {row_number}: {error}")

        valid_rows = len(valid_transactions)
        accuracy_rate = valid_rows / total_rows
        is_valid = accuracy_rate >= 0.80
        if not is_valid:
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
            r"(?P<amount>\(?[-+]?\$?\s*\d[\d,]*(?:\.\d{1,2})?\)?-?)"
            r"(?:\s+(?P<balance>\(?[-+]?\$?\s*\d[\d,]*(?:\.\d{1,2})?\)?-?))?"
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
        return cls._parse_rows(raw_rows, account_name, account_type=account_type)

    @classmethod
    def parse_pdf(
        cls,
        raw_bytes: bytes,
        account_name: str,
        account_type: str = "Checking",
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

        table_result = (
            cls._parse_rows(table_rows, account_name, account_type=account_type)
            if table_rows
            else None
        )
        full_text = "\n".join(text_parts)
        years = re.findall(r"\b(20\d{2})\b", full_text)
        default_year = int(years[-1]) if years else None
        text_rows = cls._extract_text_rows(full_text)
        text_result = (
            cls._parse_rows(
                [["Date", "Description", "Amount"], *text_rows],
                account_name,
                default_year=default_year,
                account_type=account_type,
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
    ) -> Tuple[List[Transaction], ParseMetrics]:
        """Dispatch an uploaded statement to the matching document parser."""
        if filename.lower().endswith(".pdf"):
            from src.services.pdf_worker import parse_pdf_bounded

            return parse_pdf_bounded(raw_bytes, account_name, account_type)
        return cls.parse(raw_bytes, account_name, account_type)
