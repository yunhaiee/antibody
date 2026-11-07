"""Line items: price x quantity, minus a percentage discount."""
from dataclasses import dataclass
from decimal import Decimal

from .money import money

VALID_CATEGORIES = ("standard", "food", "digital")


@dataclass(frozen=True)
class LineItem:
    sku: str
    description: str
    unit_price: Decimal
    quantity: int
    discount_pct: Decimal = Decimal("0")
    tax_category: str = "standard"


def make_line_item(sku, description, unit_price, quantity,
                   discount_pct="0", tax_category="standard") -> LineItem:
    if not sku:
        raise ValueError("sku is required")
    if not isinstance(quantity, int) or quantity <= 0:
        raise ValueError("quantity must be a positive int")
    price = money(unit_price)
    if price < 0:
        raise ValueError("unit_price must be >= 0")
    pct = Decimal(str(discount_pct)) if not isinstance(discount_pct, Decimal) else discount_pct
    if pct < 0 or pct > 100:
        raise ValueError("discount_pct must be in [0, 100]")
    if tax_category not in VALID_CATEGORIES:
        raise ValueError(f"unknown tax_category {tax_category!r}")
    return LineItem(sku, description, price, quantity, pct, tax_category)


def line_subtotal(item: LineItem) -> Decimal:
    return money(item.unit_price * item.quantity)


def line_discount(item: LineItem) -> Decimal:
    return money(line_subtotal(item) * item.discount_pct / Decimal(100))


def line_net(item: LineItem) -> Decimal:
    return money(line_subtotal(item) - line_discount(item))
