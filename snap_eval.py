"""Registry matching: keyword signatures vs SubQ Snap 1.1, on every real held-out failure
(arm 'none'). Reference = the bug class the classifier gave the failure. A lookup is right if
its top match has that class, or if it returns nothing when no proven check has that class."""
import json

import db
import registry_core
import signature as sigmod
import workers

rows = db.query("SELECT * FROM changes WHERE run_id='compare' AND arm='none' AND label='buggy' ORDER BY id")
proven = {r["bug_class"] for r in db.query("SELECT id, bug_class FROM checks WHERE parent_id IS NULL")
          if registry_core.is_proven(r["id"])}
res = {"keyword": 0, "snap": 0}
for ch in rows:
    tests, trace = workers.visible_failure(json.loads(ch["failed_tests"] or "[]"), ch["failing_trace"])
    issue = next(i for i in workers.load_issues(ch["worker"]) if i["id"] == ch["issue_id"])
    sig = sigmod.build(trace, tests, [], None)          # no class given: match on evidence only
    kw = registry_core.lookup(sig, worker=ch["worker"], method="keyword", min_score=0.2)
    text = f"Ticket: {issue['title']} {issue['body']}\nDiff:\n{ch['diff'][:3000]}\nCI:\n{trace}"
    sn = registry_core.snap_lookup(text, worker=ch["worker"])
    want = ch["bug_class"]
    for name, m in (("keyword", kw), ("snap", sn)):
        ok = (m[0]["bug_class"] == want) if m else (want not in proven)
        res[name] += ok
    print(f"{ch['issue_id']:5s} {want:14s} keyword->{kw[0]['bug_class'] if kw else '-':14s} snap->{sn[0]['bug_class'] if sn else '-'}")
n = len(rows)
for k, v in res.items():
    db.insert("metrics", run_id="snap_eval", arm=k, key="match_accuracy", value=v / n, detail=json.dumps({"right": v, "n": n}))
print(f"match accuracy on {n} held-out failures: keyword {res['keyword']}/{n}, SubQ Snap {res['snap']}/{n}")
