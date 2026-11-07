"""Antibody — 3-minute demo. Prints the five beats straight from the SQLite run log.
Nothing here is computed for show: every number was produced by learn.py / gate.py /
run_compare.py / reuse.py and is read back from antibody.db.

  python demo.py            # all beats
  python demo.py 3          # one beat
  python demo.py 5 --live   # beat 5 through the real Registry code path (Claude router + DB)
"""
from __future__ import annotations

import json
import sys
import textwrap

import config
import db

W = 96


def h(title):
    print("\n" + "━" * W + f"\n  {title}\n" + "━" * W)


def code_block(code, n=28):
    lines = code.strip().splitlines()
    for l in lines[:n]:
        print("   │ " + l[: W - 6])
    if len(lines) > n:
        print(f"   │ … ({len(lines) - n} more lines)")


def verdict_line(v):
    if v["verdict"] in ("NOT_APPLICABLE",):
        return f"{v['worker']}: NOT_APPLICABLE — {textwrap.shorten(v['reason'], 70)}"
    if v["verdict"] == "ROLLBACK":
        return f"{v['worker']}: ROLLBACK — {v['reason']}"
    orig = {1: "caught", 0: "MISSED", None: "n/a"}[v["catches_original"]]
    return (f"{v['worker']}: {v['verdict']:7s} original {orig:6s} | variants {v['variants_caught']}/{v['variants_total']} "
            f"(Wilson80 {v['catch_lo']:.2f}–{v['catch_hi']:.2f}) | known-good flagged {v['goods_flagged']}/{v['goods_total']} "
            f"(FA {v['fa_lo']:.2f}–{v['fa_hi']:.2f}) | {v['runtime_s']}s")


def beat1():
    h("1 · A failure becomes a check → gate → ADOPT → new check-set version")
    ev = db.query("SELECT * FROM learn_events WHERE path='writer' AND verdict='ADOPT' ORDER BY id LIMIT 1")
    if not ev:
        return print("  (no adopted check yet — run learn.py)")
    ev = ev[0]
    ch = db.query("SELECT * FROM changes WHERE id=?", ev["change_id"])[0]
    chk = db.query("SELECT * FROM checks WHERE id=?", ev["check_id"])[0]
    issue = next(i for i in json.load(open(config.ISSUES_DIR / f"{ch['worker']}.json")) if i["id"] == ch["issue_id"])
    print(f"  Worker {ch['worker']} · ticket {ch['issue_id']} “{issue['title']}” — {issue['body']}")
    print(f"  Hidden reference suite: {ch['ref_failed']} failing → {json.loads(ch['failed_tests'])}")
    print("  CI trace:")
    for l in (ch["failing_trace"] or "").splitlines()[:4]:
        print("   ! " + l[: W - 6])
    print(f"\n  Antibody Writer → check #{chk['id']} ({chk['kind']}, bug class {chk['bug_class']}, sha {chk['hash']}):")
    code_block(chk["code"])
    print("\n  CEGIS rounds (Writer's own variants / look-alikes, confirmed by the reference suite):")
    for r in json.loads(chk["evidence"])["cegis"]:
        print(f"   r{r['round']}: original {'caught' if r['catches_original'] else 'MISSED'}, variants "
              f"{r['variants_caught']}/{r['variants_total']}, look-alikes flagged {r['lookalikes_flagged']}/{r['lookalikes_total']}")
    v = db.query("SELECT * FROM verdicts WHERE check_id=? AND worker=? ORDER BY id LIMIT 1", chk["id"], ch["worker"])[0]
    print("\n  Adoption gate on the worker's OWN material (independent seeded variants + real known-good changes):")
    print("   " + verdict_line(v))
    print(f"   → Worker {ch['worker']} check set v{v['check_set_version']}   ·   fix recipe: {textwrap.shorten(chk['fix_recipe'], 120)}")
    print(f"   cost: {ev['tokens_in'] + ev['tokens_out']:,} tokens, {ev['minutes']} min (from llm_calls)")


