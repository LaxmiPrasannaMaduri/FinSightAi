"""
csv_parser.py
-------------
Parses CSV (or Excel) bank/UPI statement exports into the normalized
transaction schema. Handles the common mess in real-world exports:
banner/disclaimer rows before the real header, trailing summary rows,
inconsistent encodings.
"""

import io
import csv
import pandas as pd

from .normalize import normalize_dataframe

CANDIDATE_HEADER_KEYWORDS = ["date", "narration", "description", "particulars", "debit", "credit", "amount"]


def _find_header_row(text: str) -> int:
    """
    Many bank CSV exports have a few disclaimer/title rows before the
    actual header row (and those rows often have a different number of
    columns than the real table, which trips up pandas if we let it
    guess). Scan the raw lines with the stdlib csv reader -- which
    tolerates ragged rows -- for the first one that looks like a real
    header (contains several expected keywords).
    """
    reader = csv.reader(io.StringIO(text))
    best_row, best_score = 0, -1
    for i, row in enumerate(reader):
        if i >= 30:
            break
        row_vals = [str(v).strip().lower() for v in row]
        score = sum(1 for kw in CANDIDATE_HEADER_KEYWORDS if any(kw in v for v in row_vals))
        if score > best_score:
            best_score = score
            best_row = i
    return best_row


def parse_csv(file_bytes: bytes) -> pd.DataFrame:
    """Returns a normalized dataframe: date, description, amount, direction, balance"""
    text = file_bytes.decode("utf-8", errors="ignore")
    header_row = _find_header_row(text)

    df = pd.read_csv(io.StringIO(text), header=header_row, dtype=str, engine="python",
                      on_bad_lines="skip", skip_blank_lines=True)
    df = df.dropna(how="all")
    df = df.loc[:, ~df.columns.astype(str).str.contains("^Unnamed")]
    return normalize_dataframe(df)


def _find_header_row_in_df(raw: pd.DataFrame) -> int:
    best_row, best_score = 0, -1
    for i in range(min(30, len(raw))):
        row_vals = [str(v).strip().lower() for v in raw.iloc[i].tolist()]
        score = sum(1 for kw in CANDIDATE_HEADER_KEYWORDS if any(kw in v for v in row_vals))
        if score > best_score:
            best_score = score
            best_row = i
    return best_row


def parse_excel(file_bytes: bytes) -> pd.DataFrame:
    raw = pd.read_excel(io.BytesIO(file_bytes), header=None, dtype=str)
    header_row = _find_header_row_in_df(raw)
    df = pd.read_excel(io.BytesIO(file_bytes), header=header_row, dtype=str)
    df = df.dropna(how="all")
    return normalize_dataframe(df)
