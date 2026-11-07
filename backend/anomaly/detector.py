"""
detector.py
-----------
"Flag anything unusual" is answered with plain statistics -- no AI
model needed here, just well-understood outlier-detection techniques
applied per category so a big rent payment doesn't get flagged just
because it's bigger than a coffee:

1. Z-score outliers: within each category, flag debits whose amount is
   more than Z_THRESHOLD standard deviations above that category's mean
   (only needs >=4 samples in the category to be meaningful).
2. Duplicate charges: same amount + same/very similar description
   within DUPLICATE_WINDOW_HOURS of each other (classic double-charge /
   duplicate UPI push pattern).
3. New-merchant large spend: a description never seen before in the
   user's history, above NEW_MERCHANT_ABS_THRESHOLD, gets a soft flag.
4. Odd-hour transactions: debits posted late night (00:00-05:00) --
   many statements include a timestamp; if not available this check is
   skipped automatically.
"""

from collections import defaultdict
from datetime import timedelta
import statistics

Z_THRESHOLD = 2.5
DUPLICATE_WINDOW_HOURS = 24
NEW_MERCHANT_ABS_THRESHOLD = 5000


def _desc_key(description: str) -> str:
    return "".join(ch for ch in description.lower() if ch.isalnum())[:24]


def detect_anomalies(transactions):
    """
    transactions: list of dicts with keys
        id, date (datetime), description, amount, direction, category
    Returns: list of {transaction_id, reason} for flagged transactions.
    """
    flags = {}

    # --- 1. Per-category z-score outliers (debits only) ---
    by_category = defaultdict(list)
    for t in transactions:
        if t["direction"] == "debit":
            by_category[t["category"]].append(t)

    for category, txns in by_category.items():
        amounts = [t["amount"] for t in txns]
        if len(amounts) < 4:
            continue
        mean = statistics.mean(amounts)
        stdev = statistics.pstdev(amounts)
        if stdev == 0:
            continue
        for t in txns:
            z = (t["amount"] - mean) / stdev
            if z > Z_THRESHOLD:
                flags[t["id"]] = (
                    f"₹{t['amount']:.0f} is unusually high for '{category}' "
                    f"(category average ≈ ₹{mean:.0f})"
                )

    # --- 2. Duplicate charges ---
    sorted_txns = sorted(transactions, key=lambda t: t["date"])
    seen = defaultdict(list)  # (amount, desc_key) -> list of past datetimes
    for t in sorted_txns:
        if t["direction"] != "debit" or t.get("category") == "Cash & ATM":
            continue  # repeated identical ATM withdrawals are normal, not duplicates
        key = (round(t["amount"], 2), _desc_key(t["description"]))
        for prev_date in seen[key]:
            if abs((t["date"] - prev_date)) <= timedelta(hours=DUPLICATE_WINDOW_HOURS):
                flags[t["id"]] = (
                    f"Possible duplicate charge: another ₹{t['amount']:.0f} payment to a "
                    f"similar merchant within {DUPLICATE_WINDOW_HOURS}h"
                )
                break
        seen[key].append(t["date"])

    # --- 3. New merchant, large spend ---
    first_seen = {}
    for t in sorted_txns:
        if t["direction"] != "debit":
            continue
        key = _desc_key(t["description"])
        if key not in first_seen:
            first_seen[key] = t["id"]
            if t["amount"] >= NEW_MERCHANT_ABS_THRESHOLD:
                flags.setdefault(
                    t["id"],
                    f"First-ever transaction with this merchant, and it's a large amount "
                    f"(₹{t['amount']:.0f})",
                )

    # --- 4. Odd-hour transactions (only if time component present) ---
    for t in transactions:
        d = t["date"]
        if getattr(d, "hour", 0) or getattr(d, "minute", 0):
            if t["direction"] == "debit" and 0 <= d.hour < 5:
                flags.setdefault(t["id"], f"Transaction posted at an unusual hour ({d.strftime('%H:%M')})")

    return [{"transaction_id": tid, "reason": reason} for tid, reason in flags.items()]
