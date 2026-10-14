"""User intent belongs here; UI widgets do not define persistence invariants."""
import hashlib
from uuid import UUID, uuid5, NAMESPACE_URL
from src.domain.ports import TransactionStore
from src.services.categorization import categorize


class DataService:
    def __init__(self, store: TransactionStore):
        self.store = store

    def categorize(self, transactions):
        rules = self.store.get_category_rules()
        return [categorize(item, rules) for item in transactions]

    def import_statement(self, transactions, *, keep_matching=False):
        return self.store.append_statement(self.categorize(transactions), keep_matching=keep_matching)

    def undo_import(self, batch_id):
        return self.store.undo_import_batch(batch_id)

    def restore(self, raw):
        return self.store.restore_backup(raw)

    def correct_category(self, transaction_id, category, keyword='', apply_similar=False):
        if apply_similar and not keyword.strip():
            raise ValueError('Enter a merchant phrase before updating similar transactions.')
        return self.store.correct_category(transaction_id, category, keyword.strip(), apply_similar)

    @staticmethod
    def bind_statement_account(transactions, account_id):
        if not account_id:
            return transactions
        identifier = UUID(str(account_id))
        result = []
        for item in transactions:
            file_id = hashlib.sha256(f'{item.statement_file_id}:{identifier}'.encode()).hexdigest()
            result.append(item.model_copy(update={'account_id': identifier, 'statement_file_id': file_id,
                                                 'id': 'statement:'+uuid5(NAMESPACE_URL, f'{file_id}:{item.statement_row}').hex}))
        return result
