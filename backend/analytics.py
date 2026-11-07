"""
analytics.py
------------
Pure-Python aggregation helpers behind the month dropdown + the two pie
charts (credits and debits). Kept separate from app.py so it can be
tested without a web server or database.
"""

import calendar
from collections import defaultdict
from datetime import datetime

from dateutil.relativedelta import relativedelta


def available_months(transactions):
    """[{'value': '2019-06', 'label': 'June 2019'}, ...] in chronological order."""
    keys = sorted({(t["date"].year, t["date"].month) for t in transactions})
    return [{"value": f"{y:04d}-{m:02d}", "label": f"{calendar.month_name[m]} {y}"} for y, m in keys]


def monthly_breakdown(transactions, month=None):
    """
    Category totals for money going out (debit) and money coming in (credit).
    month: "YYYY-MM" for one month, or None / "all" for the whole statement.
    """
    if month and month != "all":
        start = datetime.strptime(month, "%Y-%m")          # raises ValueError on bad input
        end = start + relativedelta(months=1)
        rows = [t for t in transactions if start <= t["date"] < end]
    else:
        rows = list(transactions)

    debit, credit = defaultdict(float), defaultdict(float)
    for t in rows:
        bucket = credit if t["direction"] == "credit" else debit
        bucket[t["category"]] += t["amount"]

    def tidy(d):
        return {k: round(v, 2) for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)}

    debit, credit = tidy(debit), tidy(credit)
    return {
        "month": month or "all",
        "debit": debit,
        "credit": credit,
        "total_debit": round(sum(debit.values()), 2),
        "total_credit": round(sum(credit.values()), 2),
        "count": len(rows),
    }
