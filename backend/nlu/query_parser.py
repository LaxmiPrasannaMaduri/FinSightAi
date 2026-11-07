"""
query_parser.py
----------------
Our own lightweight NLU for questions like:
    "how much did I spend on food in March"
    "how many times did I order swiggy last month"
    "flag anything unusual in June"
    "what's my average grocery spend this year"
    "compare my spending on shopping vs food in 2026"

No external LLM API is used. This is a deterministic pipeline:

    1. date range extraction   (month names, "last month", "this month",
                                 explicit years, "between X and Y")
    2. category extraction     (category names, synonyms like "dining" or
                                 "petrol", seed keywords like "swiggy",
                                 plus fuzzy matching for typos)
    3. intent / aggregation    (sum, count, average, max, anomaly,
                                 compare, top-N)

Two things make it behave sensibly on a statement-analysis tool:

  * Dates are resolved against the LOADED DATA, not the wall clock.
    If you ask about "June" and your statement is from June 2019, we
    look at June 2019 (the year the data actually has) instead of
    assuming the current year. "this month" / "last month" are relative
    to the most recent transaction in the statement.

  * Keyword checks are whole-word matches, so "laptop" is no longer
    mistaken for "top".
"""

import re
import calendar
import difflib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta

MONTH_NAMES = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTH_ABBR = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
MONTH_LOOKUP = {**MONTH_NAMES, **MONTH_ABBR, "sept": 9}

AGG_SUM_WORDS = ["how much", "total", "spend", "spent", "sum", "paid", "pay"]
AGG_COUNT_WORDS = ["how many times", "how many transactions", "how many", "number of times",
                   "number of transactions", "count"]
AGG_AVG_WORDS = ["average", "avg", "typical", "mean"]
AGG_MAX_WORDS = ["biggest", "largest", "highest", "most expensive", "maximum"]
ANOMALY_WORDS = ["unusual", "suspicious", "flag", "flagged", "anomaly", "anomalies", "weird",
                 "odd", "fraud", "fraudulent", "strange"]
COMPARE_WORDS = ["vs", "versus", "compare", "compared to", "compared with"]
INCOME_WORDS = ["income", "salary", "credited", "earned", "received", "deposited", "credit", "credits"]
TOP_WORDS = ["top", "biggest categories", "where did most", "breakdown", "summary", "summarize",
             "summarise", "overview"]

# Extra everyday words people use for each category. (Seed keywords such as
# merchant names are matched separately.)
CATEGORY_SYNONYMS = {
    "Food & Dining": ["food", "dining", "restaurant", "restaurants", "eating", "meal", "meals",
                      "lunch", "dinner", "breakfast", "snacks", "coffee", "takeaway", "hotel"],
    "Groceries": ["groceries", "grocery", "supermarket", "vegetables"],
    "Transport": ["transport", "commute", "cab", "cabs", "taxi", "ride", "rides", "metro", "bus"],
    "Fuel": ["fuel", "petrol", "diesel", "gas"],
    "Shopping": ["shopping", "clothes", "clothing", "purchases", "purchase", "online shopping"],
    "Bills & Utilities": ["bills", "bill", "utilities", "utility", "electricity", "recharge", "recharges",
                          "broadband", "internet"],
    "Rent": ["rent"],
    "Entertainment": ["entertainment", "movies", "movie", "streaming"],
    "Healthcare": ["health", "healthcare", "medical", "medicine", "medicines", "pharmacy", "doctor"],
    "Investments": ["investment", "investments", "invest", "sip", "stocks"],
    "Income": ["salary", "income", "earnings"],
    "Transfers (P2P/UPI)": ["transfer", "transfers", "upi", "imps", "neft", "sent", "p2p"],
    "Travel": ["travel", "trip", "trips", "flight", "flights", "holiday", "vacation"],
    "Subscriptions": ["subscription", "subscriptions"],
    "Cash & ATM": ["atm", "cash", "withdrawal", "withdrawals"],
    "Others": ["others", "miscellaneous", "misc", "charges", "fees", "fee"],
}


@dataclass
class ParsedQuery:
    intent: str                       # "sum" | "count" | "average" | "max" | "anomaly" | "compare" | "top" | "list"
    categories: list = field(default_factory=list)
    date_from: datetime = None
    date_to: datetime = None
    direction: str = "debit"          # "debit" | "credit" | "both"
    compare_categories: tuple = None  # (cat_a, cat_b) for "compare" intent
    raw_text: str = ""
    month_guess_note: str = ""        # set when we had to infer a year for the user


def _has(text_l: str, words) -> bool:
    """True if any word/phrase appears as a WHOLE word/phrase (not inside another word)."""
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text_l) for w in words)


def _month_year_range(month: int, year: int):
    start = datetime(year, month, 1)
    return start, start + relativedelta(months=1)


