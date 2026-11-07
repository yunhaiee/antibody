"""Invoices: issue time is a timezone-aware UTC instant; due dates and overdue
status are evaluated on the customer's local calendar date."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .totals import Totals, compute_totals


@dataclass(frozen=True)
class Invoice:
    invoice_id: str
    customer_id: str
    items: tuple
    region: str
    issued_at: datetime
    net_days: int
    customer_tz: str
    totals: Totals


def issue_invoice(invoice_id, customer_id, items, region, issued_at: datetime,
                  net_days: int = 30, customer_tz: str = "UTC", shipping="0") -> Invoice:
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise ValueError("issued_at must be timezone-aware")
    if net_days < 0:
        raise ValueError("net_days must be >= 0")
    ZoneInfo(customer_tz)  # validate
    items = tuple(items)
    totals = compute_totals(items, region, shipping)
    return Invoice(invoice_id, customer_id, items, region,
                   issued_at.astimezone(ZoneInfo("UTC")), net_days, customer_tz, totals)


def local_issue_date(inv: Invoice) -> date:
    return inv.issued_at.astimezone(ZoneInfo(inv.customer_tz)).date()


def due_date(inv: Invoice) -> date:
    return local_issue_date(inv) + timedelta(days=inv.net_days)


def days_overdue(inv: Invoice, now: datetime) -> int:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.astimezone(ZoneInfo(inv.customer_tz)).date()
    return max(0, (today - due_date(inv)).days)


def is_overdue(inv: Invoice, now: datetime) -> bool:
    return days_overdue(inv, now) > 0


@dataclass(frozen=True)
class CreditNote:
    note_id: str
    invoice_id: str
    amount: object
    issued_at: datetime


def issue_credit_note(note_id: str, inv: Invoice, amount, issued_at: datetime) -> CreditNote:
    """Credit (part of) an invoice. issued_at must be timezone-aware and is stored in UTC;
    it may not precede the invoice. amount must be > 0 and <= the invoice total."""
    from .money import money
    if issued_at.tzinfo is None or issued_at.utcoffset() is None:
        raise ValueError("issued_at must be timezone-aware")
    issued_at = issued_at.astimezone(ZoneInfo("UTC"))
    if issued_at < inv.issued_at:
        raise ValueError("credit note cannot precede its invoice")
    amt = money(amount)
    if amt <= 0 or amt > inv.totals.total:
        raise ValueError("amount must be in (0, invoice total]")
    return CreditNote(note_id, inv.invoice_id, amt, issued_at)


def reminder_date(inv: Invoice, days_before: int = 3) -> date:
    """Customer-local calendar date on which to send a payment reminder."""
    if days_before < 0:
        raise ValueError("days_before must be >= 0")
    return due_date(inv) - timedelta(days=days_before)
