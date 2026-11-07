"""
pdf_parser.py
-------------
Parses PDF bank statements into the normalized transaction schema.

Approach:
1. Use pdfplumber to pull tables from every page (most Indian bank /
   UPI-app statements render transactions as a table).
2. Stitch tables across pages together, using the first page's header
   row as the canonical header (later pages sometimes repeat it,
   sometimes don't).
3. Hand off to the same normalize_dataframe() used for CSVs, so both
   input types converge on one schema.

Password-protected PDFs: pass `password=` through if the user's bank
statement is locked (common for e-statements).
"""

"""
pdf_parser.py
-------------
Parses PDF bank statements into the normalized transaction schema.

Two strategies, tried in order:

1. **Grid/table extraction** (pdfplumber's table detector). Works well
   when the PDF has full ruling lines between every transaction row
   (a real grid). We scan every row of every extracted table for a
   header (not just the first row of the first table -- statements
   often put an address block before the real header), and we detect
   "corrupted" tables where dozens of transactions got merged into one
   cell (this happens when there are only vertical column-separator
   lines and no horizontal ones between individual transactions).

2. **Text-position reconstruction** (`pdf_text_parser.py`), used
   automatically whenever (1) fails or produces a corrupted/garbled
   result. This works off the plain reading-order text and a small
   state machine, so it handles the very common case where a
   statement has no real row grid at all.

Password-protected PDFs: pass `password=` through if the user's bank
statement is locked (common for e-statements).
"""

import io
import re
import pandas as pd
import pdfplumber

from .normalize import normalize_dataframe
from .pdf_text_parser import parse_pdf_by_text

DATE_WORDS = ("date",)
DESC_WORDS = ("description", "particulars", "narration", "details", "remarks", "transaction")
AMOUNT_WORDS = ("debit", "credit", "withdrawal", "deposit", "amount", "dr", "cr")

MAX_NEWLINES_PER_CELL = 3  # more than this in a data cell => merged/corrupted row


def _cells(row):
    return [re.sub(r"\s+", " ", str(c)).strip().lower() for c in row if c is not None and str(c).strip()]


def _looks_like_header(row) -> bool:
    """
    A transaction-table header has a date column AND something that describes the
    entry (description/particulars/narration) or its amounts (debit/credit/...).
    This deliberately rejects unrelated tables that merely contain the word
    "date", e.g. "Account Open Date | Maturity Date | Balance".
    """
    cells = _cells(row)
    if len(cells) < 3:
        return False
    has_date = any(w in c for c in cells for w in DATE_WORDS)
    has_desc = any(w in c for c in cells for w in DESC_WORDS)
    has_amount = any(c == w or w in c for c in cells for w in AMOUNT_WORDS if len(w) > 2) or \
        any(c in ("dr", "cr") for c in cells)
    return has_date and (has_desc or has_amount)


def _row_is_corrupted(row) -> bool:
    """A row where some cell swallowed many transactions' worth of lines."""
    for cell in row:
        if cell and str(cell).count("\n") > MAX_NEWLINES_PER_CELL:
            return True
    return False


def _extract_tables_grid(file_bytes: bytes, password: str = None) -> pd.DataFrame:
    header = None
    header_key = None
    all_rows = []

    with pdfplumber.open(io.BytesIO(file_bytes), password=password) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue

                # Does this table carry its own header row?
                hdr_idx = next((i for i, r in enumerate(table) if _looks_like_header(r)), None)

                if hdr_idx is not None:
                    key = tuple(_cells(table[hdr_idx]))
                    if header is None:
                        header, header_key = table[hdr_idx], key
                    elif key != header_key:
                        continue  # a different table (e.g. "Linked Deposits") -> ignore it
                    data_rows = table[hdr_idx + 1:]
                else:
                    # no header: a continuation of the main table on a later page
                    if header is None or len(table[0]) != len(header):
                        continue
                    data_rows = table

                for row in data_rows:
                    if not row or not any(cell and str(cell).strip() for cell in row):
                        continue
                    if tuple(_cells(row)) == header_key:
                        continue  # repeated header
                    if _row_is_corrupted(row):
                        raise ValueError("corrupted-grid")  # triggers the text-based fallback
                    all_rows.append(row)

    if header is None or not all_rows:
        raise ValueError("no-grid-table-found")

    width = len(header)
    cleaned_rows = []
    for row in all_rows:
        row = list(row) + [None] * (width - len(row)) if len(row) < width else row[:width]
        cleaned_rows.append(row)

    df = pd.DataFrame(cleaned_rows, columns=[re.sub(r"\s+", " ", str(h or "")).strip() for h in header])
    df = df.dropna(how="all")
    return normalize_dataframe(df)


def parse_pdf(file_bytes: bytes, password: str | None = None) -> pd.DataFrame:
    try:
        df = _extract_tables_grid(file_bytes, password=password)
        if not df.empty:
            return df
    except Exception:
        pass  # fall through to text-based reconstruction

    # Fallback: works for statements with no full row grid (very common).
    df = parse_pdf_by_text(file_bytes, password=password)
    if df.empty:
        raise ValueError(
            "Could not find any transactions in this PDF using either table "
            "detection or text reconstruction. It may be a scanned/image-only "
            "statement (needs OCR) or use an unusual layout."
        )
    return df
