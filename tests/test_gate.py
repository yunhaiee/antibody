"""The gate must reject a planted always-pass check and adopt a real one.
Offline: hand-built ground-truth material, temp DB, no LLM calls."""
import sys, pathlib, importlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

REPORTS = pathlib.Path(__file__).resolve().parents[1] / "repos/billing/billing/reports.py"
SRC = REPORTS.read_text()
GOOD_SORT = '''    ordered = sorted(invoices, key=lambda i: i.invoice_id)
    ordered = sorted(ordered, key=lambda i: i.issued_at, reverse=True)'''

# Buggy variants of the "ordering" class (each confirmed below by the reference suite).
BUGGY_SORTS = [
    "    ordered = sorted(invoices, key=lambda i: i.issued_at, reverse=True)",
    "    ordered = sorted(invoices, key=lambda i: i.issued_at)",
    "    ordered = sorted(invoices, key=lambda i: (i.issued_at, i.invoice_id), reverse=True)",
    "    ordered = list(invoices)",
    "    ordered = sorted(invoices, key=lambda i: i.invoice_id)",
    "    ordered = sorted(invoices, key=lambda i: i.invoice_id, reverse=True)",
]
# Correct look-alikes.
GOOD_SORTS = [
    "    ordered = sorted(invoices, key=lambda i: (-i.issued_at.timestamp(), i.invoice_id))",
    "    ordered = sorted(sorted(invoices, key=lambda i: i.invoice_id), key=lambda i: i.issued_at, reverse=True)",
    GOOD_SORT,
]

ALWAYS_PASS = "def test_ok():\n    assert True\n"
REAL_CHECK = '''
from datetime import datetime, timezone
from billing.line_items import make_line_item
from billing.invoices import issue_invoice
from billing.reports import invoice_page

def mk(i, day):
    return issue_invoice(i, "c", [make_line_item("S", "x", "1.00", 1)], "US-NY",
                         datetime(2026, 1, day, tzinfo=timezone.utc))

def test_newest_first_ties_by_id():
    invs = [mk("B", 2), mk("A", 1), mk("D", 2), mk("C", 2)]
    for src in (invs, list(reversed(invs))):
        assert [i.invoice_id for i in invoice_page(src, 1, 10).items] == ["B", "C", "D", "A"]
'''


@pytest.fixture()
def gate_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTIBODY_DB", str(tmp_path / "t.db"))
    import config; importlib.reload(config)
    import db; importlib.reload(db)
    import workers, gate; importlib.reload(workers); importlib.reload(gate)
    variants = [{"billing/reports.py": SRC.replace(GOOD_SORT, b)} for b in BUGGY_SORTS]
    goods = [{"billing/reports.py": SRC.replace(GOOD_SORT, g)} for g in GOOD_SORTS] + [{}] * 17
    # sanity: ground truth comes from the reference suite
    assert all(workers.reference_label(v)["label"] == "buggy" for v in variants)
    assert all(workers.reference_label(g)["label"] == "good" for g in goods[:3])
    monkeypatch.setattr(gate, "seed_variants", lambda w, c, **k: variants)
    monkeypatch.setattr(gate, "known_good", lambda w, **k: goods)
    return gate, db


def _add_check(db, code):
    return db.insert("checks", hash=str(hash(code)), origin_worker="C", owner_worker="C",
                     bug_class="ordering", signature={}, kind="pytest", code=code, fix_recipe="",
                     cegis_rounds=0, evidence={})


def test_always_pass_check_is_rejected(gate_mod):
    gate, db = gate_mod
    cid = _add_check(db, ALWAYS_PASS)
    v = gate.evaluate(cid, "C", original={"buggy": {"billing/reports.py": SRC.replace(GOOD_SORT, BUGGY_SORTS[0])}})
    assert v["verdict"] == "REJECT"
    assert v["catches_original"] == 0 and v["variants_caught"] == 0
    assert gate.current_set("C") == (0, [])


def test_check_that_imports_os_is_rejected(gate_mod):
    gate, db = gate_mod
    cid = _add_check(db, "import os\n" + REAL_CHECK)
    v = gate.evaluate(cid, "C")
    assert v["verdict"] == "REJECT" and "static screen" in v["reason"]


def test_real_check_is_adopted_as_new_version(gate_mod):
    gate, db = gate_mod
    cid = _add_check(db, REAL_CHECK)
    v = gate.evaluate(cid, "C", original={"buggy": {"billing/reports.py": SRC.replace(GOOD_SORT, BUGGY_SORTS[0])}})
    assert v["verdict"] == "ADOPT", v
    assert v["variants_caught"] >= 5 and v["goods_flagged"] == 0
    assert gate.current_set("C") == (1, [cid])


def test_wilson_bounds():
    import gate
    lo, hi = gate.wilson(8, 8)
    assert 0.8 < lo < 0.9 and hi == 1.0
    assert gate.wilson(0, 0) == (0.0, 1.0)
