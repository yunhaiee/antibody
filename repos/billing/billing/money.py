"""Money helpers. All amounts are Decimal, quantized to cents, ROUND_HALF_UP."""
from decimal import Decimal, ROUND_HALF_UP

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """Convert int/str/Decimal to a cent-quantized Decimal. Floats are rejected."""
    if isinstance(value, float):
        raise TypeError("money() does not accept float; pass str or Decimal")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_sum(values) -> Decimal:
    total = ZERO
    for v in values:
        total += money(v)
    return money(total)
