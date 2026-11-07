"""Demo beat 3, measured: a worker hits a bug class that ANOTHER worker already solved.

For real held-out failures (arm 'none' of a run_compare run) whose bug class has a proven
check from a different origin worker, run BOTH paths and log tokens + minutes:
  with Registry : signature lookup -> adapt -> gate (this worker's material) -> retry with recipe
  from scratch  : Antibody Writer (CEGIS) -> gate -> retry with its own recipe
Each retry is labelled by the hidden reference suite ("fix applied" = reference-good).
"""
from __future__ import annotations

import json
import sys
import time

import db
import gate
import learn
import registry_core
import signature as sigmod
import workers
import writer

db.connect().executescript("""
CREATE TABLE IF NOT EXISTS reuse_runs (
  id INTEGER PRIMARY KEY, ts REAL, run_id TEXT, worker TEXT, change_id INT, bug_class TEXT, path TEXT,
  matched_check_id INT, origin_worker TEXT, verdict TEXT, fix_label TEXT, tokens INT, minutes REAL);
""")


def retry_with(worker, issue_id, recipe, check_out, rid):
    issue = next(i for i in workers.load_issues(worker) if i["id"] == issue_id)
    prop = workers.propose(worker, issue, feedback=f"{check_out}\nFix recipe: {recipe}", run_id=rid)
    return workers.reference_label(prop["files"])["label"]


def run(run_id: str, limit: int = 3):
    rows = db.query("SELECT * FROM changes WHERE run_id=? AND arm='none' AND label='buggy' ORDER BY id", run_id)
    cases = []
    for ch in rows:
        sig = sigmod.build(ch["failing_trace"], json.loads(ch["failed_tests"] or "[]"), [], ch["bug_class"])
        m = [x for x in registry_core.lookup(sig, worker=ch["worker"], method="keyword")
             if x["origin_worker"] != ch["worker"] and x["bug_class"] == ch["bug_class"]]
        if m and (ch["worker"], ch["bug_class"]) not in {(c[0]["worker"], c[0]["bug_class"]) for c in cases}:
            cases.append((ch, sig, m[0]))
    print(f"[reuse] {len(cases)} held-out failures whose bug class another worker already solved")
    for ch, sig, m in cases[:limit]:
        w = ch["worker"]
        print(f"\n[reuse] worker {w} {ch['issue_id']} ({ch['bug_class']}): Registry match check #{m['check_id']} "
              f"from worker {m['origin_worker']} (score {m['score']})")
        # --- with Registry
        rid = f"reuse-{run_id}-{ch['id']}-registry"
        t0 = time.monotonic()
        a = registry_core.adapt(m["check_id"], w, run_id=rid)
        if a["not_applicable"]:
            v = {"verdict": "NOT_APPLICABLE"}
            out = ""
        else:
            v = gate.evaluate(a["check_id"], w, run_id=rid, adopt=False, record=False)
            res = gate.run_many(db.query("SELECT code FROM checks WHERE id=?", a["check_id"])[0]["code"],
                                [json.loads(ch["files"])])[0]
            out = res.output[-1500:]
        fix = retry_with(w, ch["issue_id"], m["fix_recipe"], out, rid)
        ti, to = learn.cost(rid)
        mins = (time.monotonic() - t0) / 60
        db.insert("reuse_runs", run_id=run_id, worker=w, change_id=ch["id"], bug_class=ch["bug_class"], path="registry",
                  matched_check_id=m["check_id"], origin_worker=m["origin_worker"], verdict=v["verdict"],
                  fix_label=fix, tokens=ti + to, minutes=round(mins, 2))
        print(f"  with Registry : adapt+gate {v['verdict']}, fix {fix}  — {ti + to:,} tokens, {mins:.1f} min")
        # --- from scratch
        rid2 = f"reuse-{run_id}-{ch['id']}-scratch"
        t1 = time.monotonic()
        wc = writer.write_check(ch["id"], run_id=rid2, log=lambda *a: None)
        with db.session() as c:
            c.execute("UPDATE checks SET kind='counterfactual', parent_id=-1 WHERE id=?", (wc["check_id"],))
        v2 = gate.evaluate(wc["check_id"], w, original=wc["original"], run_id=rid2, adopt=False, record=False)
        res2 = gate.run_many(wc["code"], [json.loads(ch["files"])])[0]
        fix2 = retry_with(w, ch["issue_id"], wc["fix_recipe"], res2.output[-1500:], rid2)
        ti2, to2 = learn.cost(rid2)
        mins2 = (time.monotonic() - t1) / 60
        db.insert("reuse_runs", run_id=run_id, worker=w, change_id=ch["id"], bug_class=ch["bug_class"], path="scratch",
                  verdict=v2["verdict"], fix_label=fix2, tokens=ti2 + to2, minutes=round(mins2, 2))
        print(f"  from scratch  : Writer+CEGIS+gate {v2['verdict']}, fix {fix2}  — {ti2 + to2:,} tokens, {mins2:.1f} min")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "compare")
