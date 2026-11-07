"""
normalize.py
------------
Bank statements (CSV exports or PDF tables) all describe the same thing
-- date, description, amount, whether it's money in or out -- but every
bank names its columns differently. This module maps whatever headers
we find onto a single internal schema:

    date | description | amount | direction ("debit"/"credit") | balance

so the rest of the app never has to care which bank the statement came
from.
"""

import re
import hashlib
from datetime import datetime

import pandas as pd
from dateutil import parser as dateparser

DATE_HEADERS = ["date", "txn date", "transaction date", "value date", "posting date"]
DESC_HEADERS = ["description", "narration", "particulars", "transaction details",
                "remarks", "details", "transaction remarks"]
DEBIT_HEADERS = ["debit", "withdrawal", "withdrawal amt", "dr", "debit amount", "amount(dr)"]
CREDIT_HEADERS = ["credit", "deposit", "deposit amt", "cr", "credit amount", "amount(cr)"]
AMOUNT_HEADERS = ["amount", "transaction amount", "txn amount"]
TYPE_HEADERS = ["type", "cr/dr", "dr/cr", "transaction type"]
BALANCE_HEADERS = ["balance", "closing balance", "available balance", "running balance"]


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", str(h).strip().lower())


def _find_column(columns_normalized, candidates):
    for cand in candidates:
        for col in columns_normalized:
            if col == cand:
                return col
    # fallback: substring match
    for cand in candidates:
        for col in columns_normalized:
            if cand in col:
                return col
    return None


def _parse_amount(val):
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        if isinstance(val, float) and val != val:  # NaN check (NaN != NaN)
            return 0.0
        return float(val)
    s = str(val).strip()
    if s == "" or s.lower() in ("nan", "none", "-"):
        return 0.0
    s = s.replace(",", "").replace("₹", "").replace("Rs.", "").replace("INR", "")
    s = s.strip()
    negative = False
    # "1,000.00 Cr" / "250.00 Dr" -- many banks print a Cr/Dr marker after balances
    marker = re.search(r"\s*(cr|dr)\.?$", s, flags=re.IGNORECASE)
    if marker:
        negative = marker.group(1).lower() == "dr"
        s = s[:marker.start()].strip()
    if s.startswith("(") and s.endswith(")"):
        negative = not negative
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        v = 0.0
    return -v if negative else v


def _parse_date(val):
    if isinstance(val, datetime):
        return val
    try:
        return dateparser.parse(str(val), dayfirst=True, fuzzy=True)
    except Exception:
        return None


def fingerprint(date, description, amount):
    key = f"{date.date() if hasattr(date,'date') else date}|{description.strip().lower()}|{round(amount,2)}"
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def normalize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    Takes a raw dataframe (any bank's column layout) and returns a
    clean dataframe with columns: date, description, amount, direction, balance
    """
    original_cols = list(df.columns)
    normalized_cols = [_norm_header(c) for c in original_cols]
    col_map = dict(zip(normalized_cols, original_cols))

    date_col = _find_column(normalized_cols, DATE_HEADERS)
    desc_col = _find_column(normalized_cols, DESC_HEADERS)
    debit_col = _find_column(normalized_cols, DEBIT_HEADERS)
    credit_col = _find_column(normalized_cols, CREDIT_HEADERS)
    amount_col = _find_column(normalized_cols, AMOUNT_HEADERS)
    type_col = _find_column(normalized_cols, TYPE_HEADERS)
    balance_col = _find_column(normalized_cols, BALANCE_HEADERS)

    if not date_col or not desc_col:
        raise ValueError(
            f"Could not identify date/description columns. Found headers: {original_cols}"
        )

    rows = []
    for _, row in df.iterrows():
        raw_date = row[col_map[date_col]]
        date = _parse_date(raw_date)
        if date is None:
            continue

        description = re.sub(r"\s+", " ", str(row[col_map[desc_col]])).strip()  # unwrap multi-line cells
        if description == "" or description.lower() in ("nan", "none"):
            continue

        amount = 0.0
        direction = None

        if debit_col or credit_col:
            debit_val = _parse_amount(row[col_map[debit_col]]) if debit_col else 0.0
            credit_val = _parse_amount(row[col_map[credit_col]]) if credit_col else 0.0
            if debit_val and debit_val != 0:
                amount = abs(debit_val)
                direction = "debit"
            elif credit_val and credit_val != 0:
                amount = abs(credit_val)
                direction = "credit"
            else:
                continue
        elif amount_col:
            raw_amount = _parse_amount(row[col_map[amount_col]])
            amount = abs(raw_amount)
            if type_col:
                type_val = str(row[col_map[type_col]]).strip().lower()
                direction = "credit" if type_val.startswith("cr") else "debit"
            else:
                direction = "credit" if raw_amount > 0 else "debit"
        else:
            continue

        balance = _parse_amount(row[col_map[balance_col]]) if balance_col else None

        rows.append({
            "date": date,
            "description": description,
            "amount": round(amount, 2),
            "direction": direction,
            "balance": balance,
        })

    return pd.DataFrame(rows)
