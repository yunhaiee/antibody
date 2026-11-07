"""The sandbox must reject or contain malicious / misbehaving checks."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
import sandbox

GOOD = '''
from decimal import Decimal
from billing.line_items import make_line_item, line_subtotal
def test_decimal():
    assert isinstance(line_subtotal(make_line_item("S", "x", "0.10", 3)), Decimal)
'''


@pytest.fixture(scope="module")
def repo():
    r = sandbox.materialize()
    yield r
    sandbox.cleanup(r)


@pytest.mark.parametrize("code,why", [
    ("import os\ndef test_x(): os.system('echo hi')", "banned import: os"),
    ("import subprocess\ndef test_x(): pass", "banned import: subprocess"),
    ("from socket import socket\ndef test_x(): pass", "banned import: socket"),
    ("import shutil\ndef test_x(): pass", "banned import: shutil"),
    ("import requests\ndef test_x(): pass", "banned import: requests"),
    ("def test_x(): open('/tmp/pwn', 'w')", "banned call: open()"),
    ("def test_x(): __import__('os')", "banned call: __import__()"),
    ("def test_x(): ().__class__.__bases__[0].__subclasses__()", "banned dunder"),
    ("def test_x(): getattr(object, 'x')", "banned call: getattr()"),
])
def test_static_rejects(code, why, repo):
    r = sandbox.run_check(code, repo)
    assert r.status == "rejected" and why in r.reason


def test_good_check_passes(repo):
    r = sandbox.run_check(GOOD, repo)
    assert r.status == "pass", r.output
    assert r.safe


def test_check_that_flags_bug(repo):
    buggy = sandbox.materialize({"billing/line_items.py":
        (repo / "billing/line_items.py").read_text().replace(
            "return money(item.unit_price * item.quantity)",
            "return float(item.unit_price) * item.quantity")})
    try:
        r = sandbox.run_check(GOOD, buggy)
        assert r.status == "fail" and r.flagged
    finally:
        sandbox.cleanup(buggy)


def test_timeout_killed(repo):
    r = sandbox.run_check("def test_spin():\n    while True:\n        pass\n", repo, timeout=2)
    assert r.status == "timeout" and not r.safe
    assert r.runtime_s < 4


def test_runtime_write_blocked_even_if_static_screen_missed(repo):
    # io.open is not the bare name `open`, so the static screen lets it through;
    # the audit hook + RLIMIT_FSIZE must stop it.
    code = "import io\ndef test_w():\n    io.open('pwned.txt', 'w').write('x')\n"
    r = sandbox.run_check(code, repo)
    assert r.status in ("error", "fail") and not r.safe or r.violations, r.output
    assert any("file write" in v for v in r.violations)


def test_runtime_network_blocked(repo):
    code = ("import asyncio\n"
            "def test_net():\n"
            "    asyncio.run(asyncio.open_connection('example.com', 80))\n")
    r = sandbox.run_check(code, repo)
    assert r.violations and not r.safe, r.output


def test_repo_copy_is_read_only(repo):
    code = ("import billing.money as m, io\n"
            "def test_mut():\n"
            "    io.open(m.__file__, 'a').write('#')\n")
    r = sandbox.run_check(code, repo)
    assert not r.safe
    assert "#" not in (repo / "billing/money.py").read_text()[-3:]
