import hashlib


CATEGORY_RULES = {
    "Salary/Income": ["payroll", "direct deposit", "employer", "salary", "wages"],
    "Housing": ["rent", "mortgage", "hoa", "landlord", "property management"],
    # Card payments move money between the user's own accounts, so analytics
    # treats this category like a transfer instead of new spending.
    "Credit Card Payments": [
        "payment to chase card",
        "credit card payment",
        "applecard payment",
        "payment thank you",
        "card payment",
    ],
    "Debt Payments": [
        "loan payment",
        "auto carpay",
        "carpay",
    ],
    "Groceries": [
        "kroger",
        "trader joe",
        "safeway",
        "whole foods",
        "aldi",
        "costco",
        "h mart",
        "supermarket",
        "grocery",
    ],
    "Dining": [
        "starbucks",
        "uber eats",
        "doordash",
        "chipotle",
        "mcdonald",
        "cafe",
        "restaurant",
        "bakery",
        "boba",
        "coffee",
    ],
    "Transportation": [
        "uber",
        "lyft",
        "gas",
        "shell oil",
        "petro",
        "parking",
        "transit",
        "toll",
        "auto",
    ],
    "Utilities": [
        "electric",
        "water",
        "coned",
        "internet",
        "at&t",
        "verizon",
        "t-mobile",
        "mobile",
        "phone",
    ],
    "Health & Wellness": [
        "pharmacy",
        "cvs",
        "walgreens",
        "doctor",
        "medical",
        "dental",
        "health",
        "fitness",
        "gym",
    ],
    "Shopping": [
        "amazon",
        "target",
        "walmart",
        "applecard",
        "retail",
        "shop",
        "store",
    ],
    "Subscriptions": [
        "netflix",
        "spotify",
        "hulu",
        "disney",
        "youtube premium",
        "adobe",
        "membership",
    ],
    "Entertainment": ["steam", "cinema", "nintendo", "concert", "ticket"],
    "Travel": ["hotel", "airline", "airbnb", "travel", "flight", "chase travel"],
    "Education": ["tuition", "school", "university", "college", "course"],
    "Insurance": ["insurance", "geico", "state farm", "progressive"],
    "Fees & Interest": ["fee", "interest charge", "annual fee", "finance charge"],
    "Cash & ATM": ["atm", "cash withdrawal", "cash deposit"],
    "Gifts & Donations": ["gift", "donation", "charity"],
    "Transfers": ["transfer", "zelle", "venmo", "cash app", "wise", "ach"],
}

CATEGORIES = list(CATEGORY_RULES) + ["Uncategorized"]

# Money moved between the user's own accounts. Counting these as income or
# spending double-counts card purchases and inflates cash in and cash out.
TRANSFER_CATEGORIES = frozenset({"Transfers", "Credit Card Payments"})

# Obligations that stay roughly constant month to month.
FIXED_CATEGORIES = frozenset({"Housing", "Utilities", "Insurance", "Debt Payments", "Education"})


def auto_categorize(description: str) -> str:
    description_lower = description.lower()
    for category, keywords in CATEGORY_RULES.items():
        if any(keyword in description_lower for keyword in keywords):
            return category
    return "Uncategorized"


def categorize_with_custom_rules(description: str, rules) -> str:
    """Apply user rules first, then fall back to built-in categorization."""
    description_lower = description.lower()
    for rule in rules:
        keyword = rule.keyword if hasattr(rule, "keyword") else rule["keyword"]
        category = rule.category if hasattr(rule, "category") else rule["category"]
        if keyword.strip().lower() in description_lower:
            return category
    return auto_categorize(description)


def generate_tx_id(
    date_str: str,
    amount: float,
    description: str,
    account_name: str = "",
    account_type: str = "",
) -> str:
    payload = (
        f"{account_type.strip().lower()}_{account_name.strip().lower()}_"
        f"{date_str}_{amount:.2f}_{description.strip().lower()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
