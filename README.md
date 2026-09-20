# FinanceBuddy

FinanceBuddy is a local-first personal finance dashboard built with Python, Streamlit, SQLite, Pandas, and Plotly.

The project follows Clean Architecture so the domain models, parser, categorizer, analytics, and repository can be mapped to Swift, SwiftData, and SwiftUI later without moving business logic out of the UI.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

The SQLite database is created at `data/finance.db` on first run. It is intentionally local and is not committed to Git.

## Dashboard features

- Account-aware metrics for checking and credit-card statements
- Global date, account, category, transaction-type, merchant, and amount filters
- Responsive desktop tables and mobile transaction cards
- Editable categories, reusable merchant rules, and split transactions
- Monthly budgets, savings goals, recurring-charge detection, unusual-expense review, and a simple cash-flow estimate
- Comparison of up to six statements with automatic date ordering
- Multi-account append or account-scoped replacement with import preview, duplicate detection, confirmation, and undo
- Filtered CSV export plus full JSON backup and restore

## Test

```bash
python -m pytest
```

## CSV import behavior

The importer accepts CSV and text-based PDF statements. It detects common delimiters and encodings, extracts PDF tables or date-description-amount text rows, maps common bank header names, supports signed `Amount` or `Debit`/`Credit` columns, and refuses to commit an import when fewer than 80% of data rows are valid. Image-only/scanned PDFs are not supported yet because they require OCR.

Choose `Checking` or `Credit Card` before importing. Credit-card purchases are normalized as outflows, while payments, refunds, and credits are normalized as inflows. The import preview reports its detected period, parsed and skipped rows, and exact duplicates. Appending is the default and ignores duplicates; replacement only clears the named account and can be undone during the current session.

The dashboard includes expanded categories, accessible cash-flow and category charts, a category share table, and an optional monthly category heatmap. Comparison statements are sorted automatically by their detected transaction dates.

Transaction identifiers are deterministic hashes of date, amount, and description, so re-importing the same statement does not create duplicates.
