from collections import defaultdict
from datetime import date
from statistics import median
from typing import Any, Dict, List

from src.domain.models import Transaction


class AnalyticsService:
    """Pure calculations that can be translated directly into Swift methods."""

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
    def dashboard_metrics(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Return labels that match the financial meaning of the selected accounts."""
        summary = AnalyticsService.calculate_summary(transactions)
        account_types = {transaction.account_type for transaction in transactions}
        if account_types == {"Credit Card"}:
            return [
                {"label": "Payments & credits", "value": summary["inflow"], "format": "currency"},
                {"label": "Purchases & fees", "value": summary["outflow"], "format": "currency"},
                {
                    "label": "Balance change",
                    "value": summary["outflow"] - summary["inflow"],
                    "format": "currency",
                    "help": "Positive means the card balance increased during this period.",
                },
                {
                    "label": "Transactions",
                    "value": float(len(transactions)),
                    "format": "integer",
                },
            ]
        if account_types == {"Checking"}:
            return [
                {"label": "Income", "value": summary["inflow"], "format": "currency"},
                {"label": "Spending", "value": summary["outflow"], "format": "currency"},
                {"label": "Net savings", "value": summary["net_savings"], "format": "currency"},
                {
                    "label": "Savings rate",
                    "value": summary["savings_rate"],
                    "format": "percent",
                    "help": "Net savings divided by income for the selected period.",
                },
            ]
        return [
            {"label": "Cash in", "value": summary["inflow"], "format": "currency"},
            {"label": "Cash out", "value": summary["outflow"], "format": "currency"},
            {"label": "Net cash flow", "value": summary["net_savings"], "format": "currency"},
            {"label": "Transactions", "value": float(len(transactions)), "format": "integer"},
        ]

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
            if accounts and transaction.account_name not in accounts:
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
    def compare_periods(periods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compare ordered statement snapshots, oldest to newest."""
        comparisons: List[Dict[str, Any]] = []
        previous_summary = None
        for period in periods:
            transactions = period["transactions"]
            summary = AnalyticsService.calculate_summary(transactions)
            current = {
                "label": period["label"],
                "summary": summary,
                "categories": AnalyticsService.category_expenses(transactions),
                "category_summary": AnalyticsService.category_summary(transactions),
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
    def category_changes(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        months = AnalyticsService.monthly_category_expenses(transactions)
        month_names = sorted({item["month"] for item in months})
        if len(month_names) < 2:
            return []
        previous_month, current_month = month_names[-2:]
        previous = {
            item["category"]: item["amount"]
            for item in months
            if item["month"] == previous_month
        }
        current = {
            item["category"]: item["amount"]
            for item in months
            if item["month"] == current_month
        }
        return sorted(
            [
                {
                    "category": category,
                    "change": current.get(category, 0.0) - previous.get(category, 0.0),
                    "previous_month": previous_month,
                    "current_month": current_month,
                }
                for category in set(previous) | set(current)
            ],
            key=lambda item: abs(item["change"]),
            reverse=True,
        )

    @staticmethod
    def recurring_expenses(transactions: List[Transaction]) -> List[Dict[str, Any]]:
        """Find repeated merchant/amount combinations across distinct months."""
        grouped: Dict[tuple, List[Transaction]] = defaultdict(list)
        for transaction in transactions:
            if transaction.amount >= 0:
                continue
            merchant = " ".join(transaction.description.lower().split())
            grouped[(merchant, round(abs(transaction.amount), 2))].append(transaction)
        recurring = []
        for (merchant, amount), items in grouped.items():
            months = {item.date.strftime("%Y-%m") for item in items}
            if len(months) >= 2:
                recurring.append(
                    {
                        "merchant": items[0].description,
                        "amount": amount,
                        "frequency": len(items),
                        "months": len(months),
                    }
                )
        return sorted(recurring, key=lambda item: item["amount"], reverse=True)

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
        monthly = AnalyticsService.monthly_breakdown(transactions)
        recent = monthly[-3:]
        if not recent:
            return {"inflow": 0.0, "outflow": 0.0, "net": 0.0, "months": 0}
        inflow = sum(item["inflow"] for item in recent) / len(recent)
        outflow = sum(item["outflow"] for item in recent) / len(recent)
        return {"inflow": inflow, "outflow": outflow, "net": inflow - outflow, "months": len(recent)}

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
