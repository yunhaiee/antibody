"""Held-out experiment: 4 arms, same issues, same seeds.

  none    – no learning
  notes   – the lessons (fix recipes) as rules-file notes in the worker's prompt
  checks  – the worker's OWN adopted checks gate its changes (blocked -> 1 retry with feedback)
  network – own + peers' adapted, gated checks from the Registry (+ rollback monitor)

none/checks/network share the SAME initial proposal per (worker, issue, seed); notes needs
its own proposals because its prompt differs. Every change is labelled by the hidden
reference suite. Metrics go to the `metrics` table and are printed.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import config
import db
import gate
import llm
import workers

ARMS = ("none", "notes", "checks", "network")


def heldout(worker):
    return [i for i in workers.load_issues(worker) if i["split"] == "heldout"]


def seen_classes() -> set:
    return {r["bug_class"] for r in db.query("SELECT DISTINCT bug_class FROM changes WHERE run_id='train' AND label='buggy'")}


def notes_for(worker) -> list[str]:
    return list(dict.fromkeys(r["text"] for r in db.query("SELECT text FROM notes WHERE worker=? ORDER BY id", worker)))


def check_ids(worker, own_only: bool) -> list[int]:
    _, ids = gate.current_set(worker)
    if not own_only:
        return ids
    return [i for i in ids if db.query("SELECT origin_worker FROM checks WHERE id=?", i)[0]["origin_worker"] == worker]


def run_checks(ids: list[int], files: dict) -> list[tuple[int, object]]:
    """Return [(check_id, result)] for checks that FLAG the change."""
    flagged = []
    for cid in ids:
        code = db.query("SELECT code FROM checks WHERE id=?", cid)[0]["code"]
        r = gate.run_many(code, [files])[0]
        if r.status != "pass":
            flagged.append((cid, r))
    return flagged


def feedback_text(flags) -> str:
    out = []
    for cid, r in flags[:2]:
        recipe = db.query("SELECT fix_recipe FROM checks WHERE id=?", cid)[0]["fix_recipe"]
        out.append(f"Check #{cid} failed:\n{r.output[-1500:]}\nFix recipe: {recipe}")
    return "\n\n".join(out)


def gated_arm(run_id, arm, worker, issue, seed, base, own_only, monitor):
    """Apply check gating to a shared initial proposal; 1 retry on block."""
    ids = check_ids(worker, own_only)
    files = json.loads(base["files"]) if base["files"] else None
    label = base["label"]
    if files is None:
        return dict(label=label, merged=False, blocked=False, retried=False, change_id=base["id"])
    if monitor:  # rollback monitor sees every new change whose reference label is known
        gate.monitor(worker, base["id"], files, label)
        ids = check_ids(worker, own_only)
    flags = run_checks(ids, files)
    if not flags:
        return dict(label=label, merged=True, blocked=False, retried=False, change_id=base["id"],
                    bug_class=base["bug_class"], false_block=False)
    false_block = label == "good"
    try:
        prop = workers.propose(worker, issue, feedback=feedback_text(flags), run_id=run_id)
    except (llm.LLMError, SyntaxError, ValueError):
        return dict(label="invalid", merged=False, blocked=True, retried=True, change_id=base["id"], false_block=false_block)
    lab = workers.reference_label(prop["files"])
    diff = workers.diff_text(prop["files"])
    bc = workers.classify_bug(diff, lab["failed_tests"], lab["trace"], run_id) if lab["label"] == "buggy" else None
    cid = db.insert("changes", run_id=run_id, arm=arm, worker=worker, issue_id=issue["id"], seed=seed, diff=diff,
                    label=lab["label"], bug_class=bc, failing_trace=lab["trace"], ref_passed=lab["n_passed"],
                    ref_failed=len(lab["failed_tests"]), files=prop["files"], failed_tests=lab["failed_tests"],
                    blocked_by=json.dumps([c for c, _ in flags]))
    still = run_checks(check_ids(worker, own_only), prop["files"])
    return dict(label=lab["label"], merged=not still, blocked=True, retried=True, change_id=cid,
                bug_class=bc, false_block=false_block)


def main(run_id: str, seeds: int):
    t_start = time.time()
    seen = seen_classes()
    jobs = [(w, iss, s) for w in config.WORKERS for iss in heldout(w) for s in range(seeds)]
    print(f"[compare] run {run_id}: {len(jobs)} (issue, seed) pairs x {len(ARMS)} arms; seen bug classes: {sorted(seen)}")

    # 1) shared proposals for none/checks/network + separate proposals for notes
    have = {(r["arm"], r["worker"], r["issue_id"], r["seed"]) for r in
            db.query("SELECT arm, worker, issue_id, seed FROM changes WHERE run_id=?", run_id)}

    def propose_job(j):
        arm, (w, iss, s) = j
        if (arm, w, iss["id"], s) in have:
            return
        notes = notes_for(w) if arm == "notes" else None
        workers.run_issue(w, iss, arm=arm, seed=s, run_id=run_id, notes=notes)
    todo = [("none", j) for j in jobs] + [("notes", j) for j in jobs]
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(propose_job, todo))
    base = {(r["worker"], r["issue_id"], r["seed"]): r for r in
            db.query("SELECT * FROM changes WHERE run_id=? AND arm='none'", run_id)}
    notes_rows = {(r["worker"], r["issue_id"], r["seed"]): r for r in
                  db.query("SELECT * FROM changes WHERE run_id=? AND arm='notes'", run_id)}

    # 2) gated arms (sequential per worker so the rollback monitor sees changes in order)
    outcomes = defaultdict(list)
    for (w, iss, s) in jobs:
        b = base[(w, iss["id"], s)]
        outcomes["none"].append(dict(label=b["label"], merged=b["label"] != "invalid", bug_class=b["bug_class"]))
        n = notes_rows[(w, iss["id"], s)]
        outcomes["notes"].append(dict(label=n["label"], merged=n["label"] != "invalid", bug_class=n["bug_class"]))

    def gated_worker(w):
        res = {"checks": [], "network": []}
        for iss in heldout(w):
            for s in range(seeds):
                b = base[(w, iss["id"], s)]
                res["checks"].append(gated_arm(run_id, "checks", w, iss, s, b, own_only=True, monitor=False))
                res["network"].append(gated_arm(run_id, "network", w, iss, s, b, own_only=False, monitor=True))
        return res
    with ThreadPoolExecutor(3) as ex:
        for res in ex.map(gated_worker, config.WORKERS):
            for arm in ("checks", "network"):
                outcomes[arm] += res[arm]

    # 3) metrics
    rows = []
    for arm in ARMS:
        o = outcomes[arm]
        n = len(o)
        merged = [x for x in o if x["merged"]]
        bugs = [x for x in merged if x["label"] == "buggy"]
        repeat = [x for x in bugs if x.get("bug_class") in seen]
        m = {"attempts": n, "merged": len(merged), "bugs_through": len(bugs), "repeat_bugs_through": len(repeat),
             "repeat_bug_rate": len(repeat) / n if n else 0, "bug_escape_rate": len(bugs) / n if n else 0,
             "good_merged_rate": sum(x["label"] == "good" for x in merged) / n if n else 0,
             "blocked": sum(x.get("blocked", False) for x in o), "false_blocks": sum(x.get("false_block", False) for x in o)}
        lo, hi = gate.wilson(m["repeat_bugs_through"], n)
        m["repeat_bug_rate_lo"], m["repeat_bug_rate_hi"] = lo, hi
        for k, v in m.items():
            db.insert("metrics", run_id=run_id, arm=arm, key=k, value=float(v))
        rows.append((arm, m))
    tok = db.query("SELECT COALESCE(SUM(input_tokens+cached_tokens+cache_write_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
                   "FROM llm_calls WHERE run_id=?", run_id)[0]
    print_tables(run_id, rows, seeds, tok, time.time() - t_start)


def version_table():
    """Held-out catch/false-alarm per check-set version, on the shared 'none' proposals.
    Each check runs once per proposal (cached), then versions are evaluated from the cache."""
    out = []
    run_id = db.query("SELECT run_id FROM metrics WHERE key='attempts' ORDER BY id DESC LIMIT 1")[0]["run_id"]
    for w in config.WORKERS:
        props = db.query("SELECT files, label FROM changes WHERE run_id=? AND arm='none' AND worker=? AND files IS NOT NULL",
                         run_id, w)
        files = [json.loads(p["files"]) for p in props]
        versions = db.query("SELECT version, check_ids, reason FROM check_sets WHERE worker=? ORDER BY version", w)
        union = sorted({c for v in versions for c in json.loads(v["check_ids"])})
        flagged = {}
        for cid in union:
            code = db.query("SELECT code FROM checks WHERE id=?", cid)[0]["code"]
            flagged[cid] = [r.status != "pass" for r in gate.run_many(code, files)]
        for v in [{"version": 0, "check_ids": "[]", "reason": "no checks"}] + versions:
            ids = json.loads(v["check_ids"])
            caught = fa = nb = ng = 0
            for k, p in enumerate(props):
                f = any(flagged[c][k] for c in ids)
                if p["label"] == "buggy":
                    nb += 1; caught += f
                else:
                    ng += 1; fa += f
            out.append((w, v["version"], v["reason"], caught, nb, fa, ng, None))
            db.insert("metrics", run_id=run_id, arm=f"{w}-v{v['version']}", key="heldout_catch", value=caught,
                      detail=json.dumps({"buggy": nb, "false_alarms": fa, "good": ng, "reason": v["reason"]}))
    return out


def print_tables(run_id, rows, seeds, tok, secs):
    print(f"\n=== Held-out comparison ({run_id}; {seeds} seeds; labels from the hidden reference suite) ===")
    print(f"{'arm':9s} {'attempts':>8s} {'merged':>7s} {'bugs thru':>9s} {'repeat-bug':>10s} {'rate (Wilson 80%)':>20s} {'blocked':>8s} {'false blk':>9s}")
    for arm, m in rows:
        print(f"{arm:9s} {m['attempts']:8d} {m['merged']:7d} {m['bugs_through']:9d} {m['repeat_bugs_through']:10d} "
              f"{m['repeat_bug_rate']:7.3f} [{m['repeat_bug_rate_lo']:.3f}-{m['repeat_bug_rate_hi']:.3f}] {m['blocked']:8d} {m['false_blocks']:9d}")
    print(f"tokens this run: {tok['i'] + tok['o']:,}  wall time: {secs / 60:.1f} min")
    le = db.query("SELECT worker, bug_class, matched_check_id, verdict, tokens_in+tokens_out t, minutes, "
                  "cf_tokens_in+cf_tokens_out cft, cf_minutes, cf_verdict FROM learn_events WHERE path='registry'")
    if le:
        print("\n=== Worker hits a bug class another worker already solved (measured) ===")
        print(f"{'worker':6s} {'class':20s} {'via Registry (adapt+gate)':>28s} {'from scratch (Writer+CEGIS+gate)':>34s}")
        for r in le:
            print(f"{r['worker']:6s} {r['bug_class']:20s} {r['t']:>14,} tok {r['minutes']:5.1f} min {r['verdict']:>7s}"
                  f"   {r['cft']:>14,} tok {r['cf_minutes']:5.1f} min {r['cf_verdict']}")


# ------------------------------------------------------------------ full system arm
def pinned_set(worker: str, version: int) -> list[int]:
    return json.loads(db.query("SELECT check_ids FROM check_sets WHERE worker=? AND version=?", worker, version)[0]["check_ids"])


def run_antibody_arm(run_id: str, seeds: int, versions: dict, enforce: bool = True):
    """antibody = network checks (pinned to the pre-experiment check-set versions, with an
    arm-local rollback monitor) + the fix recipes of those same checks as rules-file notes."""
    seen = seen_classes()
    arm_name = "antibody" if enforce else "shared_notes"

    def per_worker(w):
        ids = pinned_set(w, versions[w])
        recipes = list(dict.fromkeys(db.query(f"SELECT fix_recipe FROM checks WHERE id=?", i)[0]["fix_recipe"] for i in ids))
        fa_hist = defaultdict(list)
        out, rollbacks = [], []
        for iss in heldout(w):
            for s in range(seeds):
                r = workers.run_issue(w, iss, arm=arm_name, seed=s, run_id=run_id, notes=recipes)
                if r["label"] == "invalid":
                    out.append(dict(label="invalid", merged=False)); continue
                if not enforce:
                    out.append(dict(label=r["label"], merged=True, bug_class=r.get("bug_class")))
                    continue
                files = r["files"]
                # arm-local rollback monitor (same frozen rule as gate.monitor)
                flags = run_checks(ids, files)
                for cid in ids:
                    fa_hist[cid].append(int(r["label"] == "good" and cid in {c for c, _ in flags}))
                    last = fa_hist[cid][-config.ROLLBACK_WINDOW:]
                    if sum(last) >= config.ROLLBACK_MAX_FALSE_ALARMS and cid in ids:
                        rollbacks.append(cid)
                ids = [i for i in ids if i not in rollbacks]
                flags = [(c, x) for c, x in flags if c in ids]
                if not flags:
                    out.append(dict(label=r["label"], merged=True, bug_class=r.get("bug_class"), blocked=False, false_block=False))
                    continue
                base = db.query("SELECT * FROM changes WHERE id=?", r["change_id"])[0]
                res = gated_arm_with(run_id, "antibody", w, iss, s, base, ids, flags, recipes)
                out.append(res)
        return out, rollbacks

    outcomes = []
    rb_all = {}
    with ThreadPoolExecutor(3) as ex:
        for w, (o, rb) in zip(config.WORKERS, ex.map(per_worker, config.WORKERS)):
            outcomes += o
            rb_all[w] = rb
    n = len(outcomes)
    merged = [x for x in outcomes if x["merged"]]
    bugs = [x for x in merged if x["label"] == "buggy"]
    repeat = [x for x in bugs if x.get("bug_class") in seen]
    m = {"attempts": n, "merged": len(merged), "bugs_through": len(bugs), "repeat_bugs_through": len(repeat),
         "repeat_bug_rate": len(repeat) / n, "bug_escape_rate": len(bugs) / n,
         "good_merged_rate": sum(x["label"] == "good" for x in merged) / n,
         "blocked": sum(x.get("blocked", False) for x in outcomes),
         "false_blocks": sum(x.get("false_block", False) for x in outcomes),
         "arm_rollbacks": sum(len(v) for v in rb_all.values())}
    m["repeat_bug_rate_lo"], m["repeat_bug_rate_hi"] = gate.wilson(m["repeat_bugs_through"], n)
    for k, v in m.items():
        db.insert("metrics", run_id=run_id, arm=arm_name, key=k, value=float(v),
                  detail=json.dumps(rb_all) if k == "arm_rollbacks" else None)
    print(f"{arm_name:9s} {m['attempts']:8d} {m['merged']:7d} {m['bugs_through']:9d} {m['repeat_bugs_through']:10d} "
          f"{m['repeat_bug_rate']:7.3f} [{m['repeat_bug_rate_lo']:.3f}-{m['repeat_bug_rate_hi']:.3f}] "
          f"{m['blocked']:8d} {m['false_blocks']:9d}   arm-local rollbacks: {rb_all}")


def gated_arm_with(run_id, arm, worker, issue, seed, base, ids, flags, notes):
    false_block = base["label"] == "good"
    try:
        prop = workers.propose(worker, issue, notes=notes, feedback=feedback_text(flags), run_id=run_id)
    except (llm.LLMError, SyntaxError, ValueError):
        return dict(label="invalid", merged=False, blocked=True, false_block=false_block)
    lab = workers.reference_label(prop["files"])
    diff = workers.diff_text(prop["files"])
    bc = workers.classify_bug(diff, lab["failed_tests"], lab["trace"], run_id) if lab["label"] == "buggy" else None
    db.insert("changes", run_id=run_id, arm=arm + "-retry", worker=worker, issue_id=issue["id"], seed=seed, diff=diff,
              label=lab["label"], bug_class=bc, failing_trace=lab["trace"], ref_passed=lab["n_passed"],
              ref_failed=len(lab["failed_tests"]), files=prop["files"], failed_tests=lab["failed_tests"],
              blocked_by=json.dumps([c for c, _ in flags]))
    still = run_checks(ids, prop["files"])
    return dict(label=lab["label"], merged=not still, blocked=True, bug_class=bc, false_block=false_block)


if __name__ == "__main__":
    rid = sys.argv[1] if len(sys.argv) > 1 else "compare"
    if "--antibody" in sys.argv:
        vers = json.loads((config.ROOT / "runs" / "pre_experiment_versions.json").read_text())
        run_antibody_arm(rid, config.HELDOUT_SEEDS, vers, enforce="--no-enforce" not in sys.argv)
    elif "--versions" in sys.argv:
        print("\n=== Held-out effect per check-set version (shared 'none' proposals) ===")
        print(f"{'worker':6s} {'ver':>3s} {'caught buggy':>12s} {'flagged good':>12s}  reason")
        for w, v, reason, c, nb, fa, ng, _ in version_table():
            print(f"{w:6s} v{v:<2d} {c:>5d}/{nb:<6d} {fa:>5d}/{ng:<6d}  {reason}")
    else:
        main(rid, config.HELDOUT_SEEDS)