def beat2():
    h("2 · Not every check gets in: REJECT, NOT_APPLICABLE, ROLLBACK")
    rej = db.query("SELECT v.*, c.bug_class, c.origin_worker, c.kind FROM verdicts v JOIN checks c ON c.id=v.check_id "
                   "WHERE verdict='REJECT' ORDER BY (c.kind LIKE 'ablation%'), v.id LIMIT 2")
    for v in rej:
        what = "single-round draft, no CEGIS (ablation)" if v["kind"].startswith("ablation") else v["kind"]
        print(f"  REJECT  check #{v['check_id']} ({v['bug_class']}, {what}, from worker {v['origin_worker']}) for worker {v['worker']}:")
        print("   " + verdict_line(v))
        print(f"   reason: {v['reason']}")
    if not rej:
        print("  REJECT: none in this run (see tests/test_gate.py: a planted always-pass check is rejected 0/6).")
    na = db.query("SELECT v.*, c.bug_class FROM verdicts v JOIN checks c ON c.id=v.check_id WHERE verdict='NOT_APPLICABLE' ORDER BY v.id LIMIT 2")
    for v in na:
        print(f"  NOT_APPLICABLE  check #{v['check_id']} ({v['bug_class']}) for worker {v['worker']}: {textwrap.shorten(v['reason'], 150)}")
    rb = db.query("SELECT * FROM verdicts WHERE verdict='ROLLBACK' ORDER BY id")
    for v in rb:
        mon = db.query("SELECT COUNT(*) n, SUM(false_alarm) fa FROM monitor_runs WHERE check_id=? AND worker=?", v["check_id"], v["worker"])[0]
        print(f"  ROLLBACK  check #{v['check_id']} on worker {v['worker']}: {v['reason']} "
              f"(monitored {mon['n']} new changes) → check set v{v['check_set_version']}")
    if not rb:
        mon = db.query("SELECT COUNT(*) n, COALESCE(SUM(false_alarm),0) fa FROM monitor_runs")[0]
        print(f"  ROLLBACK: none triggered — monitor ran adopted checks on {mon['n']} new changes, {mon['fa']} false alarms "
              f"(rule: ≥{config.ROLLBACK_MAX_FALSE_ALARMS} in last {config.ROLLBACK_WINDOW} → roll back one version).")


def beat3():
    h("3 · Worker hits a bug another worker already solved → Registry → fix applied")
    rows = db.query("SELECT * FROM reuse_runs ORDER BY id")
    if not rows:
        rows = []
    pairs = {}
    for r in rows:
        pairs.setdefault(r["change_id"], {})[r["path"]] = r
    if not pairs:
        le = db.query("SELECT * FROM learn_events WHERE path='registry'")
        if not le:
            return print("  (no reuse measured yet — run reuse.py)")
    for cid, p in pairs.items():
        reg, scr = p.get("registry"), p.get("scratch")
        if not reg or not scr:
            continue
        ch = db.query("SELECT worker, issue_id FROM changes WHERE id=?", cid)[0]
        print(f"  Worker {ch['worker']} {ch['issue_id']}: {reg['bug_class']} bug. Asks Registry → check #{reg['matched_check_id']} "
              f"from Worker {reg['origin_worker']}")
        print(f"   with Registry : adapt + gate {reg['verdict']:7s} → fix {reg['fix_label']:5s}  {reg['tokens']:>9,} tokens  {reg['minutes']:5.1f} min")
        print(f"   from scratch  : Writer+CEGIS+gate {scr['verdict']:7s} → fix {scr['fix_label']:5s}  {scr['tokens']:>9,} tokens  {scr['minutes']:5.1f} min")
        if scr["tokens"]:
            print(f"   saved: {scr['tokens'] - reg['tokens']:,} tokens ({1 - reg['tokens'] / scr['tokens']:.0%}), "
                  f"{scr['minutes'] - reg['minutes']:.1f} min")
    lk = db.query("SELECT COUNT(*) n, SUM(matched_check_id IS NOT NULL) m FROM lookups")[0]
    print(f"  Registry lookups logged: {lk['n']} ({lk['m'] or 0} matched)")


