"""Hidden reference suite. This, not any model, decides whether a change is good."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest
from hypothesis import given, settings, strategies as st

from billing.money import money, money_sum
from billing.line_items import make_line_item, line_subtotal, line_discount, line_net
from billing.tax import line_tax, total_tax, tax_breakdown, tax_rate
from billing.totals import compute_totals
from billing.invoices import issue_invoice, due_date, is_overdue, days_overdue, local_issue_date
from billing.reports import paginate, invoice_page, revenue_by_month, aging_report, aging_bucket

UTC = timezone.utc


def item(price="10.00", qty=1, disc="0", cat="standard", sku="SKU1"):
    return make_line_item(sku, "thing", price, qty, disc, cat)


def inv(iid="INV1", items=None, at=None, net=30, tz="UTC", region="US-CA", ship="0"):
    return issue_invoice(iid, "C1", items or [item()], region,
                         at or datetime(2026, 1, 15, 12, tzinfo=UTC), net, tz, ship)


# ---------- money ----------
def test_money_rejects_float():
    with pytest.raises(TypeError):
        money(0.1)

def test_money_half_up():
    assert money("2.675") == D("2.68")
    assert money("0.125") == D("0.13")
    assert money("-0.125") == D("-0.13")

def test_money_sum_exact():
    assert money_sum(["0.10"] * 10) == D("1.00")
    assert isinstance(money_sum(["0.10", "0.20"]), D)


# ---------- line items ----------
def test_line_basic():
    li = item("19.99", 3, "10")
    assert line_subtotal(li) == D("59.97")
    assert line_discount(li) == D("6.00")
    assert line_net(li) == D("53.97")

def test_line_types_are_decimal():
    li = item("0.10", 3)
    for v in (line_subtotal(li), line_discount(li), line_net(li)):
        assert isinstance(v, D)
    assert line_subtotal(li) == D("0.30")

@pytest.mark.parametrize("kw", [dict(qty=0), dict(qty=-1), dict(disc="101"), dict(disc="-1"),
                                dict(price="-1"), dict(cat="luxury"), dict(sku="")])
def test_line_validation(kw):
    with pytest.raises(ValueError):
        item(**kw)

def test_full_discount():
    assert line_net(item("50.00", 2, "100")) == D("0.00")


# ---------- tax ----------
def test_tax_on_net_not_gross():
    li = item("100.00", 1, "50")
    assert line_tax(li, "DE") == D("9.50")

def test_tax_category_rates():
    assert line_tax(item("10.00", cat="food"), "US-CA") == D("0.00")
    assert line_tax(item("10.00", cat="food"), "DE") == D("0.70")

def test_tax_rounded_per_line():
    items = [item("0.07", 1, sku=f"S{i}") for i in range(10)]
    # 0.07 * 0.0725 = 0.005075 -> 0.01 per line
    assert total_tax(items, "US-CA") == D("0.10")

def test_tax_unknown_region():
    with pytest.raises(KeyError):
        tax_rate("MARS", "standard")

def test_tax_breakdown_only_present():
    b = tax_breakdown([item("10.00", cat="food"), item("10.00")], "DE")
    assert b == {"food": D("0.70"), "standard": D("1.90")}


# ---------- totals ----------
def test_totals_basic():
    t = compute_totals([item("40.00", 2, "10")], "US-NY", "5.00")
    assert (t.subtotal, t.discount, t.tax, t.shipping) == (D("80.00"), D("8.00"), D("2.88"), D("5.00"))
    assert t.total == D("79.88")

def test_free_shipping_boundary_uses_discounted_amount():
    at = compute_totals([item("100.00")], "US-NY", "7.00")
    assert at.shipping == D("0.00")
    below = compute_totals([item("100.00", 1, "1")], "US-NY", "7.00")
    assert below.shipping == D("7.00")

def test_shipping_not_taxed():
    t = compute_totals([item("10.00")], "DE", "10.00")
    assert t.tax == D("1.90")

def test_totals_empty_rejected():
    with pytest.raises(ValueError):
        compute_totals([], "US-CA")

def test_totals_accepts_generator():
    t = compute_totals((item("1.00") for _ in range(3)), "US-NY")
    assert t.subtotal == D("3.00")


# ---------- invoices ----------
def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        inv(at=datetime(2026, 1, 15, 12))

def test_due_date_customer_local():
    # 2026-01-31 03:00 UTC is still Jan 30 in Los Angeles
    i = inv(at=datetime(2026, 1, 31, 3, tzinfo=UTC), net=30, tz="America/Los_Angeles")
    assert local_issue_date(i).isoformat() == "2026-01-30"
    assert due_date(i).isoformat() == "2026-03-01"

def test_due_date_ahead_tz():
    i = inv(at=datetime(2026, 3, 31, 20, tzinfo=UTC), net=0, tz="Asia/Seoul")
    assert due_date(i).isoformat() == "2026-04-01"

def test_issued_at_normalized_to_utc():
    kst = timezone(timedelta(hours=9))
    i = inv(at=datetime(2026, 5, 1, 8, tzinfo=kst))
    assert i.issued_at.utcoffset() == timedelta(0)
    assert i.issued_at.hour == 23

def test_overdue_boundary():
    i = inv(at=datetime(2026, 1, 1, 12, tzinfo=UTC), net=10)
    assert not is_overdue(i, datetime(2026, 1, 11, 23, 59, tzinfo=UTC))
    assert is_overdue(i, datetime(2026, 1, 12, 0, 1, tzinfo=UTC))
    assert days_overdue(i, datetime(2026, 1, 21, 9, tzinfo=UTC)) == 10

def test_overdue_uses_customer_tz():
    i = inv(at=datetime(2026, 1, 1, 12, tzinfo=UTC), net=10, tz="Asia/Seoul")
    # 2026-01-11 16:00 UTC == 2026-01-12 01:00 KST -> overdue in Seoul
    assert is_overdue(i, datetime(2026, 1, 11, 16, tzinfo=UTC))

def test_invoice_totals_flow_through():
    i = inv(items=[item("10.00", 2)], region="JP", ship="3.00")
    assert i.totals.total == D("25.00")


# ---------- reports ----------
def test_paginate_basic():
    p = paginate(range(10), 1, 3)
    assert p.items == [0, 1, 2] and p.total_pages == 4 and p.has_next

def test_paginate_last_and_past_end():
    assert paginate(range(10), 4, 3).items == [9]
    assert not paginate(range(10), 4, 3).has_next
    assert paginate(range(10), 5, 3).items == []

def test_paginate_exact_multiple():
    p = paginate(range(9), 3, 3)
    assert p.items == [6, 7, 8] and p.total_pages == 3 and not p.has_next

def test_paginate_empty():
    p = paginate([], 1, 5)
    assert p.items == [] and p.total_pages == 0 and not p.has_next

def test_paginate_invalid():
    with pytest.raises(ValueError):
        paginate([1], 0, 5)

def test_invoice_page_order_and_no_mutation():
    a = inv("A", at=datetime(2026, 1, 1, tzinfo=UTC))
    b = inv("B", at=datetime(2026, 2, 1, tzinfo=UTC))
    c = inv("C", at=datetime(2026, 2, 1, tzinfo=UTC))
    src = [a, c, b]
    p = invoice_page(src, 1, 10)
    assert [i.invoice_id for i in p.items] == ["B", "C", "A"]
    assert src == [a, c, b]

def test_revenue_by_month_tz():
    i = inv(at=datetime(2026, 2, 1, 2, tzinfo=UTC))
    assert list(revenue_by_month([i], "UTC")) == ["2026-02"]
    assert list(revenue_by_month([i], "America/New_York")) == ["2026-01"]

def test_revenue_sums_decimal():
    invs = [inv(f"I{n}", items=[item("0.10")], region="US-NY") for n in range(3)]
    r = revenue_by_month(invs)
    assert r == {"2026-01": D("0.30")} and isinstance(r["2026-01"], D)

def test_aging_buckets_boundaries():
    assert [aging_bucket(d) for d in (0, 1, 30, 31, 60, 61, 90, 91)] == \
        ["current", "1-30", "1-30", "31-60", "31-60", "61-90", "61-90", "90+"]

def test_aging_report_all_buckets():
    i = inv(at=datetime(2026, 1, 1, 12, tzinfo=UTC), net=0)
    r = aging_report([i], datetime(2026, 2, 15, tzinfo=UTC))
    assert set(r) == {"current", "1-30", "31-60", "61-90", "90+"}
    assert r["31-60"] == i.totals.total and r["current"] == D("0.00")


# ---------- properties ----------
cents = st.integers(min_value=0, max_value=10_000_00).map(lambda c: D(c) / 100)

@settings(max_examples=60, deadline=None)
@given(cents, st.integers(1, 50), st.integers(0, 100))
def test_prop_net_le_subtotal(price, qty, pct):
    li = make_line_item("S", "x", price, qty, D(pct))
    assert D("0") <= line_net(li) <= line_subtotal(li)
    assert line_net(li) == line_net(li).quantize(D("0.01"))

@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(cents, st.integers(1, 5)), min_size=1, max_size=6),
       st.sampled_from(["US-CA", "US-NY", "DE", "JP"]))
def test_prop_totals_identity(rows, region):
    items = [make_line_item(f"S{k}", "x", p, q) for k, (p, q) in enumerate(rows)]
    t = compute_totals(items, region, "4.99")
    assert t.total == t.subtotal - t.discount + t.tax + t.shipping
    assert all(isinstance(v, D) for v in (t.subtotal, t.discount, t.tax, t.shipping, t.total))

@settings(max_examples=60, deadline=None)
@given(st.integers(0, 200), st.integers(1, 25))
def test_prop_pagination_partitions(n, size):
    seq = list(range(n))
    pages = max(1, -(-n // size))
    got = [x for p in range(1, pages + 1) for x in paginate(seq, p, size).items]
    assert got == seq
