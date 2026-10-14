"""One category policy for statements and bank data."""
from src.domain.models import CategoryRule, Transaction
from src.services.categorizer import auto_categorize


def categorize(transaction: Transaction, rules: list[CategoryRule]) -> Transaction:
    if transaction.category_override is not None:
        category, origin, rule = transaction.category_override, "manual", None
    elif transaction.category_origin == "legacy" and transaction.category != "Uncategorized":
        # Existing categories have unknown provenance. Preserve them deliberately.
        return transaction
    else:
        rule = next((rule for rule in sorted(rules, key=lambda r: r.keyword.lower())
                     if rule.keyword.strip().lower() in transaction.description.lower()), None)
        if rule:
            category, origin = rule.category, "rule"
        elif transaction.provider_category:
            category, origin = transaction.provider_category, "provider"
        else:
            category, origin = auto_categorize(transaction.description), "builtin"
    return transaction.model_copy(update={
        "category": category, "category_origin": origin, "builtin_category": auto_categorize(transaction.description),
        "category_rule": rule.keyword if rule else None,
        "category_rule_version": rule.version if rule else None,
    })
