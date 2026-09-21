from __future__ import annotations

import argparse
from pathlib import Path

from src.repositories.transaction_repo import TransactionRepository


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a legacy FinanceBuddy SQLite profile for Supabase restore."
    )
    parser.add_argument("database", help="Path to the legacy .db file")
    parser.add_argument("output", help="Destination .json backup path")
    args = parser.parse_args()

    database = Path(args.database).resolve()
    output = Path(args.output).resolve()
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}")
    if output.suffix.lower() != ".json":
        raise SystemExit("The output path must end in .json.")

    backup = TransactionRepository(str(database)).export_backup()
    output.write_text(backup, encoding="utf-8")
    print(f"Exported legacy data to {output}")


if __name__ == "__main__":
    main()
