"""User intent belongs here; UI widgets do not define persistence invariants."""
import hashlib
import json
from uuid import UUID, uuid5, NAMESPACE_URL
from src.domain.ports import TransactionStore
from src.services.categorization import categorize
from src.services.parser import BankStatementParser
from src.services.sample_data import SAMPLE_BUDGETS, SAMPLE_GOALS, SAMPLE_SETTING, sample_statements

# app_settings values are capped by the settings_backup_bounds constraint.
MAX_SETTING_CHARS = 16_384


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

    def get_json_setting(self, key, default):
        """A saved JSON value of the same type as ``default``, or ``default``."""
        try:
            value = json.loads(self.store.get_setting(key) or 'null')
        except (ValueError, TypeError):
            return default
        return value if isinstance(value, type(default)) else default

    def set_json_setting(self, key, value):
        text = json.dumps(value, sort_keys=True, separators=(',', ':'))
        if len(text) > MAX_SETTING_CHARS:
            raise ValueError('This setting is too large to save.')
        self.store.set_setting(key, text)

    def sample_data(self) -> dict:
        return self.get_json_setting(SAMPLE_SETTING, {})

    def load_sample_data(self, today):
        if self.sample_data():
            raise ValueError('Sample data is already loaded.')
        existing = {budget.category for budget in self.store.get_budgets()}
        statements = sample_statements(today)
        marker = {
            'account_names': [name for name, _, _ in statements],
            # Never overwrite a budget the person already has.
            'budgets': [budget.model_dump(mode='json') for budget in SAMPLE_BUDGETS if budget.category not in existing],
            'goals': [{'name': goal.name, 'target_amount': str(goal.target_amount)} for goal in SAMPLE_GOALS],
            'rules_before': sorted(rule.keyword for rule in self.store.get_category_rules()),
            'batch_ids': [],
        }
        # Saved first, so a load interrupted part way can still be cleared.
        self.set_json_setting(SAMPLE_SETTING, marker)
        for name, account_type, raw in statements:
            rows, metrics = BankStatementParser.parse(raw, name, account_type, sign_convention='outflow_negative')
            if not metrics.is_valid:
                raise ValueError('Sample statement could not be read.')
            marker['batch_ids'].append(str(self.import_statement(rows)['batch_id']))
        for budget in marker['budgets']:
            self.store.upsert_budget(next(item for item in SAMPLE_BUDGETS if item.category == budget['category']))
        for goal in SAMPLE_GOALS:
            self.store.save_goal(goal)
        self.set_json_setting(SAMPLE_SETTING, marker)
        return marker

    def rules_added_since_sample(self) -> list[str]:
        before = set(self.sample_data().get('rules_before', []))
        return sorted(rule.keyword for rule in self.store.get_category_rules() if rule.keyword not in before)

    def clear_sample_data(self, remove_rules=False):
        marker = self.sample_data()
        if not marker:
            return 0
        return self.store.clear_sample(marker.get('account_names', []), marker.get('budgets', []), marker.get('goals', []),
                                       self.rules_added_since_sample() if remove_rules else [])

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
