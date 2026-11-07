"""Reports over invoices: pagination, monthly revenue, aging buckets."""
import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from .invoices import Invoice, days_overdue
from .money import money, money_sum

AGING_BUCKETS = ("current", "1-30", "31-60", "61-90", "90+")


@dataclass(frozen=True)
class Page:
    items: list
    page: int
    page_size: int
    total_count: int
    total_pages: int
    has_next: bool


def paginate(seq, page: int, page_size: int) -> Page:
    """1-indexed pages. A page past the end is empty (not an error)."""
    if page < 1 or page_size < 1:
        raise ValueError("page and page_size must be >= 1")
    seq = list(seq)
    total_pages = math.ceil(len(seq) / page_size)
    start = (page - 1) * page_size
    items = seq[start:start + page_size]
    return Page(items, page, page_size, len(seq), total_pages, page < total_pages)


def invoice_page(invoices, page: int, page_size: int) -> Page:
    """Newest first; ties broken by invoice_id ascending. Does not mutate input."""
    ordered = sorted(invoices, key=lambda i: i.invoice_id)
    ordered = sorted(ordered, key=lambda i: i.issued_at, reverse=True)
    return paginate(ordered, page, page_size)


def revenue_by_month(invoices, tz: str = "UTC") -> dict:
    """Sum of invoice totals keyed by 'YYYY-MM' of the issue instant in `tz`."""
    zone = ZoneInfo(tz)
    out = {}
    for inv in invoices:
        key = inv.issued_at.astimezone(zone).strftime("%Y-%m")
        out[key] = money(out.get(key, Decimal("0")) + inv.totals.total)
    return dict(sorted(out.items()))


def aging_bucket(days: int) -> str:
    if days <= 0:
        return "current"
    if days <= 30:
        return "1-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"


def aging_report(invoices, now: datetime) -> dict:
    """Outstanding amount per aging bucket. Every bucket is always present."""
    out = {b: [] for b in AGING_BUCKETS}
    for inv in invoices:
        out[aging_bucket(days_overdue(inv, now))].append(inv.totals.total)
    return {b: money_sum(v) for b, v in out.items()}


def customer_statement(invoices, customer_id: str, page: int, page_size: int) -> Page:
    """One page of a customer's invoices for their statement: newest first, ties broken
    by invoice_id ascending. Does not mutate input."""
    mine = [i for i in invoices if i.customer_id == customer_id]
    return invoice_page(mine, page, page_size)


def top_customers(invoices, n: int) -> list:
    """[(customer_id, total)] for the n customers with the highest invoiced total;
    ties broken by customer_id ascending. Totals are cent-rounded Decimals."""
    if n < 0:
        raise ValueError("n must be >= 0")
    totals = {}
    for inv in invoices:
        totals[inv.customer_id] = money(totals.get(inv.customer_id, Decimal("0")) + inv.totals.total)
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:n]


def overdue_invoices(invoices, now: datetime) -> list:
    """Invoices overdue at `now` (customer-local dates), most overdue first, ties by
    invoice_id ascending. Invoices due today are not overdue."""
    late = [(days_overdue(i, now), i) for i in invoices]
    late = [(d, i) for d, i in late if d > 0]
    late.sort(key=lambda di: (-di[0], di[1].invoice_id))
    return [i for _, i in late]
