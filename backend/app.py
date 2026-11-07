"""
app.py
------
FastAPI backend tying together: parsing -> categorization -> storage ->
NLU query answering -> anomaly detection.

Run with:  uvicorn app:app --reload --port 8000
(see README.md in the project root for full setup instructions)
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.database import init_db, get_db
from db.models import Transaction, UploadBatch, Correction, BudgetPlan, PlanExpense
from parsing.csv_parser import parse_csv, parse_excel
from parsing.pdf_parser import parse_pdf
from parsing.normalize import fingerprint
from ml.categorizer import categorizer
from nlu.query_engine import answer_query
from analytics import available_months, monthly_breakdown
from planner import PlanError, build_payload, create_plan, validate_expense

BASE_DIR = Path(__file__).resolve().parent
SEED_FILE = BASE_DIR / "data" / "category_seed.json"
with open(SEED_FILE) as f:
    CATEGORY_SEED = json.load(f)
KNOWN_CATEGORIES = sorted(CATEGORY_SEED.keys())

app = FastAPI(title="Bank Statement / UPI Expense Analyst")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def no_store_api_responses(request: Request, call_next):
    """
    Browsers will happily cache GET responses like /api/transactions and
    /api/summary if no cache header says otherwise -- which is exactly
    what caused "uploading a new statement still shows old data" (the
    browser served a cached copy instead of re-hitting the server).
    Force every /api/* response to be treated as fresh, every time.
    """
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------- upload ----

@app.post("/api/upload")
async def upload_statement(file: UploadFile = File(...), password: str = Form(None), db: Session = Depends(get_db)):
    content = await file.read()
    filename = file.filename or "statement"
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    try:
        if ext == "csv":
            df = parse_csv(content)
            source_type = "csv"
        elif ext in ("xlsx", "xls"):
            df = parse_excel(content)
            source_type = "csv"
        elif ext == "pdf":
            df = parse_pdf(content, password=password)
            source_type = "pdf"
        else:
            raise HTTPException(400, f"Unsupported file type: .{ext}. Use CSV, XLSX, or PDF.")
    except ValueError as e:
        raise HTTPException(422, str(e))

    if df.empty:
        raise HTTPException(422, "No transactions could be extracted from this file.")

    # Each upload REPLACES the previous statement's data (by design, per user
    # preference): this app analyzes one statement at a time rather than
    # accumulating a lifetime history across uploads. Correction history is
    # deliberately NOT wiped here, so the categorizer keeps improving across
    # statements even though the transaction list itself starts fresh.
    db.query(Transaction).delete()
    db.query(UploadBatch).delete()
    # The savings plan was built from the old statement's balance, so it goes too.
    db.query(PlanExpense).delete()
    db.query(BudgetPlan).delete()
    db.flush()

    batch = UploadBatch(filename=filename, source_type=source_type, num_transactions=0)
    db.add(batch)
    db.flush()

    inserted = 0
    for _, row in df.iterrows():
        fp = fingerprint(row["date"], row["description"], row["amount"])
        category, confidence, source = categorizer.predict(row["description"])
        txn = Transaction(
            batch_id=batch.id,
            date=row["date"],
            description=row["description"],
            amount=row["amount"],
            direction=row["direction"],
            balance=row.get("balance"),
            category=str(category),
            category_confidence=float(confidence),
            category_source=source,
            fingerprint=fp,
        )
        db.add(txn)
        inserted += 1

    batch.num_transactions = inserted
    db.commit()

    return {
        "batch_id": batch.id,
        "filename": filename,
        "inserted": inserted,
    }


# ----------------------------------------------------------- transactions ----

@app.get("/api/transactions")
def list_transactions(limit: int = 500, db: Session = Depends(get_db)):
    rows = db.query(Transaction).order_by(Transaction.date.desc()).limit(limit).all()
    return [_txn_to_dict(t) for t in rows]


@app.get("/api/summary")
def summary(db: Session = Depends(get_db)):
    rows = db.query(Transaction.category, func.sum(Transaction.amount)) \
        .filter(Transaction.direction == "debit") \
        .group_by(Transaction.category).all()
    total_spend = sum(r[1] for r in rows)
    total_income = db.query(func.sum(Transaction.amount)) \
        .filter(Transaction.direction == "credit").scalar() or 0
    return {
        "by_category": {r[0]: r[1] for r in rows},
        "total_spend": total_spend,
        "total_income": total_income,
        "transaction_count": db.query(Transaction).count(),
    }


# ------------------------------------------------------- monthly pie charts ----

@app.get("/api/months")
def list_months(db: Session = Depends(get_db)):
    """Months present in the loaded statement, for the pie-chart dropdown."""
    rows = db.query(Transaction).all()
    return available_months([_txn_to_query_dict(t) for t in rows])


@app.get("/api/breakdown")
def breakdown(month: str = "all", db: Session = Depends(get_db)):
    """Credit + debit category totals for one month ("YYYY-MM") or "all"."""
    rows = db.query(Transaction).all()
    try:
        return monthly_breakdown([_txn_to_query_dict(t) for t in rows], month)
    except ValueError:
        raise HTTPException(400, "month must look like 2026-06, or be 'all'")


# ------------------------------------------ savings plan + expense tracker ----

def _plan_payload(db: Session):
    """Current plan (or None) + the statement's balance, in the shape the UI needs."""
    txns = [_txn_to_query_dict(t) for t in db.query(Transaction).all()]
    plan = db.query(BudgetPlan).first()
    plan_dict, expenses = None, []
    if plan:
        plan_dict = {
            "balance": plan.balance,
            "savings_goal": plan.savings_goal,
            "budgets": json.loads(plan.budgets_json or "{}"),
        }
        rows = db.query(PlanExpense).filter(PlanExpense.plan_id == plan.id) \
            .order_by(PlanExpense.id.desc()).all()
        expenses = [{"id": e.id, "category": e.category, "amount": e.amount,
                     "note": e.note or "", "date": e.spent_on.isoformat()} for e in rows]
    return build_payload(txns, KNOWN_CATEGORIES, plan_dict, expenses)


@app.get("/api/plan")
def get_plan(db: Session = Depends(get_db)):
    return _plan_payload(db)


@app.post("/api/plan")
def save_plan(savings_goal: float = Form(...), balance: Optional[float] = Form(None),
              db: Session = Depends(get_db)):
    """
    Create the plan, or recalculate it when the goal/balance changes.
    Expenses already logged are kept, so adjusting the goal doesn't wipe the tracker.
    """
    txns = [_txn_to_query_dict(t) for t in db.query(Transaction).all()]
    try:
        new_plan = create_plan(txns, savings_goal, balance)
    except PlanError as e:
        raise HTTPException(400, str(e))

    plan = db.query(BudgetPlan).first()
    if plan is None:
        plan = BudgetPlan()
        db.add(plan)
    plan.balance = new_plan["balance"]
    plan.savings_goal = new_plan["savings_goal"]
    plan.budgets_json = json.dumps(new_plan["budgets"])
    plan.updated_at = datetime.utcnow()
    db.commit()
    return _plan_payload(db)


@app.delete("/api/plan")
def reset_plan(db: Session = Depends(get_db)):
    db.query(PlanExpense).delete()
    db.query(BudgetPlan).delete()
    db.commit()
    return _plan_payload(db)


@app.post("/api/plan/expense")
def add_plan_expense(category: str = Form(...), amount: float = Form(...), note: str = Form(""),
                     db: Session = Depends(get_db)):
    plan = db.query(BudgetPlan).first()
    if plan is None:
        raise HTTPException(400, "Create a savings plan first.")
    try:
        category, amount = validate_expense(category, amount, KNOWN_CATEGORIES)
    except PlanError as e:
        raise HTTPException(400, str(e))
    db.add(PlanExpense(plan_id=plan.id, category=category, amount=amount,
                       note=(note or "").strip()[:200] or None))
    db.commit()
    return _plan_payload(db)


@app.delete("/api/plan/expense/{expense_id}")
def delete_plan_expense(expense_id: int, db: Session = Depends(get_db)):
    expense = db.get(PlanExpense, expense_id)
    if expense is None:
        raise HTTPException(404, "Expense not found")
    db.delete(expense)
    db.commit()
    return _plan_payload(db)


# ----------------------------------------------------------- corrections ----

@app.post("/api/correct")
def correct_category(transaction_id: int = Form(...), correct_category: str = Form(...),
                      db: Session = Depends(get_db)):
    txn = db.get(Transaction, transaction_id)
    if not txn:
        raise HTTPException(404, "Transaction not found")

    correction = Correction(
        transaction_id=txn.id,
        description=txn.description,
        predicted_category=txn.category,
        correct_category=correct_category,
    )
    db.add(correction)

    txn.category = correct_category
    txn.category_source = "user"
    txn.category_confidence = 1.0
    db.commit()
    return {"status": "ok", "transaction_id": txn.id, "category": correct_category}


@app.post("/api/retrain")
def retrain(db: Session = Depends(get_db)):
    """
    Re-runs ml/train_model.py, which folds in every correction logged so
    far, hot-reloads the model into the running categorizer, and then
    re-categorizes every already-stored transaction that wasn't manually
    corrected by the user. Without this last step, old rows would keep
    whatever category they were assigned at upload time forever, and
    both the summary panel and the chatbot (which both read straight
    from the database) would keep showing stale results even though a
    fresh model was loaded.
    """
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "ml" / "train_model.py")],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise HTTPException(500, f"Retrain failed: {result.stderr[-2000:]}")
    categorizer.reload()

    updated = 0
    rows = db.query(Transaction).filter(Transaction.category_source != "user").all()
    for txn in rows:
        category, confidence, source = categorizer.predict(txn.description)
        category = str(category)
        if category != txn.category or source != txn.category_source:
            txn.category = category
            txn.category_confidence = float(confidence)
            txn.category_source = source
            updated += 1
    db.commit()

    return {
        "status": "retrained",
        "recategorized": updated,
        "log_tail": result.stdout[-2000:],
    }


# ------------------------------------------------------------------ ask ----

@app.post("/api/ask")
def ask(question: str = Form(...), db: Session = Depends(get_db)):
    rows = db.query(Transaction).all()
    transactions = [_txn_to_query_dict(t) for t in rows]
    result = answer_query(question, transactions, KNOWN_CATEGORIES, CATEGORY_SEED)
    return {"answer": result["answer"], "data": _jsonable(result["data"])}


@app.get("/api/categories")
def get_categories():
    return KNOWN_CATEGORIES


# --------------------------------------------------------------- helpers ----

def _txn_to_dict(t: Transaction):
    return {
        "id": t.id,
        "date": t.date.isoformat(),
        "description": t.description,
        "amount": t.amount,
        "direction": t.direction,
        "balance": t.balance,
        "category": t.category,
        "category_confidence": t.category_confidence,
        "category_source": t.category_source,
    }


def _txn_to_query_dict(t: Transaction):
    return {
        "id": t.id,
        "date": t.date,
        "description": t.description,
        "amount": t.amount,
        "direction": t.direction,
        "category": t.category,
        "balance": t.balance,
    }


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


# serve the simple frontend (index.html/app.js/style.css) as static files
FRONTEND_DIR = BASE_DIR.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
