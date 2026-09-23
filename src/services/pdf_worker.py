"""Parse untrusted PDFs outside the Streamlit server process."""
from __future__ import annotations

import json
import multiprocessing
import sys
from typing import TYPE_CHECKING

from src.domain.models import ParseMetrics, Transaction
from src.services.security import MAX_UPLOAD_BYTES

if TYPE_CHECKING:
    from multiprocessing.connection import Connection

PDF_TIMEOUT_SECONDS = 15
MAX_RESULT_BYTES = 12 * 1024 * 1024


def _parse_in_worker(connection: Connection, raw: bytes, account_name: str, account_type: str) -> None:
    try:
        if sys.platform.startswith("linux"):
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        from src.services.parser import BankStatementParser

        transactions, metrics = BankStatementParser.parse_pdf(raw, account_name, account_type)
        result = json.dumps({
            "transactions": [item.model_dump(mode="json") for item in transactions],
            "metrics": metrics.model_dump(mode="json"),
        }).encode("utf-8")
        if len(result) > MAX_RESULT_BYTES:
            raise ValueError("PDF result exceeds the processing limit.")
        connection.send_bytes(result)
    except Exception:
        connection.send_bytes(b'{"error":"This PDF could not be processed safely. Try a smaller file or CSV export."}')
    finally:
        connection.close()


def parse_pdf_bounded(raw: bytes, account_name: str, account_type: str) -> tuple[list[Transaction], ParseMetrics]:
    from src.services.parser import BankStatementParser

    if len(raw) > MAX_UPLOAD_BYTES:
        return BankStatementParser._rejected("Statement exceeds the 10 MB limit.")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_parse_in_worker, args=(sender, raw, account_name, account_type))
    try:
        process.start()
    except OSError:
        receiver.close()
        sender.close()
        return BankStatementParser._rejected("PDF processing is temporarily unavailable. Try a CSV export.")
    sender.close()
    try:
        if not receiver.poll(PDF_TIMEOUT_SECONDS):
            return BankStatementParser._rejected("PDF processing took too long. Try a smaller file or CSV export.")
        payload = json.loads(receiver.recv_bytes(MAX_RESULT_BYTES))
        if "error" in payload:
            return BankStatementParser._rejected(payload["error"])
        return ([Transaction.model_validate(row) for row in payload["transactions"]],
                ParseMetrics.model_validate(payload["metrics"]))
    except (EOFError, OSError, ValueError, KeyError):
        return BankStatementParser._rejected("This PDF could not be processed safely. Try a smaller file or CSV export.")
    finally:
        receiver.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()
