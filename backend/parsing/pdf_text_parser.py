"""
pdf_text_parser.py
-------------------
Fallback PDF transaction extractor that does NOT depend on pdfplumber's
grid/table detection. Most real-world bank statement PDFs (this was
confirmed against an actual Standard Chartered statement) only have
strong VERTICAL column-separator lines and sparse/no horizontal lines
between individual transactions. pdfplumber's default table strategy
then merges dozens of transactions into a single mangled cell.

This parser instead works directly off the plain reading-order text
(`page.extract_text()`) using a small, general state machine:

  - A new transaction/ledger-entry starts on a line that either:
      (a) begins with one or two date-like tokens, or
      (b) contains descriptive text followed by trailing monetary
          amount(s) (banks often print fee/tax lines -- e.g. "CGST
          @ 9%" -- with no date at all, immediately followed by their
          own amount).
  - A line that is *purely* numeric tokens completes whichever
    transaction is currently open and still missing its amount --
    this handles statements where the amount is printed a couple of
    lines below the description instead of on the same line.
  - Any other line (no date, no amount) is just additional wrapped
    description text and gets appended to the currently open entry.

  - Direction (debit/credit) is inferred from the running balance
    delta between consecutive entries, rather than from column
    position -- this is what makes the parser bank-agnostic: it works
    whether the statement calls the columns "Debit/Credit",
    "Withdrawal/Deposit", "Dr/Cr", or doesn't separate them into
    columns at all.

This is intentionally a generic, best-effort reconstruction -- it will
not be pixel-perfect for every bank on earth, but it degrades much
more gracefully than requiring a clean ruled grid.
"""

import re
from datetime import datetime
from dateutil import parser as dateparser

import pandas as pd

MONEY_RE = re.compile(r"\d{1,3}(?:,\d{2,3})*\.\d{2}(?!\s*%)")

STOP_SECTION_MARKERS = ("reward points statement", "reward points summary")

DATE_TOKEN = (
    r"(?:\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}"      # 16/06/2019, 16-06-19
    r"|\d{4}[/\-.]\d{1,2}[/\-.]\d{1,2}"           # 2019-06-16
    r"|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})"        # 16 Jun 19 / 16 June 2019
)
LEADING_DATES_RE = re.compile(
    rf"^\s*(?P<d1>{DATE_TOKEN})(?:\s+(?P<d2>{DATE_TOKEN}))?\s*(?P<rest>.*)$"
)

# Lines that are page furniture / summaries, never real transactions.
NOISE_PREFIXES = (
    "page ", "total ", "reward points", "scheme opening", "bank deposits",
    "please register", "report irregularities", "branch address", "micr:",
    "account statement", "dda ", "branch :", "statement date", "currency",
    "account type", "account no", "nominee registered", "opening balance",
    "closing balance", "carried forward", "brought forward",
)
NOISE_CONTAINS = ("cheque", "withdrawal", "deposit", "balance") \
    # only used to detect a repeated header row (must contain >=2 of these + "date")


def _is_noise_or_header(line: str) -> bool:
    low = line.strip().lower()
    if not low:
        return True
    if low.startswith(NOISE_PREFIXES):
        return True
    keyword_hits = sum(1 for kw in ("date", "description", "cheque", "withdrawal", "deposit", "balance")
                        if kw in low)
    if keyword_hits >= 3:
        return True  # repeated header row on a later page
    return False


def _parse_date(token: str):
    try:
        return dateparser.parse(token, dayfirst=True, fuzzy=True)
    except Exception:
        return None


def _parse_money(token: str) -> float:
    return float(token.replace(",", ""))


class _OpenTxn:
    __slots__ = ("date", "value_date", "desc_lines", "amounts")

    def __init__(self, date, value_date):
        self.date = date
        self.value_date = value_date
        self.desc_lines = []
        self.amounts = []

    def description(self) -> str:
        return " ".join(part for part in self.desc_lines if part).strip()


