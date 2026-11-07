"""
planner.py
----------
Savings plan + expense tracker logic (pure Python, no web/database code, so
it can be tested on its own).

How the plan works
  1. The user's remaining balance is read from the latest transaction of the
     uploaded statement (they can override it).
  2. The user says how much they want to SAVE.
  3. spendable = balance - savings goal.
  4. The spendable amount is split across the categories the user actually
     spends on, in proportion to their past spending in this statement
     (e.g. Shopping Rs 1,000, Food Rs 2,000 ...), rounded to tidy numbers
     and adjusted so the budgets add up to the spendable amount exactly.
  5. As the user logs new expenses, each category shows spent / left, and
     the plan shows whether the savings goal is still on track.
"""

from collections import defaultdict

from formatting import inr as _inr


class PlanError(ValueError):
    """A problem the user can fix (bad input); the message is shown to them."""


def _rupees(x: float) -> str:
    return _inr(x)


def latest_balance(transactions):
    """Balance after the most recent transaction in the statement, or None."""
    rows = [t for t in transactions if t.get("balance") is not None]
    if not rows:
        return None
    latest = max(rows, key=lambda t: (t["date"], t["id"]))
    return float(latest["balance"])


def suggest_budget(transactions, spendable: float):
    """{category: rupees} - spendable split in proportion to past debit spending."""
    spend = defaultdict(float)
    for t in transactions:
        if t["direction"] == "debit":
            spend[t["category"]] += t["amount"]
    total = sum(spend.values())
    whole = int(round(spendable))
    if total <= 0 or whole <= 0:
        return {}

    step = 10 if whole < 5_000 else 50 if whole < 50_000 else 100   # tidy numbers
    budget = {}
    for cat, amt in spend.items():
        b = round(whole * amt / total / step) * step
        if b > 0:
            budget[cat] = float(b)

    if not budget:                       # spendable is tiny: give it all to the top category
        return {max(spend, key=spend.get): float(whole)}

    # rounding leaves a few rupees over/under; put them on the biggest category
    leftover = whole - sum(budget.values())
    biggest = max(budget, key=budget.get)
    budget[biggest] = max(0.0, budget[biggest] + leftover)
    return dict(sorted(budget.items(), key=lambda kv: kv[1], reverse=True))


def create_plan(transactions, savings_goal, balance=None):
    """Validate the inputs and return {'balance', 'savings_goal', 'budgets'}."""
    if balance is None:
        balance = latest_balance(transactions)
    if balance is None:
        raise PlanError("I couldn't find a balance in your statement. Enter your remaining balance.")
    if savings_goal is None or savings_goal < 0:
        raise PlanError("Enter how much you want to save (0 or more).")
    if balance <= 0:
        raise PlanError("Your remaining balance must be more than ₹0 to make a plan.")

    spendable = balance - savings_goal
    if spendable <= 0:
        if savings_goal > balance:
            raise PlanError(f"You want to save {_rupees(savings_goal)} but your balance is only "
                            f"{_rupees(balance)}. Lower the savings goal (by at least "
                            f"{_rupees(savings_goal - balance)}).")
        raise PlanError("Saving your entire balance leaves nothing to spend. Choose a smaller amount to save.")

    budgets = suggest_budget(transactions, spendable)
    if not budgets:
        raise PlanError("There is no spending in this statement to base a budget on. "
                        "Upload a statement that has debit transactions.")
    return {"balance": round(float(balance), 2), "savings_goal": round(float(savings_goal), 2),
            "budgets": budgets}


def validate_expense(category, amount, known_categories):
    if category not in known_categories:
        raise PlanError("Pick a category for this expense.")
    if amount is None or amount <= 0:
        raise PlanError("Enter an expense amount greater than ₹0.")
    return category, round(float(amount), 2)


def plan_status(balance, savings_goal, budgets, expenses):
    """Per-category budget / spent / left plus overall tracking numbers."""
    spent_by = defaultdict(float)
    for e in expenses:
        spent_by[e["category"]] += e["amount"]

    categories = list(budgets) + [c for c in spent_by if c not in budgets]
    rows = []
    for cat in categories:
        budget = float(budgets.get(cat, 0.0))
        spent = round(spent_by.get(cat, 0.0), 2)
        pct = (spent / budget * 100) if budget > 0 else (100.0 if spent > 0 else 0.0)
        status = "over" if spent > budget else ("warn" if pct >= 80 else "ok")
        rows.append({"category": cat, "budget": budget, "spent": spent,
                     "left": round(budget - spent, 2), "percent": round(pct, 1), "status": status})

    spendable = round(balance - savings_goal, 2)
    total_spent = round(sum(spent_by.values()), 2)
    projected_savings = round(balance - total_spent, 2)
    return {
        "balance": balance,
        "savings_goal": savings_goal,
        "spendable": spendable,
        "total_budget": round(sum(budgets.values()), 2),
        "total_spent": total_spent,
        "left_to_spend": round(spendable - total_spent, 2),
        "projected_savings": projected_savings,
        "on_track": projected_savings >= savings_goal,
        "rows": rows,
    }


def build_payload(transactions, known_categories, plan=None, expenses=()):
    """Everything the frontend needs in one response."""
    payload = {
        "has_statement": bool(transactions),
        "statement_balance": latest_balance(transactions),
        "categories": list(known_categories),
        "plan": None,
    }
    if plan:
        status = plan_status(plan["balance"], plan["savings_goal"], plan["budgets"], expenses)
        status["expenses"] = list(expenses)
        payload["plan"] = status
    return payload