def beat4():
    h("4 · Does it get better? (labels from the hidden reference suite)")
    rec = {}
    for r in db.query("SELECT arm, key, value FROM metrics WHERE run_id='recurrence'"):
        rec.setdefault(r["arm"], {})[r["key"]] = r["value"]
    if "recurrence" in rec:
        r0, rn = rec["recurrence"], rec.get("recurrence-notes", {})
        print(f"  Same tickets that failed in training, fresh seeds ({int(r0['attempts'])} attempts):")
        print(f"   no learning      {int(r0['buggy']):>3d}/{int(r0['attempts'])} merge buggy")
        if rn:
            print(f"   notes in prompt  {int(rn['buggy']):>3d}/{int(rn['attempts'])} merge buggy")
        print(f"   checks           {int(r0['buggy']) - int(r0['buggy_blocked']):>3d}/{int(r0['attempts'])} merge buggy "
              f"({int(r0['buggy_blocked'])} blocked, {int(r0['good_flagged'])} false blocks)\n")
    run = db.query("SELECT run_id FROM metrics WHERE key='attempts' AND arm='none' ORDER BY id DESC LIMIT 1")
    if not run:
        return print("  (run run_compare.py first)")
    run = run[0]["run_id"]
    m = {}
    for r in db.query("SELECT arm, key, value FROM metrics WHERE run_id=?", run):
        m.setdefault(r["arm"], {})[r["key"]] = r["value"]
    print("  New code — held-out tickets implementing functions that did not exist during learning:")
    print(f"  run {run}: held-out tickets × {config.HELDOUT_SEEDS} seeds; repeat bug = buggy merged change of a class seen in training")
    print(f"  {'arm':9s} {'attempts':>8s} {'bugs thru':>9s} {'repeat bugs':>11s} {'repeat-bug rate (Wilson80)':>28s} {'good merged':>11s} {'blocked':>7s} {'false blk':>9s}")
    for arm in ("none", "notes", "shared_notes", "checks", "network", "antibody", "antibody+instances"):
        x = m.get(arm)
        if not x:
            continue
        print(f"  {arm[:9]:9s} {int(x['attempts']):8d} {int(x['bugs_through']):9d} {int(x['repeat_bugs_through']):11d} "
              f"{x['repeat_bug_rate']:10.3f} [{x['repeat_bug_rate_lo']:.3f}–{x['repeat_bug_rate_hi']:.3f}] "
              f"{x['good_merged_rate']:11.2%} {int(x['blocked']):7d} {int(x['false_blocks']):9d}")
    print("  none=no learning · notes=lessons in prompt · checks=own checks gate merges · network=+peers' adapted checks"
          " · shared_notes=the same recipes as antibody, no checks (control) · antibody=network checks + their fix recipes")
    vers = [r for r in db.query("SELECT arm, value, detail FROM metrics WHERE run_id=? AND key='heldout_catch' ORDER BY id", run)]
    if vers:
        print("\n  Held-out buggy changes caught per check-set version (same 'none' proposals):")
        for r in vers:
            d = json.loads(r["detail"])
            print(f"   {r['arm']:6s} caught {int(r['value'])}/{d['buggy']} buggy · flagged {d['false_alarms']}/{d['good']} good")


def beat5(live=False):
    q = "Is there a proven check for money amounts handled as float instead of Decimal?"
    h(f"5 · ASI:One → “{q}”")
    if live:
        sys.path.insert(0, str(config.ROOT / "agents"))
        from agents.registry import handle_text
        print(handle_text(q))
    else:
        rows = db.query("SELECT output FROM llm_calls WHERE 0")  # placeholder to keep DB-only path explicit
        print(f"  Ask in ASI:One:  @<registry handle> {q}")
        addr = (config.ROOT / "runs" / "registry_address.txt")
        if addr.exists():
            print(f"  Registry agent address: {addr.read_text().strip()}")
        print("  (python demo.py 5 --live runs the same code path locally)")


BEATS = {"1": beat1, "2": beat2, "3": beat3, "4": beat4, "5": beat5}

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for b in (args or list(BEATS)):
        if b == "5":
            beat5(live="--live" in sys.argv)
        else:
            BEATS[b]()
    print()
