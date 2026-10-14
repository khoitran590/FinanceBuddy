"""Project source records for analytics without replacing their identities."""
from decimal import Decimal

from src.domain.models import Transaction, TransactionAllocation


def allocations_match(transaction: Transaction) -> bool:
    allocations = transaction.allocations
    return bool(allocations) and all(
        item.amount != 0 and (item.amount > 0) == (transaction.amount > 0)
        for item in allocations
    ) and abs(
        sum((Decimal(str(item.amount)) for item in allocations), Decimal(0))
        - Decimal(str(transaction.amount))
    ) <= Decimal('0.005')


def project_transactions(transactions: list[Transaction]) -> list[Transaction]:
    result = []
    for transaction in transactions:
        if not transaction.allocations:
            result.append(transaction)
        elif not allocations_match(transaction):
            # A changed bank amount must be counted once at its current value.
            # Retain the old allocations so the person can review and re-split.
            result.append(transaction.model_copy(update={'split_needs_review': True}))
        else:
            for allocation in transaction.allocations:
                result.append(transaction.model_copy(update={
                    'id': f'allocation:{allocation.id}',
                    'amount': allocation.amount,
                    'category': allocation.category,
                    'source_transaction_id': transaction.id,
                    'source_amount': transaction.amount,
                }))
    return result


def recover_legacy_splits(transactions: list[Transaction]) -> list[Transaction]:
    """Recover only complete pairs produced by the former split implementation.

    Unpaired or edited legacy rows remain intact; never guess missing amounts.
    Process deepest splits first so a repeatedly split record can be recovered.
    """
    by_id = {item.id: item for item in transactions}
    if len(by_id) != len(transactions):
        raise ValueError("Duplicate transaction IDs in backup.")
    changed = True
    while changed:
        changed = False
        for identifier in sorted(list(by_id), key=len, reverse=True):
            if not identifier.endswith('-split-1'):
                continue
            parent_id = identifier[:-8]
            first, second = by_id.get(identifier), by_id.get(parent_id + '-split-2')
            if first is None or second is None:
                continue
            description = first.description.removesuffix(' (split 1)')
            if (first.description != description + ' (split 1)'
                    or second.description != description + ' (split 2)'
                    or (first.date, first.account_name, first.account_type)
                    != (second.date, second.account_name, second.account_type)
                    or first.amount == 0 or second.amount == 0
                    or (first.amount > 0) != (second.amount > 0)):
                continue
            parent = by_id.get(parent_id) or first.model_copy(update={
                'id': parent_id, 'description': description,
                'amount': first.amount + second.amount, 'allocations': [],
            })
            if parent.allocations:
                continue
            allocations = []
            for part in (first, second):
                allocations.extend(part.allocations or [
                    TransactionAllocation(amount=part.amount, category=part.category)
                ])
            by_id[parent_id] = parent.model_copy(update={'allocations': allocations})
            del by_id[first.id], by_id[second.id]
            changed = True
    return list(by_id.values())
