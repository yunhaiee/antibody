"""Build the three issue queues (issues/A.json, B.json, C.json).

Every issue asks a worker to change ONE existing function while keeping its observable
behaviour. Correctness is decided only by the hidden reference suite. Issues are
realistic tickets (refactor / decouple from a helper / extend with an optional param /
speed up); none of them instructs a bug. The order is a deterministic shuffle; the last
third of each queue is held out for run_compare.py.
"""
import json
import random
from pathlib import Path

HERE = Path(__file__).parent

# (worker, file, function, kind, ask)
SPECS = [
    # ---------------- Worker A: money / line_items / tax ----------------
    ("A", "billing/money.py", "money_sum", "decouple",
     "Implement the per-value conversion and the final quantize inline in `money_sum` so it no longer calls `money()`."),
    ("A", "billing/money.py", "money_sum", "perf",
     "Use the built-in `sum()` instead of the explicit loop."),
    ("A", "billing/money.py", "money_sum", "extend",
     "Add an optional keyword `start` (default zero) that is added to the total."),
    ("A", "billing/money.py", "money", "extend",
     "Add an optional keyword `cents: bool = False`; when True the value is an integer number of cents (e.g. 1999 -> 19.99)."),
    ("A", "billing/line_items.py", "make_line_item", "extend",
     "Callers in the mobile app send `discount_pct` as a float such as 12.5. Accept floats for `discount_pct` as well as str/Decimal."),
    ("A", "billing/line_items.py", "make_line_item", "refactor",
     "Restructure the validation into a clear sequence of guard clauses with descriptive error messages."),
    ("A", "billing/line_items.py", "line_subtotal", "decouple",
     "Compute and round the subtotal inline so `line_subtotal` no longer calls `money()`."),
    ("A", "billing/line_items.py", "line_subtotal", "extend",
     "Add an optional `quantity: int | None = None` that overrides `item.quantity` when given."),
    ("A", "billing/line_items.py", "line_discount", "decouple",
     "Compute the discount directly from `unit_price`, `quantity` and `discount_pct` instead of calling `line_subtotal`."),
    ("A", "billing/line_items.py", "line_discount", "refactor",
     "Express the percentage as a fraction (`pct / 100`) once and make the computation easier to read."),
    ("A", "billing/line_items.py", "line_net", "decouple",
     "Compute the net amount directly from the item's fields in one expression instead of calling `line_subtotal` and `line_discount`."),
    ("A", "billing/line_items.py", "line_net", "perf",
     "Avoid computing the subtotal twice."),
    ("A", "billing/tax.py", "line_tax", "decouple",
     "Compute the taxable base from the item's fields directly so `tax.py` no longer depends on `line_net`."),
    ("A", "billing/tax.py", "line_tax", "extend",
     "Add an optional `rate: Decimal | None = None` that overrides the table rate when provided."),
    ("A", "billing/tax.py", "total_tax", "decouple",
     "Sum the line taxes without calling `money_sum`."),
    ("A", "billing/tax.py", "total_tax", "perf",
     "Look up each category's rate once instead of once per line."),
    ("A", "billing/tax.py", "tax_breakdown", "refactor",
     "Use `collections.defaultdict` to simplify the accumulation."),
    ("A", "billing/tax.py", "tax_breakdown", "extend",
     "Add `include_zero: bool = False`; when True, every category of the region appears (0.00 if absent)."),
    ("A", "billing/tax.py", "tax_breakdown", "decouple",
     "Compute each line's tax inline instead of calling `line_tax`."),
    ("A", "billing/tax.py", "tax_rate", "extend",
     "Make region lookup case-insensitive (`'us-ca'` should work)."),
    # ---------------- Worker B: totals / invoices ----------------
    ("B", "billing/totals.py", "compute_totals", "perf",
     "Make a single pass over the items instead of three separate sums."),
    ("B", "billing/totals.py", "compute_totals", "extend",
     "Add an optional `coupon` (absolute amount off the order, default '0') applied after line discounts; it must not make the taxable amount negative."),
    ("B", "billing/totals.py", "compute_totals", "decouple",
     "Compute subtotal and discount inline from the items instead of calling `line_subtotal`/`line_discount`."),
    ("B", "billing/totals.py", "compute_totals", "refactor",
     "Extract the free-shipping rule into a small local helper and simplify the function."),
    ("B", "billing/invoices.py", "issue_invoice", "extend",
     "Our CSV importer passes `issued_at` as an ISO-8601 string (e.g. '2026-03-01T09:30:00+09:00'). Accept strings as well as datetimes."),
    ("B", "billing/invoices.py", "issue_invoice", "decouple",
     "Normalise `issued_at` to UTC using `datetime.timezone.utc` instead of `ZoneInfo('UTC')`."),
    ("B", "billing/invoices.py", "issue_invoice", "refactor",
     "Move the input validation to the top in a clear order and simplify the body."),
    ("B", "billing/invoices.py", "due_date", "decouple",
     "Compute the due date inline so `due_date` no longer calls `local_issue_date`."),
    ("B", "billing/invoices.py", "due_date", "extend",
     "Add `business_days: bool = False`; when True, count only Mon-Fri. Default behaviour unchanged."),
    ("B", "billing/invoices.py", "due_date", "perf",
     "Avoid constructing a ZoneInfo object on every call (cache it)."),
    ("B", "billing/invoices.py", "days_overdue", "decouple",
     "Compute days overdue directly from `issued_at` and `net_days` instead of calling `due_date`."),
    ("B", "billing/invoices.py", "days_overdue", "extend",
     "Add `grace_days: int = 0`; an invoice is only overdue after the grace period."),
    ("B", "billing/invoices.py", "days_overdue", "refactor",
     "Simplify the function and give the intermediate values clear names."),
    ("B", "billing/invoices.py", "is_overdue", "decouple",
     "Compare `now` against the due date directly instead of calling `days_overdue`."),
    ("B", "billing/invoices.py", "is_overdue", "extend",
     "Add `grace_days: int = 0` with the same meaning as in collections tooling."),
    ("B", "billing/invoices.py", "local_issue_date", "extend",
     "Add an optional `tz: str | None` that overrides the customer's time zone."),
    ("B", "billing/invoices.py", "local_issue_date", "perf",
     "Cache ZoneInfo lookups."),
    ("B", "billing/totals.py", "compute_totals", "extend",
     "Add `shipping_region_surcharge: dict | None` mapping region -> extra shipping amount, applied before the free-shipping check."),
    ("B", "billing/invoices.py", "issue_invoice", "perf",
     "Avoid validating the time zone twice."),
    ("B", "billing/invoices.py", "is_overdue", "refactor",
     "Make the comparison explicit and readable."),
    # ---------------- Worker C: reports ----------------
    ("C", "billing/reports.py", "paginate", "decouple",
     "Compute `total_pages` with integer arithmetic so `reports.py` no longer needs `math.ceil`."),
    ("C", "billing/reports.py", "paginate", "perf",
     "Avoid copying the sequence when it is already a list."),
    ("C", "billing/reports.py", "paginate", "extend",
     "Add `max_page_size: int | None = None`; when set, larger page sizes are clamped to it."),
    ("C", "billing/reports.py", "paginate", "refactor",
     "Simplify the slicing and has_next logic."),
    ("C", "billing/reports.py", "invoice_page", "perf",
     "Sort once with a single composite key instead of two sorts."),
    ("C", "billing/reports.py", "invoice_page", "decouple",
     "Do the slicing inline instead of calling `paginate`, returning the same Page."),
    ("C", "billing/reports.py", "invoice_page", "extend",
     "Add `newest_first: bool = True`; when False, oldest first (ties still by invoice_id ascending)."),
    ("C", "billing/reports.py", "invoice_page", "refactor",
     "Make the ordering rule obvious from the code."),
    ("C", "billing/reports.py", "revenue_by_month", "decouple",
     "Accumulate the monthly totals inline without calling `money()`."),
    ("C", "billing/reports.py", "revenue_by_month", "perf",
     "Accumulate with a dict.get-free approach (defaultdict) and round once per month at the end."),
    ("C", "billing/reports.py", "revenue_by_month", "extend",
     "Add `region: str | None = None` to restrict the report to one region."),
    ("C", "billing/reports.py", "revenue_by_month", "refactor",
     "Simplify the month-key computation."),
    ("C", "billing/reports.py", "aging_bucket", "refactor",
     "Make the bucket edges table-driven."),
    ("C", "billing/reports.py", "aging_bucket", "extend",
     "Add `edges: tuple = (30, 60, 90)` so finance can configure the bucket edges; default behaviour unchanged."),
    ("C", "billing/reports.py", "aging_report", "decouple",
     "Sum each bucket without calling `money_sum`."),
    ("C", "billing/reports.py", "aging_report", "perf",
     "Accumulate bucket totals in a single pass without building intermediate lists."),
    ("C", "billing/reports.py", "aging_report", "extend",
     "Add `only_overdue: bool = False`; when True, omit the 'current' bucket from the result."),
    ("C", "billing/reports.py", "aging_report", "refactor",
     "Simplify the function."),
    ("C", "billing/reports.py", "revenue_by_month", "extend",
     "Add `customer_id: str | None = None` to restrict the report to one customer."),
    ("C", "billing/reports.py", "paginate", "extend",
     "Accept any iterable (including generators) for `seq`."),
]

KIND_PREFIX = {
    "decouple": "Decouple",
    "perf": "Performance",
    "extend": "Feature",
    "refactor": "Refactor",
}


def build(seed: int = 7):
    rng = random.Random(seed)
    for w in "ABC":
        rows = [s for s in SPECS if s[0] == w]
        rng.shuffle(rows)
        issues = []
        for i, (_, file, fn, kind, ask) in enumerate(rows, 1):
            issues.append({
                "id": f"{w}-{i:02d}",
                "worker": w,
                "file": file,
                "function": fn,
                "kind": kind,
                "title": f"{KIND_PREFIX[kind]}: `{fn}`",
                "body": f"{ask} Existing behaviour and all current callers must keep working.",
            })
        n_hold = round(len(issues) / 3)
        for k, iss in enumerate(issues):
            iss["split"] = "heldout" if k >= len(issues) - n_hold else "train"
        (HERE / f"{w}.json").write_text(json.dumps(issues, indent=2) + "\n")
        print(w, len(issues), "issues,", n_hold, "held out")


if __name__ == "__main__":
    build()
