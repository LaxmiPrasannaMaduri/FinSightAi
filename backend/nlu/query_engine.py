"""
query_engine.py
----------------
Takes a question plus the user's transaction data, parses it (query_parser.py),
computes the answer and phrases it in plain English.

When nothing matches, it explains why (wrong period? no spending in that
category?) and tells the user what the loaded statement actually contains,
instead of a bare "couldn't find anything".
"""

from collections import defaultdict
from datetime import timedelta

from .query_parser import parse_query
from anomaly.detector import detect_anomalies
from formatting import inr_auto


def _money(x: float) -> str:
    return inr_auto(x)


def _in_range(t, q):
    if q.date_from and t["date"] < q.date_from:
        return False
    if q.date_to and t["date"] >= q.date_to:
        return False
    return True


def _filter(transactions, q):
    rows = [t for t in transactions if _in_range(t, q)]
    if q.direction != "both":
        rows = [t for t in rows if t["direction"] == q.direction]
    if q.categories:
        cat_set = set(q.categories)
        rows = [t for t in rows if t["category"] in cat_set]
    return rows


def _period_phrase(q):
    if q.date_from and q.date_to:
        span_days = (q.date_to - q.date_from).days
        if span_days <= 31 and q.date_from.day == 1:
            return f"in {q.date_from.strftime('%B %Y')}"
        last_day = q.date_to - timedelta(days=1)  # date_to is exclusive
        return f"between {q.date_from.strftime('%d %b %Y')} and {last_day.strftime('%d %b %Y')}"
    return "across the whole statement"


def _category_phrase(q):
    return " / ".join(q.categories) if q.categories else "all categories"


def _coverage(transactions):
    lo = min(t["date"] for t in transactions)
    hi = max(t["date"] for t in transactions)
    return f"Your loaded statement covers {lo.strftime('%d %b %Y')} to {hi.strftime('%d %b %Y')}."


def _nothing_found(q, transactions):
    """Explain an empty result and point to where the data actually is."""
    period = _period_phrase(q)
    kind = "income" if q.direction == "credit" else "spending"
    what = f"{_category_phrase(q)} {kind}" if q.categories else kind
    parts = [f"I couldn't find any {what} {period}."]

    in_period = [t for t in transactions if _in_range(t, q) and
                 (q.direction == "both" or t["direction"] == q.direction)]
    if in_period and q.categories:
        totals = defaultdict(float)
        for t in in_period:
            totals[t["category"]] += t["amount"]
        top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:5]
        parts.append(f"In that period the {kind} was in: " +
                     ", ".join(f"{c} ({_money(v)})" for c, v in top) + ".")
        parts.append("If something is filed under the wrong category, fix it in the table below "
                     "and click Retrain model.")
    elif not in_period:
        parts.append(_coverage(transactions))
    return " ".join(parts)