def _extract_date_range(text: str, anchor: datetime, available_months: set):
    """
    anchor: the "today" to resolve relative phrases against (latest transaction
            in the data, or the real current date when there is no data).
    available_months: set of (year, month) tuples that actually have data.
    Returns (date_from, date_to, note)
    """
    text_l = text.lower()

    if _has(text_l, ["last month", "previous month"]):
        first_of_this = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return first_of_this - relativedelta(months=1), first_of_this, ""

    if _has(text_l, ["this month", "current month"]):
        start = anchor.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, start + relativedelta(months=1), ""

    if _has(text_l, ["this year"]):
        start = anchor.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return start, start + relativedelta(years=1), ""

    if _has(text_l, ["last year", "previous year"]):
        start = anchor.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0) - relativedelta(years=1)
        return start, start + relativedelta(years=1), ""

    m = re.search(r"(?:last|past)\s+(\d+)\s+days?", text_l)
    if m:
        days = int(m.group(1))
        end = anchor + timedelta(days=1)
        return end - timedelta(days=days), end, ""

    if _has(text_l, ["last week", "past week"]):
        end = anchor + timedelta(days=1)
        return end - timedelta(days=7), end, ""

    if _has(text_l, ["yesterday"]):
        d = (anchor - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return d, d + timedelta(days=1), ""

    # explicit dd/mm/yyyy style dates (checked early, before bare months/years)
    date_like = re.findall(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", text)
    if len(date_like) >= 2:
        try:
            from dateutil import parser as dp
            d1 = dp.parse(date_like[0], dayfirst=True)
            d2 = dp.parse(date_like[1], dayfirst=True)
            lo, hi = min(d1, d2), max(d1, d2) + timedelta(days=1)
            return lo, hi, ""
        except Exception:
            pass

    year_match = re.search(r"\b(20\d{2})\b", text_l)
    explicit_year = int(year_match.group(1)) if year_match else None

    # month name, e.g. "june" / "jun" / "june 2019"
    month_num = None
    for name, num in MONTH_LOOKUP.items():
        if name == "may":
            # "may" is also a verb ("may I ask...") -- only treat it as a month
            # when it's clearly used as one.
            if re.search(r"\b(in|of|for|during|since|until|by|on)\s+may\b", text_l) or \
               re.search(r"\bmay\s+(20\d{2})\b", text_l) or re.fullmatch(r".*\bmay\b\s*[?.!]*", text_l):
                month_num = num
                break
            continue
        if re.search(rf"\b{name}\b", text_l):
            month_num = num
            break

    if month_num is not None:
        if explicit_year:
            return (*_month_year_range(month_num, explicit_year), "")

        # No year given -> use whichever year in the DATA has that month.
        years_with_data = sorted(y for (y, mo) in available_months if mo == month_num)
        if years_with_data:
            year = years_with_data[-1]  # most recent year that has this month
            note = ""
            if len(years_with_data) > 1:
                note = (f"(Your data has {calendar.month_name[month_num]} in several years; "
                        f"I used the latest, {year}. Add a year to pick another.)")
            return (*_month_year_range(month_num, year), note)

        # month isn't in the data at all: fall back to the anchor's year logic
        year = anchor.year if month_num <= anchor.month else anchor.year - 1
        return (*_month_year_range(month_num, year), "")

    if explicit_year:
        return datetime(explicit_year, 1, 1), datetime(explicit_year + 1, 1, 1), ""

    return None, None, ""  # no date filter -> all available history


def _category_significant_words(category: str):
    words = re.findall(r"[a-z]+", category.lower())
    return [w for w in words if len(w) > 2 and w not in {"and", "the", "p2p", "upi"}]


def _extract_categories(text: str, known_categories: list, category_seed: dict):
    text_l = text.lower()
    text_words = set(re.findall(r"[a-z]+", text_l))
    found = []

    for category in known_categories:
        matched = False

        if category.lower() in text_l:
            matched = True

        if not matched:
            for syn in CATEGORY_SYNONYMS.get(category, []):
                if _has(text_l, [syn]):
                    matched = True
                    break

        if not matched:
            for word in _category_significant_words(category):
                if word in text_words:
                    matched = True
                    break
                if len(word) >= 5 and difflib.get_close_matches(word, text_words, n=1, cutoff=0.86):
                    matched = True  # typo tolerance ("grocries", "shoping")
                    break

        if not matched:
            for kw in category_seed.get(category, {}).get("keywords", []):
                if len(kw) >= 3 and _has(text_l, [kw.strip()]):
                    matched = True
                    break

        if matched:
            found.append(category)

    return list(dict.fromkeys(found))


def parse_query(text: str, known_categories: list, category_seed: dict,
                now: datetime = None, available_months: set = None) -> ParsedQuery:
    """
    now:              "today" to resolve relative dates against (pass the
                      latest transaction date so 'last month' means last
                      month *of the statement*).
    available_months: set of (year, month) that exist in the data, used to
                      infer the year when the question only says "June".
    """
    now = now or datetime.now()
    available_months = available_months or set()
    text_l = text.lower()

    date_from, date_to, note = _extract_date_range(text, now, available_months)
    categories = _extract_categories(text, known_categories, category_seed)

    direction = "debit"
    if _has(text_l, INCOME_WORDS):
        direction = "credit"
    if _has(text_l, ["spend", "spent", "expense", "expenses", "paid", "debit", "debited"]):
        direction = "debit"

    if _has(text_l, COMPARE_WORDS) and len(categories) >= 2:
        return ParsedQuery(
            intent="compare", categories=categories, date_from=date_from, date_to=date_to,
            direction=direction, compare_categories=(categories[0], categories[1]),
            raw_text=text, month_guess_note=note,
        )

    if _has(text_l, ANOMALY_WORDS):
        intent = "anomaly"
    elif _has(text_l, TOP_WORDS) and not categories:
        intent = "top"
    elif _has(text_l, AGG_MAX_WORDS):
        intent = "max"
    elif _has(text_l, AGG_COUNT_WORDS):
        intent = "count"
    elif _has(text_l, AGG_AVG_WORDS):
        intent = "average"
    elif _has(text_l, AGG_SUM_WORDS):
        intent = "sum"
    else:
        intent = "list"

    return ParsedQuery(intent=intent, categories=categories, date_from=date_from, date_to=date_to,
                       direction=direction, raw_text=text, month_guess_note=note)
