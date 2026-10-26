from decimal import ROUND_HALF_UP, Decimal

TWO = Decimal("0.01")


def to_money(value) -> Decimal:
    return Decimal(str(value)).quantize(TWO, rounding=ROUND_HALF_UP)


def order_total(unit_price: Decimal, squads: int) -> Decimal:
    if squads < 1:
        raise ValueError("squads must be >= 1")
    return to_money(Decimal(unit_price) * squads)


def fmt_npr(value: Decimal) -> str:
    s = f"{to_money(value):,.2f}"
    return s[:-3] if s.endswith(".00") else s


def fmt_money(value, currency: str = "NPR") -> str:
    """Amount in the customer's currency: 'NPR 375' or '3.50 USDT'."""
    v = to_money(value)
    if currency == "USDT":
        return f"{v:,.2f} USDT"
    return f"NPR {fmt_npr(v)}"


def to_minor(value) -> int:
    """Integer minor units (paisa / cents): safe to compare."""
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))
