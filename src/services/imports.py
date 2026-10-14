"""Occurrence-aware import planning, shared by preview and local persistence."""
from collections import defaultdict, deque
from decimal import Decimal

from src.domain.models import Transaction


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def normalize_text(value: str) -> str:
    # One explicit normalization rule in Python and SQL, independent of locale.
    return value.strip(" ").translate(_ASCII_LOWER)


def match_key(item: Transaction) -> tuple:
    return (item.date, Decimal(str(item.amount)), normalize_text(item.description),
            str(item.account_id) if item.account_id else normalize_text(item.account_name),
            normalize_text(item.account_type), item.currency)


def reference_key(item: Transaction) -> tuple:
    return (str(item.account_id) if item.account_id else normalize_text(item.account_name), normalize_text(item.account_type), item.statement_reference)


def plan_statement_import(incoming, existing, mappings=(), *, keep_matching=False) -> dict:
    """A content match is an occurrence, never a unique transaction identity.

    Receipt/row IDs and bank references are authoritative. Other matching rows
    consume existing occurrences once; new identical rows do not consume each
    other. The caller can retain unmatched-file content matches as new purchases.
    """
    incoming, existing = list(incoming), list(existing)
    by_id = {item.id: item for item in existing}
    receipts = {(row['file_id'], row['row_number']): row['transaction_id'] for row in mappings}
    for item in existing:
        if item.statement_file_id and item.statement_row:
            receipts.setdefault((item.statement_file_id, item.statement_row), item.id)
    references = {reference_key(item): item for item in existing if item.statement_reference}
    reserved = {receipts.get((item.statement_file_id, item.statement_row), item.id) for item in incoming}
    reserved.update(references[reference_key(item)].id for item in incoming
                    if item.statement_reference and reference_key(item) in references)
    used = set(reserved)
    pools, unreferenced = defaultdict(deque), defaultdict(deque)
    for item in sorted(existing, key=lambda item: item.id):
        if item.id not in reserved:
            pools[match_key(item)].append(item)
            if not item.statement_reference:
                unreferenced[match_key(item)].append(item)
    inserted, mappings_out, matched, references_added, provenance_added = [], [], 0, [], []
    for item in incoming:
        if not item.statement_file_id or not item.statement_row or item.source_transaction_id:
            raise ValueError('A statement row must have file and row identity.')
        selected = by_id.get(receipts.get((item.statement_file_id, item.statement_row), item.id))
        if selected is not None and item.account_id and selected.account_id and item.account_id != selected.account_id:
            raise ValueError("Statement belongs to a different account.")
        if selected is None and item.statement_reference:
            selected = references.get(reference_key(item))
            if selected is not None and match_key(selected) != match_key(item):
                raise ValueError('A bank reference already exists with different transaction details. Review it before appending.')
        if selected is None and not keep_matching:
            pool = unreferenced[match_key(item)] if item.statement_reference else pools[match_key(item)]
            while pool:
                candidate = pool.popleft()
                if candidate.id not in used:
                    selected = candidate
                    break
        if selected is None:
            selected = item
            inserted.append(item)
            by_id[item.id] = item
        else:
            matched += 1
        used.add(selected.id)
        if not keep_matching and not selected.id.startswith('plaid:') and selected.statement_file_id is None:
            selected = selected.model_copy(update={'statement_file_id': item.statement_file_id,
                'statement_row': item.statement_row, 'statement_match_bank': True})
            provenance_added.append((selected.id, item.statement_file_id, item.statement_row))
            by_id[selected.id] = selected
        if item.statement_reference:
            if selected.statement_reference is None:
                # Associate a reference with a matched legacy/bank record, without
                # modifying its amount, category, allocations, or provider identity.
                references_added.append((selected.id, item.statement_reference))
                selected = selected.model_copy(update={'statement_reference': item.statement_reference})
                by_id[selected.id] = selected
            references[reference_key(item)] = selected
        mappings_out.append({'file_id': item.statement_file_id, 'row_number': item.statement_row,
                             'transaction_id': selected.id})
    return {'inserted': len(inserted), 'matched': matched, 'inserted_rows': [item.model_copy(update={'statement_match_bank': not keep_matching}) for item in inserted],
            'mappings': mappings_out, 'references_added': references_added,
            'provenance_added': provenance_added}
