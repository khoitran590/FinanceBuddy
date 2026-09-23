import calendar
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean, median, pstdev
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.domain.models import Budget, SavingsGoal, Transaction
from src.services.categorizer import FIXED_CATEGORIES, TRANSFER_CATEGORIES


WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEK_OF_MONTH_BUCKETS = (("Days 1–7", 1, 7), ("Days 8–14", 8, 14), ("Days 15–21", 15, 21), ("Days 22–end", 22, 31))
PAYMENT_CHANNEL_LABELS = {"online": "Online", "in store": "In store", "other": "Other"}

# (label, shortest interval, longest interval, charges per year). Intervals are days.
# Intervals of 12-17 days are split into "Every 2 weeks" and "Twice a month" later.
CADENCES = (
    ("Weekly", 5, 9, 52),
    ("Every 2 weeks", 12, 17, 26),
    ("Monthly", 26, 35, 12),
    ("Quarterly", 80, 100, 4),
    ("Yearly", 340, 390, 1),
)
MONTHS_PER_CHARGE = {"Monthly": 1, "Quarterly": 3, "Yearly": 12}
# A charge whose amounts vary more than this (largest / smallest) is not one bill.
MAX_RECURRING_SPREAD = 1.5

# Card processors and bank memo prefixes that hide the merchant's actual name.
_MERCHANT_PREFIX = re.compile(
    r"^(?:(?:sq|tst|sp|pp|py|paypal)\s*\*\s*|pos\s+(?:purchase\s+|debit\s+)?|"
    r"debit card purchase\s+|purchase authorized on\s+\S+\s+|checkcard\s+\S*\s*|"
    r"card purchase\s+|recurring payment\s+|ach debit\s+)",
    re.IGNORECASE,
)
_SPLIT_SUFFIX = re.compile(r"\s*\(split \d+\)$", re.IGNORECASE)
_MERCHANT_ALIASES = (
    (re.compile(r"\b(?:amzn|amazon)\b", re.IGNORECASE), "Amazon"),
    (re.compile(r"\buber\s*eats\b", re.IGNORECASE), "Uber Eats"),
    (re.compile(r"\buber\b", re.IGNORECASE), "Uber"),
    (re.compile(r"\blyft\b", re.IGNORECASE), "Lyft"),
    (re.compile(r"\bdoordash\b", re.IGNORECASE), "DoorDash"),
    (re.compile(r"\bstarbucks\b", re.IGNORECASE), "Starbucks"),
    (re.compile(r"\bnetflix\b", re.IGNORECASE), "Netflix"),
    (re.compile(r"\bspotify\b", re.IGNORECASE), "Spotify"),
    (re.compile(r"\bapple\.com\b", re.IGNORECASE), "Apple"),
    (re.compile(r"\bcostco\b", re.IGNORECASE), "Costco"),
    (re.compile(r"\bwalmart\b|\bwal-mart\b", re.IGNORECASE), "Walmart"),
    (re.compile(r"\btarget\b", re.IGNORECASE), "Target"),
)


def clean_merchant(description: str) -> str:
    """Reduce a bank memo to a stable merchant name for grouping.

    Store numbers, card-processor prefixes, and reference codes differ between
    charges from the same merchant, so they are removed before grouping.
    """
    text = _SPLIT_SUFFIX.sub("", description.strip())
    previous = None
    while previous != text:
        previous = text
        text = _MERCHANT_PREFIX.sub("", text).strip()
    for pattern, alias in _MERCHANT_ALIASES:
        if pattern.search(text):
            return alias
    tokens = []
    for token in re.sub(r"[*#]", " ", text).split():
        if sum(character.isdigit() for character in token) >= 3:
            continue
        if not any(character.isalnum() for character in token):
            continue
        tokens.append(token)
    return " ".join(tokens) or description.strip()


def merchant_name(transaction: Transaction) -> str:
    return transaction.merchant_name or clean_merchant(transaction.description)


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


def _month_bounds(key: str) -> Tuple[date, date]:
    year, month = (int(part) for part in key.split("-"))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _money(value: float) -> str:
    return f"${value:,.2f}" if abs(value) < 100 else f"${value:,.0f}"


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + value.month - 1 + months
    year, month = index // 12, index % 12 + 1
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def _next_occurrence(dates: Sequence[date], cadence: Dict[str, Any]) -> date:
    """Predict the next date in a schedule, keeping calendar-based days of month."""
    ordered = sorted(set(dates))
    label = cadence["label"]
    if label in MONTHS_PER_CHARGE:
        return _add_months(ordered[-1], MONTHS_PER_CHARGE[label])
    if label == "Twice a month" and len(ordered) >= 2:
        # Paydays alternate between two days of the month, e.g. the 1st and 15th.
        return _add_months(ordered[-2], 1)
    return ordered[-1] + timedelta(days=round(cadence["interval_days"]))


def _date_range(start: date, end: date) -> Iterable[date]:
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


