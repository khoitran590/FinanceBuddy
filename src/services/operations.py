"""Recover committed results after an uncertain network response using the same ID."""
import hashlib
import json
from uuid import uuid4
from src.services.supabase import SupabaseError


class OperationService:
    def __init__(self, client, pending=None):
        self.client = client
        self.pending = pending if isinstance(pending, dict) else {}

    def execute(self, kind: str, payload: dict, operation_id=None):
        key = hashlib.sha256(json.dumps([kind, payload], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        identifier = str(operation_id or self.pending.setdefault(key, str(uuid4())))
        request = {'p_operation_id': identifier, 'p_kind': kind, 'p_payload': payload}
        try:
            result = self.client.rpc('fb_execute_operation', request)
        except SupabaseError as error:
            if not error.uncertain:
                self.pending.pop(key, None)
                raise
            try:
                receipt = self.client.rpc('fb_operation_result', {'p_operation_id': identifier})
                result = receipt['result'] if receipt else self.client.rpc('fb_execute_operation', request)
            except SupabaseError as recovery_error:
                recovery_error.operation_id = identifier
                raise recovery_error from error
        self.pending.pop(key, None)
        return result