def extract_transactions_from_text(full_text: str):
    """
    Returns a list of dicts: date, description, amount, direction, balance
    """
    lines = [ln for ln in full_text.split("\n")]

    records = []
    current = None
    previous_balance = None

    def flush(txn):
        nonlocal previous_balance
        if txn is None:
            return
        if not txn.amounts:
            return  # nothing usable, drop silently
        amounts = [_parse_money(a) for a in txn.amounts]
        if len(amounts) == 1:
            # a lone balance figure (e.g. "BALANCE FORWARD") -- establishes
            # the running balance baseline but isn't itself a transaction.
            previous_balance = amounts[0]
            return
        balance = amounts[-1]
        if previous_balance is None:
            # first real movement in the document with no prior baseline;
            # best-effort: assume the second-to-last figure is the amount
            # and guess debit (most statements list spends more often).
            amount = amounts[-2]
            direction = "debit"
        else:
            amount = abs(round(balance - previous_balance, 2))
            direction = "credit" if balance > previous_balance else "debit"
            if amount == 0:
                # balance didn't move (rounding/reporting quirk) -- fall back
                # to the printed amount token and assume debit.
                amount = amounts[-2] if len(amounts) >= 2 else 0.0
        desc = txn.description() or "(no description)"
        records.append({
            "date": txn.date,
            "description": desc,
            "amount": round(amount, 2),
            "direction": direction,
            "balance": balance,
        })
        previous_balance = balance

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if any(marker in line.lower() for marker in STOP_SECTION_MARKERS):
            break  # everything after this is non-transactional footer content
        if _is_noise_or_header(line):
            continue

        m = LEADING_DATES_RE.match(line)
        has_date = bool(m and m.group("d1"))

        if has_date:
            rest = m.group("rest") or ""
            d1 = _parse_date(m.group("d1"))
            d2 = _parse_date(m.group("d2")) if m.group("d2") else d1
            if d1 is None:
                # failed to parse despite matching the pattern -- treat as
                # plain continuation text instead of crashing the parse.
                if current is not None:
                    current.desc_lines.append(line)
                continue

            flush(current)
            current = _OpenTxn(d1, d2)

            tokens = MONEY_RE.findall(rest)
            desc_part = MONEY_RE.sub("", rest).strip(" \t:/-")
            if desc_part:
                current.desc_lines.append(desc_part)
            if tokens:
                current.amounts = tokens
            continue

        # no leading date on this line
        tokens = MONEY_RE.findall(line)
        if not tokens:
            if current is not None:
                current.desc_lines.append(line)
            continue

        desc_part = MONEY_RE.sub("", line).strip(" \t:/-")
        if not desc_part:
            # purely numeric line -- completes whichever entry is still
            # waiting on its amount (common when amount prints a line or
            # two below the description instead of alongside it).
            if current is not None and not current.amounts:
                current.amounts = tokens
            else:
                flush(current)
                current = _OpenTxn(current.date if current else None,
                                    current.value_date if current else None)
                current.amounts = tokens
        else:
            # descriptive text with its own trailing amount -> a distinct
            # ledger entry (e.g. a fee/tax line with no date of its own).
            flush(current)
            current = _OpenTxn(current.date if current else None,
                                current.value_date if current else None)
            current.desc_lines.append(desc_part)
            current.amounts = tokens

    flush(current)
    return records


def parse_pdf_by_text(file_bytes: bytes, password: str = None) -> pd.DataFrame:
    import io
    import pdfplumber

    all_records = []
    with pdfplumber.open(io.BytesIO(file_bytes), password=password) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            all_records.extend(extract_transactions_from_text(text))

    if not all_records:
        raise ValueError(
            "Could not reconstruct any transactions from this PDF's text layer. "
            "It may be a scanned/image-only statement, which needs OCR first."
        )

    df = pd.DataFrame(all_records)
    df = df.dropna(subset=["date"])
    return df