def answer_query(text: str, transactions: list, known_categories: list, category_seed: dict):
    """
    transactions: list of dicts (id, date, description, amount, direction, category)
    Returns dict: {answer: str, data: {...}, parsed: {...}}
    """
    if not transactions:
        return {"answer": "No statement is loaded yet. Upload a statement first, then ask me about it.",
                "data": {}, "parsed": {}}

    available_months = {(t["date"].year, t["date"].month) for t in transactions}
    anchor = max(t["date"] for t in transactions)

    q = parse_query(text, known_categories, category_seed, now=anchor,
                    available_months=available_months)

    # "total income" / "how much did I receive" means ALL money coming in, not just
    # rows the model happened to file under the 'Income' category.
    if q.direction == "credit" and q.categories == ["Income"]:
        q.categories = []

    period = _period_phrase(q)
    cat_phrase = _category_phrase(q)
    subject = f"{cat_phrase} " if q.categories else ""      # "Food & Dining " or ""
    note = f"\n{q.month_guess_note}" if q.month_guess_note else ""

    def reply(answer, data):
        return {"answer": answer + note, "data": data, "parsed": vars(q)}

    # ---- anomalies -------------------------------------------------------
    if q.intent == "anomaly":
        flags = detect_anomalies(transactions)  # full history gives the best baselines
        flagged = {f["transaction_id"]: f["reason"] for f in flags}
        relevant = [t for t in transactions if _in_range(t, q) and t["id"] in flagged]
        if q.categories:
            relevant = [t for t in relevant if t["category"] in set(q.categories)]
        if not relevant:
            return reply(f"Nothing looked unusual {period}. No statistical outliers, duplicate "
                         f"charges, or first-time large payments were flagged.", {"flags": []})
        lines = [f"Found {len(relevant)} transaction(s) worth a second look {period}:"]
        for t in sorted(relevant, key=lambda x: x["date"]):
            lines.append(f"  • {t['date'].strftime('%d %b %Y')} — {t['description'][:60]} "
                         f"({_money(t['amount'])}) — {flagged[t['id']]}")
        return reply("\n".join(lines),
                     {"flags": [{"id": t["id"], "reason": flagged[t["id"]]} for t in relevant]})

    # ---- compare two categories -------------------------------------------
    if q.intent == "compare" and q.compare_categories:
        cat_a, cat_b = q.compare_categories

        def total(cat):
            return sum(t["amount"] for t in transactions
                       if t["category"] == cat and t["direction"] == q.direction and _in_range(t, q))

        total_a, total_b = total(cat_a), total(cat_b)
        diff = total_a - total_b
        word = "more" if diff > 0 else "less"
        period_cap = period[0].upper() + period[1:]
        return reply(f"{period_cap}, you spent {_money(total_a)} on {cat_a} vs {_money(total_b)} on "
                     f"{cat_b} — that's {_money(abs(diff))} {word} on {cat_a}.",
                     {cat_a: total_a, cat_b: total_b})

    rows = _filter(transactions, q)

    # ---- top categories / overview -----------------------------------------
    if q.intent == "top":
        if not rows:
            return reply(_nothing_found(q, transactions), {})
        totals = defaultdict(float)
        for t in rows:
            totals[t["category"]] += t["amount"]
        ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:6]
        label = "Top income sources" if q.direction == "credit" else "Top spending categories"
        lines = [f"{label} {period} (total {_money(sum(totals.values()))}):"]
        for cat, amt in ranked:
            lines.append(f"  • {cat}: {_money(amt)}")
        return reply("\n".join(lines), dict(ranked))

    # ---- everything else needs matching rows --------------------------------
    if not rows:
        return reply(_nothing_found(q, transactions), {"count": 0, "total": 0})

    amounts = [t["amount"] for t in rows]
    total_amt, count = sum(amounts), len(amounts)
    avg = total_amt / count
    biggest = max(rows, key=lambda t: t["amount"])
    txn_word = "transaction" if count == 1 else "transactions"

    if q.intent == "count":
        return reply(f"You made {count} {subject}{txn_word} {period}.", {"count": count})
    if q.intent == "average":
        return reply(f"Your average {subject}transaction {period} was {_money(avg)} "
                     f"(across {count} {txn_word}).", {"average": avg, "count": count})
    if q.intent == "max":
        return reply(f"Your biggest {subject}transaction {period} was {_money(biggest['amount'])} "
                     f"on {biggest['date'].strftime('%d %b %Y')} ({biggest['description'][:60]}).",
                     {"max": biggest["amount"], "description": biggest["description"]})

    verb = "received" if q.direction == "credit" else "spent"
    prep = "from" if q.direction == "credit" else "on"
    target = f"{prep} {cat_phrase}" if q.categories else "in total"
    ans = f"You {verb} {_money(total_amt)} {target} {period} ({count} {txn_word})."
    if count <= 5:
        ans += "\n" + "\n".join(
            f"  • {t['date'].strftime('%d %b %Y')} — {t['description'][:60]} ({_money(t['amount'])})"
            for t in sorted(rows, key=lambda x: x["date"]))
    return reply(ans, {"total": total_amt, "count": count})
