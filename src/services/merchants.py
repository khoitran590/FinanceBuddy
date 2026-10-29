"""Merchant phrases for category rules.

Rules match when the lowercased description contains the keyword literally
(categorization.py, fb_correct_category), so a suggested phrase must be a
substring of every description it is meant to cover. Cleaned display names
such as "Amazon" for "AMZN Mktp US*2K3L91" are not.
"""
from collections import defaultdict
from decimal import Decimal
from typing import Iterable, Optional

from src.services.analytics import merchant_name, merchant_tokens

# Words that name a payment rail or memo boilerplate rather than a merchant.
PROCESSOR_WORDS = frozenset({
    "ach", "card", "check", "checkcard", "credit", "debit", "deposit", "online", "payment",
    "paypal", "pos", "purchase", "recurring", "sq", "square", "transfer", "tst", "venmo",
    "withdrawal", "zelle",
})
MAX_PHRASE_WORDS = 3
BROAD_SHARE = 0.25
BROAD_MINIMUM_TRANSACTIONS = 20


def is_processor_phrase(phrase: str) -> bool:
    words = phrase.strip().lower().split()
    return bool(words) and all(word in PROCESSOR_WORDS for word in words)


def _usable(phrase: str) -> bool:
    return len(phrase) >= 3 and not is_processor_phrase(phrase)


def _candidates(description: str) -> list[str]:
    """Contiguous merchant-word runs, leading words first, longest first."""
    tokens = [token.lower() for token in merchant_tokens(description)]
    return [
        " ".join(tokens[start:start + length])
        for start in range(len(tokens))
        for length in range(min(MAX_PHRASE_WORDS, len(tokens) - start), 0, -1)
    ]


def rule_keyword(descriptions: Iterable[str], merchant: Optional[str] = None) -> Optional[str]:
    """A phrase contained in every description, or None when there is no reliable one."""
    texts = [text.lower() for text in descriptions if text and text.strip()]
    if not texts:
        return None
    candidates = ([merchant.strip().lower()] if merchant and merchant.strip() else []) + _candidates(texts[0])
    return next((phrase for phrase in candidates if _usable(phrase) and all(phrase in text for text in texts)), None)


def keyword_warning(keyword: str, matches: int, total: int) -> Optional[str]:
    if is_processor_phrase(keyword):
        return f'"{keyword.strip()}" names a payment method, not a merchant, so it would match unrelated purchases.'
    if total >= BROAD_MINIMUM_TRANSACTIONS and matches > total * BROAD_SHARE:
        return f'"{keyword.strip()}" matches {matches:,} of {total:,} saved transactions. A longer phrase is safer.'
    return None


def uncategorized_groups(transactions, limit: int = 8) -> list[dict]:
    """Uncategorized rows grouped by rule phrase, largest groups first.

    Grouping by the phrase (not the full cleaned name) keeps one merchant's
    branches together, and every row in a group contains its phrase.
    """
    grouped: dict[str, list] = defaultdict(list)
    for item in transactions:
        if item.category == "Uncategorized":
            keyword = rule_keyword([item.description], item.merchant_name)
            grouped[keyword or "\0" + merchant_name(item).lower()].append(item)
    result = []
    for key, items in grouped.items():
        keyword = None if key.startswith("\0") else key
        first = items[0].description
        start = first.lower().find(keyword) if keyword else -1
        result.append({
            "merchant": first[start:start + len(keyword)] if start >= 0 else merchant_name(items[0]),
            "keyword": keyword,
            "ids": [item.id for item in items],
            "count": len(items),
            "total": sum((item.amount for item in items), Decimal(0)),
            "sample": first,
        })
    result.sort(key=lambda group: (-group["count"], group["total"], group["merchant"]))
    return result[:limit]
