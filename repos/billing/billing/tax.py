"""Sales tax per region and category. Tax is charged on the discounted (net) amount,
computed per line and rounded per line."""
from decimal import Decimal

from .line_items import LineItem, line_net
from .money import money, money_sum

TAX_RATES = {
    "US-CA": {"standard": Decimal("0.0725"), "food": Decimal("0"), "digital": Decimal("0.0725")},
    "US-NY": {"standard": Decimal("0.04"), "food": Decimal("0"), "digital": Decimal("0.04")},
    "DE": {"standard": Decimal("0.19"), "food": Decimal("0.07"), "digital": Decimal("0.19")},
    "JP": {"standard": Decimal("0.10"), "food": Decimal("0.08"), "digital": Decimal("0.10")},
}


def tax_rate(region: str, category: str) -> Decimal:
    if region not in TAX_RATES:
        raise KeyError(f"unknown region {region!r}")
    return TAX_RATES[region][category]


def line_tax(item: LineItem, region: str) -> Decimal:
    return money(line_net(item) * tax_rate(region, item.tax_category))


def total_tax(items, region: str) -> Decimal:
    return money_sum(line_tax(i, region) for i in items)


def tax_breakdown(items, region: str) -> dict:
    """Tax per category, only for categories present in items."""
    out = {}
    for i in items:
        out[i.tax_category] = money(out.get(i.tax_category, Decimal("0")) + line_tax(i, region))
    return out
