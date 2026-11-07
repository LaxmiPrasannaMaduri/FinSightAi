"""Small formatting helpers shared by the chatbot and the savings planner."""


def inr(amount: float, decimals: int = 0) -> str:
    """Rupees with Indian digit grouping: 116149.46 -> ₹1,16,149 ; 1234567.5 (2dp) -> ₹12,34,567.50"""
    sign = "-" if amount < 0 else ""
    whole, _, frac = f"{abs(amount):.{decimals}f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}" + (f".{frac}" if frac else "")


def inr_auto(amount: float) -> str:
    """Whole rupees when there are no paise, otherwise two decimals."""
    return inr(amount, 0 if abs(amount - round(amount)) < 0.005 else 2)
