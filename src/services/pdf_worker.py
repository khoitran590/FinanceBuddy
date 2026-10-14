"""Parse untrusted PDFs in a bounded interpreter that never re-imports the UI."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from src.domain.models import ParseMetrics, Transaction
from src.services.security import MAX_UPLOAD_BYTES

PDF_TIMEOUT_SECONDS = 15
MAX_RESULT_BYTES = 12 * 1024 * 1024
MAX_CONFIG_BYTES = 4096
WORKER_ERROR = "This PDF could not be processed safely. Try a smaller file or CSV export."


def _run_worker() -> None:
    try:
        if sys.platform.startswith("linux"):
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        from src.services.parser import BankStatementParser

        config_size = int.from_bytes(sys.stdin.buffer.read(4), "big")
        if not 0 < config_size <= MAX_CONFIG_BYTES:
            raise ValueError("Invalid PDF worker configuration.")
        config = json.loads(sys.stdin.buffer.read(config_size))
        raw = sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1)
        transactions, metrics = BankStatementParser.parse_pdf(
            raw, config["account_name"], config["account_type"], **config["options"]
        )
        result = json.dumps({
            "transactions": [item.model_dump(mode="json") for item in transactions],
            "metrics": metrics.model_dump(mode="json"),
        }).encode("utf-8")
        if len(result) > MAX_RESULT_BYTES:
            raise ValueError("PDF result exceeds the processing limit.")
    except Exception:
        result = json.dumps({"error": WORKER_ERROR}).encode("utf-8")
    sys.stdout.buffer.write(result)


def parse_pdf_bounded(raw: bytes, account_name: str, account_type: str, **options) -> tuple[list[Transaction], ParseMetrics]:
    from src.services.parser import BankStatementParser

    if len(raw) > MAX_UPLOAD_BYTES:
        return BankStatementParser._rejected("Statement exceeds the 10 MB limit.")
    config = json.dumps({"account_name": account_name, "account_type": account_type, "options": options}).encode("utf-8")
    if len(config) > MAX_CONFIG_BYTES:
        return BankStatementParser._rejected(WORKER_ERROR)
    try:
        # Spawn has to re-import __main__, which Streamlit replaces with app.py.
        # A fresh module entry point avoids UI/session side effects. Configuration
        # and financial bytes travel through stdin, never process arguments.
        completed = subprocess.run(
            [sys.executable, "-m", "src.services.pdf_worker"],
            input=len(config).to_bytes(4, "big") + config + raw,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=PDF_TIMEOUT_SECONDS, cwd=Path(__file__).resolve().parents[2],
        )
        if completed.returncode or len(completed.stdout) > MAX_RESULT_BYTES:
            return BankStatementParser._rejected(WORKER_ERROR)
        payload = json.loads(completed.stdout)
        if "error" in payload:
            return BankStatementParser._rejected(payload["error"])
        return ([Transaction.model_validate(row) for row in payload["transactions"]],
                ParseMetrics.model_validate(payload["metrics"]))
    except subprocess.TimeoutExpired:
        return BankStatementParser._rejected("PDF processing took too long. Try a smaller file or CSV export.")
    except OSError:
        return BankStatementParser._rejected("PDF processing is temporarily unavailable. Try a CSV export.")
    except (ValueError, KeyError, TypeError):
        return BankStatementParser._rejected(WORKER_ERROR)


if __name__ == "__main__":
    _run_worker()