class AnalyticsService:
    """Pure calculations that can be translated directly into Swift methods."""

    # ------------------------------------------------------------------ basics

    @staticmethod
    def is_transfer(transaction: Transaction) -> bool:
        return transaction.category in TRANSFER_CATEGORIES

    @staticmethod
    def exclude_transfers(transactions: List[Transaction]) -> List[Transaction]:
        """Drop money moved between the user's own accounts."""
        return [item for item in transactions if item.category not in TRANSFER_CATEGORIES]

    @staticmethod
    def calculate_summary(transactions: List[Transaction]) -> Dict[str, float]:
        inflow = sum(transaction.amount for transaction in transactions if transaction.amount > 0)
        outflow = sum(abs(transaction.amount) for transaction in transactions if transaction.amount < 0)
        net_savings = inflow - outflow
        savings_rate = (net_savings / inflow * 100.0) if inflow > 0 else 0.0

        return {
            "inflow": inflow,
            "outflow": outflow,
            "net_savings": net_savings,
            "savings_rate": savings_rate,
        }

    @staticmethod
    def dashboard_metrics(
        transactions: List[Transaction],
        previous_transactions: Optional[List[Transaction]] = None,
        include_transfers: bool = False,
    ) -> List[Dict[str, Any]]:
        """Return labels that match the financial meaning of the selected accounts.

        Transfers and card payments are excluded from income and spending unless
        ``include_transfers`` is set, and are reported as their own metric. When a
        previous period is supplied, each metric carries its change from it.
        """
        metrics = AnalyticsService._base_metrics(transactions, include_transfers)
        if previous_transactions:
            previous = {
                item["label"]: item
                for item in AnalyticsService._base_metrics(previous_transactions, include_transfers)
            }
            for metric in metrics:
                earlier = previous.get(metric["label"])
                if earlier is not None and metric["trend"] != "neutral":
                    metric["delta"] = metric["value"] - earlier["value"]
        for metric in metrics:
            metric["delta_color"] = {
                "higher": "normal", "lower": "inverse", "neutral": "off"
            }[metric.pop("trend")]
        return metrics

    @staticmethod
    def _base_metrics(transactions: List[Transaction], include_transfers: bool) -> List[Dict[str, Any]]:
        account_types = {transaction.account_type for transaction in transactions}
        if account_types == {"Credit Card"}:
            # Card payments are what reduce a card balance, so the card view keeps them.
            summary = AnalyticsService.calculate_summary(transactions)
            return [
                {"label": "Payments & credits", "value": summary["inflow"], "format": "currency", "trend": "higher"},
                {"label": "Purchases & fees", "value": summary["outflow"], "format": "currency", "trend": "lower"},
                {
                    "label": "Balance change",
                    "value": summary["outflow"] - summary["inflow"],
                    "format": "currency",
                    "help": "Positive means the card balance increased during this period.",
                    "trend": "lower",
                },
                {
                    "label": "Transactions",
                    "value": float(len(transactions)),
                    "format": "integer",
                    "trend": "neutral",
                },
            ]

        counted = transactions if include_transfers else AnalyticsService.exclude_transfers(transactions)
        summary = AnalyticsService.calculate_summary(counted)
        checking_only = account_types == {"Checking"}
        metrics = [
            {"label": "Income" if checking_only else "Money in", "value": summary["inflow"], "format": "currency", "trend": "higher"},
            {"label": "Spending", "value": summary["outflow"], "format": "currency", "trend": "lower"},
            {
                "label": "Net savings" if checking_only else "Net cash flow",
                "value": summary["net_savings"],
                "format": "currency",
                "trend": "higher",
            },
            {
                "label": "Savings rate",
                "value": summary["savings_rate"],
                "format": "percent",
                "help": "Net savings divided by income for the selected period.",
                "trend": "higher",
            },
        ]
        if not include_transfers:
            moved = sum(
                abs(item.amount)
                for item in transactions
                if item.amount < 0 and AnalyticsService.is_transfer(item)
            )
            if moved:
                metrics.append(
                    {
                        "label": "Moved between accounts",
                        "value": moved,
                        "format": "currency",
                        "help": (
                            "Transfers and credit-card payments leaving these accounts. They are left out "
                            "of income and spending so a card purchase is not counted twice."
                        ),
                        "trend": "neutral",
                    }
                )
        return metrics

    @staticmethod
    def preset_range(preset: str, min_date: date, max_date: date) -> Tuple[date, date]:
        """Resolve a quick date range, measured back from the latest transaction.

        Imported statements can end long before today, so anchoring on the most
        recent activity keeps presets such as "Last 30 days" useful.
        """
        anchor = max_date

        def months_back(count: int) -> date:
            index = anchor.year * 12 + anchor.month - 1 - count
            return date(index // 12, index % 12 + 1, 1)

        if preset == "This month":
            start, end = anchor.replace(day=1), anchor
        elif preset == "Last month":
            end = anchor.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
        elif preset == "Last 30 days":
            start, end = anchor - timedelta(days=29), anchor
        elif preset == "Last 3 months":
            start, end = months_back(2), anchor
        elif preset == "Year to date":
            start, end = date(anchor.year, 1, 1), anchor
        elif preset == "Last 12 months":
            start, end = months_back(11), anchor
        else:
            start, end = min_date, max_date
        start = min(max(start, min_date), max_date)
        end = min(max(end, min_date), max_date)
        return (start, end) if start <= end else (end, end)

    @staticmethod
    def previous_period(start: date, end: date) -> Tuple[date, date]:
        """Return the comparable window just before ``start``.

        A range starting on the 1st shifts back by the months it spans, so
        September 1–22 compares with August 1–22 and monthly bills such as rent
        land in both windows. Other ranges use the equally long window that ends
        the day before ``start``.
        """
        if start.day == 1:
            months = (end.year - start.year) * 12 + end.month - start.month + 1
            previous_end = _add_months(end, -months)
            if end.day == calendar.monthrange(end.year, end.month)[1]:
                previous_end = previous_end.replace(
                    day=calendar.monthrange(previous_end.year, previous_end.month)[1]
                )
            return _add_months(start, -months), previous_end
        length = (end - start).days + 1
        return start - timedelta(days=length), start - timedelta(days=1)

    @staticmethod
    def filter_transactions(
        transactions: List[Transaction],
        start_date: date,
        end_date: date,
        accounts: List[str],
        categories: List[str],
        transaction_types: List[str],
        search: str = "",
        minimum_amount: float = 0.0,
    ) -> List[Transaction]:
        search_lower = search.strip().lower()
        results = []
        for transaction in transactions:
            kind = "Income / credit" if transaction.amount > 0 else "Expense / purchase"
            if not start_date <= transaction.date <= end_date:
                continue
            # An empty account selection intentionally means "show no accounts" so
            # users can clear the dashboard before choosing another account.
            if transaction.account_name not in accounts:
                continue
            if categories and transaction.category not in categories:
                continue
            if transaction_types and kind not in transaction_types:
                continue
            if abs(transaction.amount) < minimum_amount:
                continue
            if search_lower and search_lower not in transaction.description.lower():
                continue
            results.append(transaction)
        return results

    @staticmethod
    def cumulative_cash_flow(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Return daily cumulative net flow relative to the selected period's start."""
        daily: Dict[date, float] = defaultdict(float)
        for transaction in transactions:
            daily[transaction.date] += transaction.amount
        cumulative = 0.0
        points = []
        for transaction_date, amount in sorted(daily.items()):
            cumulative += amount
            points.append(
                {
                    "date": transaction_date,
                    "pnl": round(cumulative, 2),
                    "daily_change": round(amount, 2),
                }
            )
        return points

    @staticmethod
    def monthly_breakdown(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        months = defaultdict(lambda: {"inflow": 0.0, "outflow": 0.0})
        for transaction in transactions:
            month = transaction.date.strftime("%Y-%m")
            if transaction.amount > 0:
                months[month]["inflow"] += transaction.amount
            else:
                months[month]["outflow"] += abs(transaction.amount)

        return sorted(
            [{"month": month, **values} for month, values in months.items()],
            key=lambda item: item["month"],
        )

    @staticmethod
    def monthly_savings_rate(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Monthly net cash flow and savings rate; the rate is None without income."""
        return [
            {
                **item,
                "net": item["inflow"] - item["outflow"],
                "savings_rate": (
                    (item["inflow"] - item["outflow"]) / item["inflow"] * 100.0
                    if item["inflow"] > 0
                    else None
                ),
            }
            for item in AnalyticsService.monthly_breakdown(transactions)
        ]

    @staticmethod
    def complete_months(transactions: List[Transaction]) -> List[str]:
        """Months fully covered by the data, so partial months don't skew averages.

        A month is complete when the history starts no later than its third day
        and ends no earlier than three days before its last day.
        """
        if not transactions:
            return []
        first = min(item.date for item in transactions)
        last = max(item.date for item in transactions)
        complete = []
        for key in sorted({_month_key(item.date) for item in transactions}):
            start, end = _month_bounds(key)
            if first <= start + timedelta(days=3) and last >= end - timedelta(days=3):
                complete.append(key)
        return complete

    # -------------------------------------------------------------- categories

    @staticmethod
    def category_expenses(transactions: List[Transaction]) -> Dict[str, float]:
        breakdown = defaultdict(float)
        for transaction in transactions:
            if transaction.amount < 0:
                breakdown[transaction.category] += abs(transaction.amount)
        return dict(breakdown)

    @staticmethod
    def category_summary(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Return category totals with share, count, and average spend."""
        grouped: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"amount": 0.0, "transactions": 0}
        )
        for transaction in transactions:
            if transaction.amount < 0:
                grouped[transaction.category]["amount"] += abs(transaction.amount)
                grouped[transaction.category]["transactions"] += 1

        total = sum(item["amount"] for item in grouped.values())
        return [
            {
                "category": category,
                "amount": values["amount"],
                "transactions": int(values["transactions"]),
                "share": (values["amount"] / total * 100.0) if total else 0.0,
                "average": (
                    values["amount"] / values["transactions"]
                    if values["transactions"]
                    else 0.0
                ),
            }
            for category, values in sorted(
                grouped.items(), key=lambda item: item[1]["amount"], reverse=True
            )
        ]

    @staticmethod
    def monthly_category_expenses(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        grouped = defaultdict(float)
        for transaction in transactions:
            if transaction.amount < 0:
                grouped[(transaction.date.strftime("%Y-%m"), transaction.category)] += abs(
                    transaction.amount
                )
        return [
            {"month": month, "category": category, "amount": amount}
            for (month, category), amount in sorted(grouped.items())
        ]

    @staticmethod
    def subcategory_summary(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Spending by the bank's detailed category, when a bank connection supplies one."""
        grouped: Dict[str, Dict[str, float]] = defaultdict(lambda: {"amount": 0.0, "transactions": 0})
        for item in transactions:
            if item.amount < 0 and item.subcategory:
                grouped[item.subcategory]["amount"] += abs(item.amount)
                grouped[item.subcategory]["transactions"] += 1
        return [
            {"subcategory": name, "amount": values["amount"], "transactions": int(values["transactions"])}
            for name, values in sorted(grouped.items(), key=lambda item: item[1]["amount"], reverse=True)
        ]

    @staticmethod
    def fixed_vs_flexible(transactions: List[Transaction]) -> Dict[str, Any]:
        """Split spending into fixed obligations and flexible, day-to-day spending."""
        months: Dict[str, Dict[str, float]] = defaultdict(lambda: {"fixed": 0.0, "flexible": 0.0})
        for item in transactions:
            if item.amount >= 0:
                continue
            kind = "fixed" if item.category in FIXED_CATEGORIES else "flexible"
            months[_month_key(item.date)][kind] += abs(item.amount)
        fixed = sum(values["fixed"] for values in months.values())
        flexible = sum(values["flexible"] for values in months.values())
        total = fixed + flexible
        return {
            "fixed": fixed,
            "flexible": flexible,
            "fixed_share": fixed / total * 100.0 if total else 0.0,
            "months": [{"month": month, **values} for month, values in sorted(months.items())],
        }

    @staticmethod
    def category_changes(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Compare category spending between two like-for-like periods.

        A partial latest month is compared with the same days of the previous
        month; otherwise the last two complete months are compared.
        """
        expenses = [item for item in transactions if item.amount < 0]
        months = sorted({_month_key(item.date) for item in expenses})
        if len(months) < 2:
            return []
        last = max(item.date for item in transactions)
        complete = set(AnalyticsService.complete_months(transactions))
        current_key, previous_key = months[-1], months[-2]
        if current_key in complete and previous_key in complete:
            current_range = _month_bounds(current_key)
            previous_range = _month_bounds(previous_key)
            labels = (
                current_range[0].strftime("%b %Y"),
                previous_range[0].strftime("%b %Y"),
            )
        elif current_key not in complete and previous_key in complete:
            current_start = _month_bounds(current_key)[0]
            previous_start, previous_end = _month_bounds(previous_key)
            current_range = (current_start, last)
            previous_range = (
                previous_start,
                min(previous_start + timedelta(days=last.day - 1), previous_end),
            )
            labels = (
                f"{current_start.strftime('%b')} 1–{last.day}",
                f"{previous_start.strftime('%b')} 1–{previous_range[1].day}",
            )
        else:
            full = [key for key in months if key in complete]
            if len(full) < 2:
                return []
            current_range = _month_bounds(full[-1])
            previous_range = _month_bounds(full[-2])
            labels = (
                current_range[0].strftime("%b %Y"),
                previous_range[0].strftime("%b %Y"),
            )

        def totals(bounds: Tuple[date, date]) -> Dict[str, float]:
            return AnalyticsService.category_expenses(
                [item for item in expenses if bounds[0] <= item.date <= bounds[1]]
            )

        current, previous = totals(current_range), totals(previous_range)
        return sorted(
            [
                {
                    "category": category,
                    "change": current.get(category, 0.0) - previous.get(category, 0.0),
                    "current": current.get(category, 0.0),
                    "previous": previous.get(category, 0.0),
                    "current_month": labels[0],
                    "previous_month": labels[1],
                }
                for category in set(previous) | set(current)
            ],
            key=lambda item: abs(item["change"]),
            reverse=True,
        )

    # --------------------------------------------------------------- merchants

    @staticmethod
    def merchant_summary(transactions: List[Transaction], limit: int = 10) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, Any]] = {}
        total = 0.0
        for item in transactions:
            if item.amount >= 0:
                continue
            name = merchant_name(item)
            entry = grouped.setdefault(
                name.lower(),
                {"merchant": name, "amount": 0.0, "visits": 0, "categories": Counter(), "last_date": item.date},
            )
            entry["amount"] += abs(item.amount)
            entry["visits"] += 1
            entry["categories"][item.category] += abs(item.amount)
            entry["last_date"] = max(entry["last_date"], item.date)
            total += abs(item.amount)
        rows = sorted(grouped.values(), key=lambda entry: entry["amount"], reverse=True)[:limit]
        return [
            {
                "merchant": entry["merchant"],
                "amount": entry["amount"],
                "visits": entry["visits"],
                "average": entry["amount"] / entry["visits"],
                "share": entry["amount"] / total * 100.0 if total else 0.0,
                "category": entry["categories"].most_common(1)[0][0],
                "last_date": entry["last_date"],
            }
            for entry in rows
        ]

    @staticmethod
    def payment_channel_summary(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, float]] = defaultdict(lambda: {"amount": 0.0, "transactions": 0})
        for item in transactions:
            if item.amount < 0 and item.payment_channel:
                label = PAYMENT_CHANNEL_LABELS.get(item.payment_channel.lower(), item.payment_channel.title())
                grouped[label]["amount"] += abs(item.amount)
                grouped[label]["transactions"] += 1
        total = sum(values["amount"] for values in grouped.values())
        return [
            {
                "channel": label,
                "amount": values["amount"],
                "transactions": int(values["transactions"]),
                "share": values["amount"] / total * 100.0 if total else 0.0,
            }
            for label, values in sorted(grouped.items(), key=lambda item: item[1]["amount"], reverse=True)
        ]

    @staticmethod
    def location_summary(transactions: List[Transaction], limit: int = 8) -> List[Dict[str, Any]]:
        grouped: Dict[str, Dict[str, float]] = defaultdict(lambda: {"amount": 0.0, "transactions": 0})
        for item in transactions:
            if item.amount < 0 and item.location:
                grouped[item.location]["amount"] += abs(item.amount)
                grouped[item.location]["transactions"] += 1
        return [
            {"location": name, "amount": values["amount"], "transactions": int(values["transactions"])}
            for name, values in sorted(
                grouped.items(), key=lambda item: item[1]["amount"], reverse=True
            )[:limit]
        ]

    # ------------------------------------------------------------ time habits

    @staticmethod
    def _period(transactions: List[Transaction], start: Optional[date], end: Optional[date]) -> Tuple[date, date]:
        return (
            start or min(item.date for item in transactions),
            end or max(item.date for item in transactions),
        )

    @staticmethod
    def weekday_spending(
        transactions: List[Transaction], start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """Spending per weekday, averaged over how often that weekday occurs in the period."""
        if not any(item.amount < 0 for item in transactions):
            return []
        start, end = AnalyticsService._period(transactions, start, end)
        full_weeks, remainder = divmod((end - start).days + 1, 7)
        day_counts = [full_weeks] * 7
        for offset in range(remainder):
            day_counts[(start.weekday() + offset) % 7] += 1
        totals = [0.0] * 7
        counts = [0] * 7
        for item in transactions:
            if item.amount < 0 and start <= item.date <= end:
                totals[item.date.weekday()] += abs(item.amount)
                counts[item.date.weekday()] += 1
        return [
            {
                "day": WEEKDAY_NAMES[index],
                "total": totals[index],
                "transactions": counts[index],
                "days": day_counts[index],
                "average_per_day": totals[index] / day_counts[index] if day_counts[index] else 0.0,
            }
            for index in range(7)
        ]

    @staticmethod
    def weekend_vs_weekday(weekday_rows: List[Dict[str, Any]]) -> Optional[Dict[str, float]]:
        weekend = [row for row in weekday_rows if row["day"] in ("Saturday", "Sunday")]
        weekdays = [row for row in weekday_rows if row["day"] not in ("Saturday", "Sunday")]
        weekend_days = sum(row["days"] for row in weekend)
        weekday_days = sum(row["days"] for row in weekdays)
        if not weekend_days or not weekday_days:
            return None
        weekend_average = sum(row["total"] for row in weekend) / weekend_days
        weekday_average = sum(row["total"] for row in weekdays) / weekday_days
        return {
            "weekend": weekend_average,
            "weekday": weekday_average,
            "difference": (
                (weekend_average - weekday_average) / weekday_average * 100.0 if weekday_average else 0.0
            ),
        }

    @staticmethod
    def week_of_month_spending(
        transactions: List[Transaction], start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        if not any(item.amount < 0 for item in transactions):
            return []
        start, end = AnalyticsService._period(transactions, start, end)
        day_counts = [0] * len(WEEK_OF_MONTH_BUCKETS)
        for day in _date_range(start, end):
            day_counts[min((day.day - 1) // 7, 3)] += 1
        totals = [0.0] * len(WEEK_OF_MONTH_BUCKETS)
        for item in transactions:
            if item.amount < 0 and start <= item.date <= end:
                totals[min((item.date.day - 1) // 7, 3)] += abs(item.amount)
        grand_total = sum(totals)
        return [
            {
                "bucket": label,
                "total": totals[index],
                "share": totals[index] / grand_total * 100.0 if grand_total else 0.0,
                "average_per_day": totals[index] / day_counts[index] if day_counts[index] else 0.0,
            }
            for index, (label, _, _) in enumerate(WEEK_OF_MONTH_BUCKETS)
        ]

    @staticmethod
    def daily_spending(
        transactions: List[Transaction], start: Optional[date] = None, end: Optional[date] = None
    ) -> List[Dict[str, Any]]:
        """Zero-filled daily spending with a trailing seven-day average."""
        if not transactions:
            return []
        start, end = AnalyticsService._period(transactions, start, end)
        totals: Dict[date, float] = defaultdict(float)
        for item in transactions:
            if item.amount < 0:
                totals[item.date] += abs(item.amount)
        days = list(_date_range(start, end))
        points = []
        for index, day in enumerate(days):
            window = days[max(0, index - 6):index + 1]
            points.append(
                {
                    "date": day,
                    "spend": totals.get(day, 0.0),
                    "rolling_7": sum(totals.get(value, 0.0) for value in window) / len(window),
                }
            )
        return points

    @staticmethod
    def spending_pace(
        transactions: List[Transaction], start: Optional[date] = None, end: Optional[date] = None
    ) -> Dict[str, float]:
        daily = AnalyticsService.daily_spending(transactions, start, end)
        if not daily:
            return {"average_daily": 0.0, "last_7_days": 0.0, "last_30_days": 0.0, "days": 0}
        spend = [point["spend"] for point in daily]
        return {
            "average_daily": sum(spend) / len(spend),
            "last_7_days": mean(spend[-7:]),
            "last_30_days": mean(spend[-30:]),
            "days": len(spend),
        }

    # --------------------------------------------------------------- recurring

    @staticmethod
    def detect_cadence(dates: Sequence[date]) -> Optional[Dict[str, Any]]:
        """Classify the spacing of repeated dates, or None when it is irregular."""
        unique = sorted(set(dates))
        if len(unique) < 2:
            return None
        intervals = [(later - earlier).days for earlier, later in zip(unique, unique[1:])]
        typical = median(intervals)
        for label, shortest, longest, per_year in CADENCES:
            if shortest <= typical <= longest:
                regular = sum(shortest <= interval <= longest for interval in intervals)
                if regular / len(intervals) < 0.6:
                    return None
                if label == "Every 2 weeks" and len(intervals) >= 2:
                    # True biweekly schedules repeat every 14 days apart from the odd
                    # holiday shift. Fixed days of the month (1st and 15th) also give
                    # 14-day gaps, but only about half the time.
                    if sum(interval == 14 for interval in intervals) / len(intervals) < 0.75:
                        return {"label": "Twice a month", "interval_days": 365.25 / 24, "per_year": 24}
                return {"label": label, "interval_days": typical, "per_year": per_year}
        return None

    @staticmethod
    def _amount_clusters(items: List[Transaction]) -> List[Tuple[List[Transaction], bool]]:
        """Group one merchant's charges into candidate recurring series.

        Amounts are chained while each step is within 35%, so a gradually rising
        bill stays together. A chain that spreads too widely (everyday purchases
        at the same store) is split into exact-amount groups instead, which still
        finds a fixed-price membership bought alongside other purchases. Those
        exact-amount groups are flagged so callers can demand more evidence, since
        two identical everyday purchases can line up by coincidence.
        """
        chains: List[List[Transaction]] = []
        for item in sorted(items, key=lambda value: abs(value.amount)):
            if chains and abs(item.amount) <= abs(chains[-1][-1].amount) * 1.35 + 0.01:
                chains[-1].append(item)
            else:
                chains.append([item])
        clusters: List[Tuple[List[Transaction], bool]] = []
        for chain in chains:
            smallest, largest = abs(chain[0].amount), abs(chain[-1].amount)
            if largest <= smallest * MAX_RECURRING_SPREAD + 0.01:
                clusters.append((chain, False))
                continue
            exact: Dict[float, List[Transaction]] = defaultdict(list)
            for item in chain:
                exact[round(abs(item.amount), 2)].append(item)
            clusters.extend((group, True) for group in exact.values())
        return clusters

    @staticmethod
    def _price_change(cluster: List[Transaction]) -> Tuple[float, float, Optional[date]]:
        """Latest price change in a date-ordered series: (change, old price, first new charge).

        Bills that vary nearly every time, like electricity, have no single price.
        """
        amounts = [round(abs(item.amount), 2) for item in cluster]
        latest = amounts[-1]
        if len(set(amounts)) > 3:
            return 0.0, latest, None
        for index in range(len(amounts) - 2, -1, -1):
            change = round(latest - amounts[index], 2)
            if abs(change) >= 0.5 and abs(change) >= amounts[index] * 0.02:
                # Only a price that held for at least two charges counts as the old price.
                if index == 0 or abs(amounts[index - 1] - amounts[index]) > amounts[index] * 0.02:
                    return 0.0, latest, None
                return change, amounts[index], cluster[index + 1].date
        return 0.0, latest, None

    @staticmethod
    def _recurring_groups(transactions: List[Transaction]) -> List[Tuple[Dict[str, Any], List[Transaction]]]:
        groups: Dict[str, List[Transaction]] = defaultdict(list)
        for item in transactions:
            if item.amount < 0:
                groups[merchant_name(item).lower()].append(item)
        if not groups:
            return []
        reference = max(item.date for item in transactions)
        found = []
        for items in groups.values():
            for cluster, coincidental in AnalyticsService._amount_clusters(items):
                if len(cluster) < (3 if coincidental else 2):
                    continue
                cluster.sort(key=lambda value: value.date)
                cadence = AnalyticsService.detect_cadence([item.date for item in cluster])
                if not cadence:
                    continue
                months = {_month_key(item.date) for item in cluster}
                # Two charges a week apart are weak evidence; require a third, and a
                # mostly fixed amount so habits like weekly coffee aren't mistaken for bills.
                if cadence["per_year"] >= 24:
                    typical_amount = median(abs(item.amount) for item in cluster)
                    steady = sum(abs(abs(item.amount) - typical_amount) <= typical_amount * 0.05 for item in cluster)
                    if len(cluster) < 3 or steady / len(cluster) < 0.5:
                        continue
                if cadence["per_year"] <= 12 and len(months) < 2:
                    continue
                latest = cluster[-1]
                amount = round(abs(latest.amount), 2)
                change, previous_amount, changed_on = AnalyticsService._price_change(cluster)
                interval = cadence["interval_days"]
                next_date = _next_occurrence([item.date for item in cluster], cadence)
                grace = timedelta(days=max(7, round(interval * 0.5)))
                found.append(
                    (
                        {
                            "merchant": merchant_name(latest),
                            "amount": amount,
                            "previous_amount": previous_amount,
                            "price_change": change,
                            "price_changed_on": changed_on,
                            "cadence": cadence["label"],
                            "interval_days": interval,
                            "frequency": len(cluster),
                            "months": len(months),
                            "last_date": latest.date,
                            "next_date": next_date,
                            "annual_cost": amount * cadence["per_year"],
                            "monthly_cost": amount * cadence["per_year"] / 12,
                            "category": latest.category,
                            # A charge that stopped arriving was probably cancelled.
                            "active": next_date + grace >= reference,
                        },
                        cluster,
                    )
                )
        return sorted(found, key=lambda pair: pair[0]["annual_cost"], reverse=True)

    @staticmethod
    def recurring_expenses(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Find charges repeating on a regular schedule at a similar amount.

        Amounts may drift, so a price increase is reported instead of hiding the
        subscription.
        """
        return [summary for summary, _ in AnalyticsService._recurring_groups(transactions)]

    @staticmethod
    def upcoming_charges(
        recurring: List[Dict[str, Any]], start: date, end: date
    ) -> List[Dict[str, Any]]:
        """Expected recurring charges dated from ``start`` through ``end``."""
        charges = []
        for item in recurring:
            if not item.get("active", True):
                continue
            cadence = {"label": item["cadence"], "interval_days": item["interval_days"]}
            due = item["next_date"]
            while due < start:
                due = AnalyticsService._advance(due, cadence)
            while due <= end:
                charges.append({"merchant": item["merchant"], "amount": item["amount"], "date": due})
                due = AnalyticsService._advance(due, cadence)
        return sorted(charges, key=lambda charge: charge["date"])

    @staticmethod
    def price_increases(
        recurring: List[Dict[str, Any]], transactions: List[Transaction], within_days: int = 180
    ) -> List[Dict[str, Any]]:
        """Active recurring charges whose price rose recently."""
        if not transactions:
            return []
        cutoff = max(item.date for item in transactions) - timedelta(days=within_days)
        return [
            item
            for item in recurring
            if item.get("active", True)
            and item["price_change"] > 0
            and item["price_changed_on"]
            and item["price_changed_on"] >= cutoff
        ]

    @staticmethod
    def _advance(value: date, cadence: Dict[str, Any]) -> date:
        if cadence["label"] in MONTHS_PER_CHARGE:
            return _add_months(value, MONTHS_PER_CHARGE[cadence["label"]])
        return value + timedelta(days=max(round(cadence["interval_days"]), 1))

    @staticmethod
    def unusual_expenses(transactions: List[Transaction]) -> List[Transaction]:
        expenses = [transaction for transaction in transactions if transaction.amount < 0]
        if len(expenses) < 4:
            return []
        typical = median(abs(transaction.amount) for transaction in expenses)
        threshold = max(typical * 3, 100.0)
        return sorted(
            [transaction for transaction in expenses if abs(transaction.amount) >= threshold],
            key=lambda transaction: abs(transaction.amount),
            reverse=True,
        )[:5]

    @staticmethod
    def cash_flow_forecast(transactions: List[Transaction]) -> Dict[str, float]:
        """Estimate next month from known recurring charges plus typical variable spending.

        Averages use complete months when available so a partial current month
        doesn't understate them. The range reflects the lowest and highest recent
        variable spending.
        """
        monthly = AnalyticsService.monthly_breakdown(transactions)
        complete = set(AnalyticsService.complete_months(transactions))
        recent = ([item for item in monthly if item["month"] in complete] or monthly)[-3:]
        if not recent:
            return {
                "inflow": 0.0, "outflow": 0.0, "net": 0.0, "months": 0,
                "recurring": 0.0, "variable": 0.0, "outflow_low": 0.0, "outflow_high": 0.0,
            }
        inflow = sum(item["inflow"] for item in recent) / len(recent)
        groups = [
            pair for pair in AnalyticsService._recurring_groups(transactions) if pair[0]["active"]
        ]
        recurring_ids = {item.id for _, cluster in groups for item in cluster}
        recent_months = {item["month"] for item in recent}
        recurring_by_month: Dict[str, float] = defaultdict(float)
        for item in transactions:
            if item.id in recurring_ids and _month_key(item.date) in recent_months:
                recurring_by_month[_month_key(item.date)] += abs(item.amount)
        variable = [item["outflow"] - recurring_by_month[item["month"]] for item in recent]
        reference = max(item.date for item in transactions)
        due = AnalyticsService.upcoming_charges(
            [summary for summary, _ in groups], reference + timedelta(days=1), reference + timedelta(days=30)
        )
        recurring = sum(charge["amount"] for charge in due)
        variable_average = sum(variable) / len(variable)
        outflow = recurring + variable_average
        return {
            "inflow": inflow,
            "outflow": outflow,
            "net": inflow - outflow,
            "months": len(recent),
            "recurring": recurring,
            "variable": variable_average,
            "outflow_low": recurring + min(variable),
            "outflow_high": recurring + max(variable),
        }

    # ------------------------------------------------------------------ income

    @staticmethod
    def income_summary(transactions: List[Transaction]) -> Optional[Dict[str, Any]]:
        income = [item for item in transactions if item.amount > 0 and item.category == "Salary/Income"]
        if not income:
            return None
        cadence = AnalyticsService.detect_cadence([item.date for item in income])
        last_pay_date = max(item.date for item in income)
        months = AnalyticsService.complete_months(transactions) or sorted(
            {_month_key(item.date) for item in transactions}
        )
        totals: Dict[str, float] = defaultdict(float)
        for item in income:
            totals[_month_key(item.date)] += item.amount
        values = [totals.get(month, 0.0) for month in months]
        average = sum(values) / len(values) if values else 0.0
        variation = pstdev(values) / average * 100.0 if len(values) >= 2 and average else 0.0
        if len(values) < 2:
            stability = "Not enough history"
        elif variation < 10:
            stability = "Stable"
        elif variation < 25:
            stability = "Somewhat variable"
        else:
            stability = "Variable"
        sources: Dict[str, float] = defaultdict(float)
        for item in income:
            sources[merchant_name(item)] += item.amount
        return {
            "paychecks": len(income),
            "typical_paycheck": median(item.amount for item in income),
            "cadence": cadence["label"] if cadence else "Irregular",
            "last_pay_date": last_pay_date,
            "next_pay_date": _next_occurrence([item.date for item in income], cadence) if cadence else None,
            "average_monthly": average,
            "variation": variation,
            "stability": stability,
            "months": [{"month": month, "income": totals.get(month, 0.0)} for month in months],
            "sources": [
                {"source": name, "amount": amount}
                for name, amount in sorted(sources.items(), key=lambda item: item[1], reverse=True)[:3]
            ],
        }

    @staticmethod
    def income_sources(transactions: List[Transaction]) -> Dict[str, float]:
        sources: Dict[str, float] = defaultdict(float)
        for item in transactions:
            if item.amount > 0:
                sources[item.category] += item.amount
        return dict(sources)

    # ---------------------------------------------------------- budgets/goals

    @staticmethod
    def budget_status(
        budgets: List[Budget], transactions: List[Transaction], month: str, today: date
    ) -> List[Dict[str, Any]]:
        """Compare each budget with spending so far and project the month's total.

        Fixed categories such as rent are usually paid once, so they are not
        projected forward from the days elapsed.
        """
        month_start, month_end = _month_bounds(month)
        days = month_end.day
        if today < month_start:
            elapsed = 0
        elif today > month_end:
            elapsed = days
        else:
            elapsed = today.day
        in_progress = 0 < elapsed < days
        spending = AnalyticsService.category_expenses(
            [item for item in transactions if month_start <= item.date <= month_end]
        )
        rows = []
        for budget in budgets:
            spent = spending.get(budget.category, 0.0)
            limit = budget.monthly_limit
            projected = (
                spent / elapsed * days
                if in_progress and budget.category not in FIXED_CATEGORIES
                else spent
            )
            remaining = limit - spent
            if spent > limit:
                status = "Over budget"
            elif in_progress and projected > limit:
                status = "Projected to go over"
            elif in_progress:
                status = "On track"
            elif elapsed == 0:
                status = "Not started"
            else:
                status = "Under budget"
            rows.append(
                {
                    "category": budget.category,
                    "limit": limit,
                    "spent": spent,
                    "remaining": remaining,
                    "projected": projected,
                    "spent_share": spent / limit * 100.0 if limit else 0.0,
                    "elapsed_share": elapsed / days * 100.0,
                    "days_elapsed": elapsed,
                    "days_in_month": days,
                    "daily_allowance": (
                        remaining / (days - elapsed) if in_progress and remaining > 0 else 0.0
                    ),
                    "status": status,
                }
            )
        return rows

    @staticmethod
    def budget_history(
        budgets: List[Budget], transactions: List[Transaction], months: List[str]
    ) -> Dict[str, Any]:
        """Monthly spending against each budget, plus how often it was met."""
        spending: Dict[Tuple[str, str], float] = {
            (item["month"], item["category"]): item["amount"]
            for item in AnalyticsService.monthly_category_expenses(transactions)
        }
        cells = []
        summary = []
        for budget in budgets:
            values = [spending.get((month, budget.category), 0.0) for month in months]
            for month, spent in zip(months, values):
                cells.append(
                    {
                        "category": budget.category,
                        "month": month,
                        "spent": spent,
                        "limit": budget.monthly_limit,
                        "ratio": spent / budget.monthly_limit * 100.0 if budget.monthly_limit else None,
                    }
                )
            if values:
                highest = max(range(len(values)), key=lambda index: values[index])
                summary.append(
                    {
                        "category": budget.category,
                        "limit": budget.monthly_limit,
                        "months_on_budget": sum(value <= budget.monthly_limit for value in values),
                        "months": len(values),
                        "average": sum(values) / len(values),
                        "highest": values[highest],
                        "highest_month": months[highest],
                    }
                )
        return {"cells": cells, "summary": summary}

    @staticmethod
    def average_monthly_savings(transactions: List[Transaction], months: int = 3) -> Optional[float]:
        """Average monthly net cash flow over recent complete months, ignoring transfers."""
        counted = AnalyticsService.exclude_transfers(transactions)
        complete = set(AnalyticsService.complete_months(transactions))
        recent = [
            item for item in AnalyticsService.monthly_breakdown(counted) if item["month"] in complete
        ][-months:]
        if not recent:
            return None
        return sum(item["inflow"] - item["outflow"] for item in recent) / len(recent)

    @staticmethod
    def goal_plan(
        goal: SavingsGoal, today: date, average_monthly_savings: Optional[float]
    ) -> Dict[str, Any]:
        remaining = max(goal.target_amount - goal.current_amount, 0.0)
        plan: Dict[str, Any] = {
            "remaining": remaining,
            "complete": remaining == 0,
            "months_left": None,
            "required_monthly": None,
            "on_track": None,
            "projected_date": None,
        }
        if remaining == 0:
            return plan
        if goal.target_date:
            months_left = max((goal.target_date - today).days / 30.44, 0.0)
            required = remaining / months_left if months_left >= 1 else remaining
            plan["months_left"] = months_left
            plan["required_monthly"] = required
            if average_monthly_savings is not None:
                plan["on_track"] = average_monthly_savings >= required
        if average_monthly_savings and average_monthly_savings > 0:
            plan["projected_date"] = today + timedelta(
                days=round(remaining / average_monthly_savings * 30.44)
            )
        return plan

    # ---------------------------------------------------------------- balances

    @staticmethod
    def utilization_rating(utilization: Optional[float]) -> str:
        if utilization is None:
            return "Unknown"
        if utilization < 10:
            return "Excellent"
        if utilization < 30:
            return "Good"
        if utilization < 50:
            return "High"
        return "Very high"

    @staticmethod
    def balance_overview(accounts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Net worth, spendable cash, and credit utilization from connected accounts."""
        assets = liabilities = cash = 0.0
        cards = []
        counted = 0
        for account in accounts:
            current = account.get("current_balance")
            if current is None:
                continue
            counted += 1
            current = float(current)
            kind = str(account.get("type") or "")
            if kind in ("credit", "loan"):
                liabilities += current
                if kind == "credit":
                    limit = account.get("credit_limit")
                    limit = float(limit) if limit else None
                    utilization = current / limit * 100.0 if limit else None
                    cards.append(
                        {
                            "account": account.get("official_name") or account.get("name") or "Credit card",
                            "mask": account.get("mask"),
                            "balance": current,
                            "limit": limit,
                            "utilization": utilization,
                            "rating": AnalyticsService.utilization_rating(utilization),
                        }
                    )
            else:
                assets += current
                if kind == "depository":
                    available = account.get("available_balance")
                    cash += float(available) if available is not None else current
        limited = [card for card in cards if card["limit"]]
        total_limit = sum(card["limit"] for card in limited)
        utilization = (
            sum(card["balance"] for card in limited) / total_limit * 100.0 if total_limit else None
        )
        return {
            "assets": assets,
            "liabilities": liabilities,
            "net_worth": assets - liabilities,
            "cash_available": cash,
            "credit_utilization": utilization,
            "utilization_rating": AnalyticsService.utilization_rating(utilization),
            "cards": cards,
            "accounts": counted,
        }

    @staticmethod
    def net_worth_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Daily net worth, carrying each account's last known balance forward."""
        by_date: Dict[date, List[Dict[str, Any]]] = defaultdict(list)
        for row in history:
            if row.get("current_balance") is None:
                continue
            as_of = row["as_of"] if isinstance(row["as_of"], date) else date.fromisoformat(str(row["as_of"]))
            by_date[as_of].append(row)
        latest: Dict[str, Tuple[str, float]] = {}
        points = []
        for as_of in sorted(by_date):
            for row in by_date[as_of]:
                latest[row["account_id"]] = (str(row.get("type") or ""), float(row["current_balance"]))
            assets = sum(value for kind, value in latest.values() if kind not in ("credit", "loan"))
            liabilities = sum(value for kind, value in latest.values() if kind in ("credit", "loan"))
            points.append(
                {"date": as_of, "assets": assets, "liabilities": liabilities, "net_worth": assets - liabilities}
            )
        return points

    @staticmethod
    def safe_to_spend(
        cash_available: float,
        recurring: List[Dict[str, Any]],
        next_pay_date: Optional[date],
        today: date,
    ) -> Dict[str, Any]:
        """Cash left after recurring charges expected before the next paycheck."""
        horizon = next_pay_date if next_pay_date and next_pay_date > today else today + timedelta(days=30)
        charges = AnalyticsService.upcoming_charges(recurring, today, horizon)
        committed = sum(charge["amount"] for charge in charges)
        return {
            "cash_available": cash_available,
            "committed": committed,
            "safe_to_spend": cash_available - committed,
            "horizon": horizon,
            "charges": charges,
            "until_payday": bool(next_pay_date and next_pay_date > today),
        }

    # ---------------------------------------------------------------- insights

    @staticmethod
    def insights(
        transactions: List[Transaction],
        history: Optional[List[Transaction]] = None,
        limit: int = 3,
    ) -> List[str]:
        """Short plain-language observations, most actionable first.

        Recurring charges need a longer ``history`` than a short date range holds;
        it defaults to ``transactions``.
        """
        found: List[str] = []
        history = transactions if history is None else history
        recurring = [item for item in AnalyticsService.recurring_expenses(history) if item["active"]]
        for item in AnalyticsService.price_increases(recurring, history):
            found.append(
                f"{item['merchant']} went up from ${item['previous_amount']:,.2f} to "
                f"${item['amount']:,.2f} ({item['cadence'].lower()})."
            )
            break
        growth = [
            item
            for item in AnalyticsService.category_changes(transactions)
            if item["change"] >= 25 and item["change"] >= item["previous"] * 0.1
        ]
        if growth:
            item = growth[0]
            found.append(
                f"{item['category']} spending is up {_money(item['change'])} "
                f"({item['current_month']} vs {item['previous_month']})."
            )
        subscriptions = [item for item in recurring if item["category"] not in FIXED_CATEGORIES]
        if subscriptions:
            yearly = sum(item["annual_cost"] for item in subscriptions)
            found.append(
                f"{len(subscriptions)} subscriptions and memberships cost about {_money(yearly)} a year."
                if len(subscriptions) != 1
                else f"1 subscription or membership costs about {_money(yearly)} a year."
            )
        if transactions:
            split = AnalyticsService.weekend_vs_weekday(AnalyticsService.weekday_spending(transactions))
            if split and split["weekday"] and abs(split["difference"]) >= 15:
                direction = "more" if split["difference"] > 0 else "less"
                found.append(
                    f"You spend {abs(split['difference']):.0f}% {direction} per day on weekends than on weekdays."
                )
        merchants = AnalyticsService.merchant_summary(transactions, limit=1)
        if merchants:
            top = merchants[0]
            found.append(
                f"{top['merchant']} is your top merchant: {_money(top['amount'])} across "
                f"{top['visits']} purchase{'s' if top['visits'] != 1 else ''} ({top['share']:.0f}% of spending)."
            )
        return found[:limit]

    # ------------------------------------------------------------- comparison

    @staticmethod
    def compare_periods(
        periods: List[Dict[str, Any]], include_transfers: bool = False
    ) -> List[Dict[str, Any]]:
        """Compare ordered statement snapshots, oldest to newest."""
        comparisons: List[Dict[str, Any]] = []
        previous_summary = None
        for period in periods:
            transactions = period["transactions"]
            counted = transactions if include_transfers else AnalyticsService.exclude_transfers(transactions)
            summary = AnalyticsService.calculate_summary(counted)
            current = {
                "label": period["label"],
                "summary": summary,
                "categories": AnalyticsService.category_expenses(counted),
                "category_summary": AnalyticsService.category_summary(counted),
                "status": "Baseline" if previous_summary is None else "Mixed",
                "outflow_delta": None,
                "savings_rate_delta": None,
                "net_savings_delta": None,
                "account_types": sorted({item.account_type for item in transactions}),
            }
            if previous_summary is not None:
                outflow_delta = summary["outflow"] - previous_summary["outflow"]
                savings_rate_delta = summary["savings_rate"] - previous_summary["savings_rate"]
                net_savings_delta = summary["net_savings"] - previous_summary["net_savings"]
                current.update(
                    {
                        "outflow_delta": outflow_delta,
                        "savings_rate_delta": savings_rate_delta,
                        "net_savings_delta": net_savings_delta,
                        "status": (
                            AnalyticsService._spending_status(outflow_delta)
                            if {item.account_type for item in transactions} == {"Credit Card"}
                            else AnalyticsService._comparison_status(outflow_delta, savings_rate_delta)
                        ),
                    }
                )
            comparisons.append(current)
            previous_summary = summary
        return comparisons

    @staticmethod
    def duplicate_candidates(transactions: List[Transaction]) -> List[List[Transaction]]:
        grouped: Dict[tuple, List[Transaction]] = defaultdict(list)
        for transaction in transactions:
            key = (
                transaction.account_name,
                transaction.date,
                round(transaction.amount, 2),
                " ".join(transaction.description.lower().split()),
            )
            grouped[key].append(transaction)
        return [items for items in grouped.values() if len(items) > 1]

    @staticmethod
    def _comparison_status(outflow_delta: float, savings_rate_delta: float) -> str:
        epsilon = 1e-9
        if outflow_delta < -epsilon and savings_rate_delta >= -epsilon:
            return "Improved"
        if outflow_delta > epsilon and savings_rate_delta <= epsilon:
            return "Needs attention"
        if abs(outflow_delta) <= epsilon and abs(savings_rate_delta) <= epsilon:
            return "Unchanged"
        return "Mixed"

    @staticmethod
    def _spending_status(outflow_delta: float) -> str:
        epsilon = 1e-9
        if outflow_delta < -epsilon:
            return "Lower spending"
        if outflow_delta > epsilon:
            return "Higher spending"
        return "Unchanged"
