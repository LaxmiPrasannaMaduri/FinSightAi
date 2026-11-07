# Bank Statement / UPI Expense Analyst

Upload a bank/UPI statement (PDF or CSV), get every transaction automatically
categorized by **your own locally-trained ML model** (no OpenAI/Claude/Gemini
API calls anywhere), and ask plain-English questions like:

- "How much did I spend on food in March?"
- "How many times did I order Swiggy?"
- "Flag anything unusual."
- "Compare my shopping vs food spend this year."

Everything runs on your machine. Your statements never leave your computer.

It also draws two pie charts (credits and debits, split by category) with a
dropdown to pick the month, and includes a **savings plan + expense tracker**:
say how much you want to save and it splits what's left across your usual
expense categories, then you log spending against those budgets.

---

## Why "own AI" instead of an LLM API

You asked specifically for this to use its own model rather than calling an
LLM API. Concretely, that means two custom, from-scratch components:

1. **Categorizer** (`backend/ml/`): a TF-IDF (character n-gram) vectorizer +
   `SGDClassifier` (scikit-learn), trained on a synthetic dataset generated
   from `backend/data/category_seed.json`. It's a real supervised ML model
   that generalizes to merchant strings it's never seen -- not a lookup
   table. When it's unsure (low confidence), it falls back to keyword
   matching. Every time you correct a category in the UI, that correction is
   stored and folded into the next retrain (`↻ Retrain model` button), so it
   keeps improving on *your* real statements over time.

2. **NLU query engine** (`backend/nlu/`): a rule + fuzzy-matching pipeline
   (regex, `dateutil`, `difflib`) that parses a natural-language question
   into a structured query (date range, category, aggregation type), then
   computes the answer directly from your transaction database. No network
   call, fully deterministic, and you can read exactly why it interpreted a
   question the way it did.

Anomaly detection ("flag anything unusual") is plain statistics (per-category
z-scores, duplicate-charge detection, new-merchant-large-spend detection) --
see `backend/anomaly/detector.py`.

**Honest caveat:** a from-scratch model trained on synthetic data will not be
as sharp on day one as a general-purpose LLM would be on unusual or highly
personal descriptions. Accuracy improves quickly as you correct a few
transactions and hit "Retrain model" -- the model is designed to learn from
you specifically.

---

## Project structure

```
bank-expense-analyst/
├── backend/
│   ├── app.py                 # FastAPI app (all endpoints)
│   ├── analytics.py           # month list + credit/debit totals for the pie charts
│   ├── planner.py             # savings plan: budgets per category + expense-tracker maths
│   ├── formatting.py          # Indian-style rupee formatting (₹1,16,149)
│   ├── requirements.txt
│   ├── data/
│   │   └── category_seed.json # keyword/template seed used to bootstrap training
│   ├── ml/
│   │   ├── train_model.py     # trains the categorizer (run once, or after corrections)
│   │   ├── categorizer.py     # loads + runs the trained model at request time
│   │   ├── model.joblib        (generated)
│   │   ├── vectorizer.joblib   (generated)
│   │   └── labels.json         (generated)
│   ├── parsing/
│   │   ├── csv_parser.py      # CSV/XLSX statement parsing
│   │   ├── pdf_parser.py      # PDF statement parsing (pdfplumber)
│   │   └── normalize.py       # maps any bank's column names to one schema
│   ├── nlu/
│   │   ├── query_parser.py    # question -> structured query
│   │   └── query_engine.py    # structured query -> answer
│   ├── anomaly/
│   │   └── detector.py        # z-score outliers, duplicates, new-merchant flags
│   └── db/
│       ├── database.py        # SQLite engine/session (storage/expenses.db)
│       └── models.py          # Transaction / UploadBatch / Correction tables
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
└── storage/                    # SQLite DB lives here (created automatically)
```

---

## Setup

Requires Python 3.10+.

```bash
cd bank-expense-analyst/backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 1. The model trains itself

There is nothing to run by hand: the first time the server starts it trains
the categorizer (takes a few seconds) and saves it to `backend/ml/`. It
retrains automatically whenever `data/category_seed.json` changes or a saved
model can't be loaded (e.g. after upgrading scikit-learn). You can still run
`python ml/train_model.py` manually if you like.

### 2. Run the app

```bash
uvicorn app:app --reload --port 8000
```

Open **http://localhost:8000** in your browser. The FastAPI backend also
serves the frontend directly, so there's nothing else to start.

### 3. Use it

1. Upload a CSV or PDF bank/UPI statement.
2. Check the auto-generated category for each transaction; use the "Fix…"
   dropdown on any row that's wrong.
3. Click **↻ Retrain model** occasionally after making a few corrections --
   this re-runs `train_model.py` in the background and hot-reloads the new
   model, so accuracy compounds the more you use it.
4. Use the **Month** dropdown above the pie charts to see credits and debits
   for a single month (or "All months").
5. **Savings plan & expense tracker**: your remaining balance is read from the
   statement (you can edit it). Enter how much you want to save; the app works
   out `balance - savings` = what you can spend, and splits that across your
   categories in proportion to how you spent in the statement (e.g. Shopping
   ₹1,000, Food ₹2,000 ...). Log expenses as you go: each category shows spent,
   left and a progress bar (amber at 80%, red when over), plus whether you're
   still on track to hit your savings goal. Changing the goal recalculates the
   budgets but keeps the expenses you've logged. Uploading a new statement
   clears the plan, because it was based on the old statement's balance.
6. Ask questions in the "Ask a question" box. If you don't mention a year
   ("food in June"), it uses the year your statement actually contains, and
   "last month" means the month before your statement's latest transaction.

---

## Notes on statement formats

- **CSV/XLSX**: the parser auto-detects the header row (skipping bank
  disclaimer/title rows) and maps common column names (Narration/Particulars,
  Debit/Withdrawal, Credit/Deposit, Balance, etc.) automatically. If your
  bank uses very unusual headers, `backend/parsing/normalize.py` is the file
  to extend -- add your bank's column name to the relevant `*_HEADERS` list.
- **PDF**: works for statements where transactions render as a real table
  (most Indian bank e-statements). Scanned/image-only PDFs aren't supported
  out of the box (would need OCR, e.g. `pytesseract`, as an extra step).
  Password-protected PDFs are supported -- enter the password in the upload
  form.
- **Duplicate uploads / re-uploads**: each upload **replaces** the previous
  statement's transactions entirely — this app is designed to analyze one
  statement at a time rather than build a lifetime history across uploads.
  If you want to look at last month's statement again after uploading this
  month's, just re-upload the file you want to view. (Category corrections
  you've made are preserved across uploads, so the model keeps improving no
  matter which statement is currently loaded.)

---

## Extending it

- **New categories / merchants**: edit `backend/data/category_seed.json`,
  then re-run `python ml/train_model.py`.
- **New anomaly rules**: add a function in `backend/anomaly/detector.py`.
- **New question types**: extend the keyword lists and intent branches in
  `backend/nlu/query_parser.py` / `query_engine.py`.
- **Multi-user support**: currently single-user/local by design (one SQLite
  file). To support multiple people, add a `user_id` column to `Transaction`
  / `UploadBatch` and add basic auth -- not included here to keep the
  self-hosted setup simple.
