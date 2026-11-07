"""Order totals: subtotal - discount + tax + shipping."""
from dataclasses import dataclass
from decimal import Decimal

from .line_items import line_discount, line_subtotal
from .money import ZERO, money, money_sum
from .tax import total_tax

FREE_SHIPPING_THRESHOLD = Decimal("100.00")


@dataclass(frozen=True)
class Totals:
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    shipping: Decimal
    total: Decimal


def compute_totals(items, region: str, shipping="0") -> Totals:
    """Shipping is waived when the discounted amount (subtotal - discount) is
    >= FREE_SHIPPING_THRESHOLD. Tax never applies to shipping."""
    items = list(items)
    if not items:
        raise ValueError("an order needs at least one line item")
    subtotal = money_sum(line_subtotal(i) for i in items)
    discount = money_sum(line_discount(i) for i in items)
    tax = total_tax(items, region)
    ship = money(shipping)
    if ship < 0:
        raise ValueError("shipping must be >= 0")
    if subtotal - discount >= FREE_SHIPPING_THRESHOLD:
        ship = ZERO
    total = money(subtotal - discount + tax + ship)
    return Totals(subtotal, discount, tax, ship, total)


def shipping_cost(items, region: str, base_shipping="0") -> Decimal:
    """Shipping charged for an order: waived when the discounted amount reaches the
    free-shipping threshold (the same rule compute_totals applies)."""
    items = list(items)
    if not items:
        raise ValueError("an order needs at least one line item")
    ship = money(base_shipping)
    if ship < 0:
        raise ValueError("shipping must be >= 0")
    discounted = money_sum(line_subtotal(i) for i in items) - money_sum(line_discount(i) for i in items)
    return ZERO if discounted >= FREE_SHIPPING_THRESHOLD else ship
