from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship

from .database import Base


class UploadBatch(Base):
    """One uploaded statement file (PDF or CSV)."""
    __tablename__ = "upload_batches"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    source_type = Column(String)  # "pdf" or "csv"
    num_transactions = Column(Integer, default=0)

    transactions = relationship("Transaction", back_populates="batch")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, ForeignKey("upload_batches.id"))

    date = Column(DateTime, nullable=False, index=True)
    description = Column(Text, nullable=False)
    amount = Column(Float, nullable=False)          # always positive
    direction = Column(String, nullable=False)       # "debit" or "credit"
    balance = Column(Float, nullable=True)

    category = Column(String, index=True)
    category_confidence = Column(Float, default=0.0)
    category_source = Column(String, default="model")  # "model" | "rule" | "user"

    is_flagged = Column(Boolean, default=False)
    flag_reason = Column(String, nullable=True)

    # simple de-dup fingerprint: date+amount+description hash
    fingerprint = Column(String, index=True)

    batch = relationship("UploadBatch", back_populates="transactions")


class Correction(Base):
    """
    Stores a user's manual re-categorization of a transaction.
    Used to retrain / improve the model over time (see train_model.py).
    """
    __tablename__ = "corrections"

    id = Column(Integer, primary_key=True, index=True)
    transaction_id = Column(Integer, ForeignKey("transactions.id"), nullable=True)
    description = Column(Text, nullable=False)
    predicted_category = Column(String)
    correct_category = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class BudgetPlan(Base):
    """
    The user's savings plan (one active plan at a time): how much balance they
    have, how much they want to save, and the per-category spending budgets
    that were worked out from that.
    """
    __tablename__ = "budget_plans"

    id = Column(Integer, primary_key=True, index=True)
    balance = Column(Float, nullable=False)
    savings_goal = Column(Float, nullable=False)
    budgets_json = Column(Text, nullable=False, default="{}")   # {"Shopping": 1000.0, ...}
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)


class PlanExpense(Base):
    """An expense the user logged against their plan (the expense-tracker part)."""
    __tablename__ = "plan_expenses"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("budget_plans.id"), index=True)
    category = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    note = Column(String, nullable=True)
    spent_on = Column(DateTime, default=datetime.utcnow)
