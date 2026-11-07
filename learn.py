"""Training + learning phase.

1. Workers A, B, C work through their TRAIN issues (2 seeds). The hidden reference suite
   labels every change.
2. Every distinct failure becomes a learning event, in queue order:
     - the worker first asks the Registry for a PROVEN check matching the bug signature;
       on a match it adapts that check to its own module and gates the adapted version;
     - otherwise the Antibody Writer writes a new check (CEGIS) and the origin worker gates it;
       an ADOPTed check is then offered to every other worker (adapt -> gate on THEIR material).
   For every Registry hit we ALSO run the Writer from scratch (counterfactual) so the
   tokens/minutes saved are measured, not estimated.
3. Every failure also yields a NOTE (the lesson as a rules-file line) for the NOTES arm.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import config
import db
import gate
import llm
import registry_core
import signature as sigmod
import workers
import writer

RUN = "train"


def cost(run_id: str) -> tuple[int, int]:
    r = db.query("SELECT COALESCE(SUM(input_tokens + cached_tokens + cache_write_tokens),0) i, "
                 "COALESCE(SUM(output_tokens),0) o FROM llm_calls WHERE run_id=?", run_id)[0]
    return r["i"], r["o"]


def train_changes(seeds=(0, 1)):
    jobs = [(w, iss, s) for s in seeds for w in config.WORKERS
            for iss in workers.load_issues(w) if iss["split"] == "train"]
    done = {(r["worker"], r["issue_id"], r["seed"]) for r in
            db.query("SELECT worker, issue_id, seed FROM changes WHERE run_id=?", RUN)}
    jobs = [j for j in jobs if (j[0], j[1]["id"], j[2]) not in done]
    print(f"[train] {len(jobs)} worker changes to generate")

    def one(j):
        w, iss, s = j
        r = workers.run_issue(w, iss, arm="train", seed=s, run_id=RUN)
        print(f"[train] {w} {iss['id']} seed{s} {iss['kind']:9s} {iss['function']:18s} -> {r['label']} {r.get('bug_class') or ''}")
        return r
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, jobs))


def learning_events():
    rows = db.query("SELECT id, worker, issue_id, seed, bug_class FROM changes WHERE run_id=? AND label='buggy' "
                    "ORDER BY seed, CAST(substr(issue_id, 3) AS INT), worker", RUN)
    seen, out = set(), []
    for r in rows:
        key = (r["worker"], r["issue_id"], r["bug_class"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def broadcast(check_id: int, origin: str):
    others = [w for w in config.WORKERS if w != origin]
    with ThreadPoolExecutor(len(others)) as ex:
        return list(ex.map(lambda w: registry_core.adopt_from_registry(check_id, w, run_id=f"bcast-{check_id}-{w}"), others))


def learn_one(ev: dict):
    cid, w = ev["id"], ev["worker"]
    ch = db.query("SELECT * FROM changes WHERE id=?", cid)[0]
    sig = sigmod.build(ch["failing_trace"], json.loads(ch["failed_tests"] or "[]"), [], ch["bug_class"])
    print(f"\n=== learning event: worker {w} {ch['issue_id']} change #{cid} class={ch['bug_class']}")
    _, own_ids = gate.current_set(w)
    own_classes = {r["bug_class"] for r in db.query(
        f"SELECT bug_class FROM checks WHERE id IN ({','.join('?' * len(own_ids)) or 'NULL'})", *own_ids)}
    if ch["bug_class"] in own_classes:
        print(f"[learn] worker {w} already has an adopted check for {ch['bug_class']}; note only")
    matches = registry_core.lookup(sig, exclude_owner=w, worker=w)
    t0 = time.monotonic()
    if matches and ch["bug_class"] not in own_classes:
        m = matches[0]
        print(f"[registry] worker {w} asked for signature {sig['bug_class']}/{sig['error_type']} -> "
              f"match check #{m['check_id']} from worker {m['origin_worker']} (score {m['score']})")
        rid = f"learn-{cid}-registry"
        v = registry_core.adopt_from_registry(m["check_id"], w, run_id=rid)
        ti, to = cost(rid)
        minutes = (time.monotonic() - t0) / 60
        # counterfactual: what would writing it from scratch have cost?
        t1 = time.monotonic()
        cf_rid = f"learn-{cid}-counterfactual"
        cf = writer.write_check(cid, run_id=cf_rid, log=lambda *a: None)
        with db.session() as c:  # counterfactual checks never enter the Registry
            c.execute("UPDATE checks SET kind='counterfactual', parent_id=-1 WHERE id=?", (cf["check_id"],))
        cfv = gate.evaluate(cf["check_id"], w, original=cf["original"], run_id=cf_rid, adopt=False, record=False)
        cfi, cfo = cost(cf_rid)
        cf_minutes = (time.monotonic() - t1) / 60
        print(f"[tokens] registry path: {ti + to:,} tokens, {minutes:.1f} min  vs  from scratch: "
              f"{cfi + cfo:,} tokens, {cf_minutes:.1f} min (counterfactual verdict {cfv['verdict']})")
        db.insert("learn_events", worker=w, change_id=cid, bug_class=ch["bug_class"], path="registry",
                  check_id=v.get("adapted_check_id"), matched_check_id=m["check_id"], verdict=v["verdict"],
                  tokens_in=ti, tokens_out=to, minutes=round(minutes, 2), cf_tokens_in=cfi, cf_tokens_out=cfo,
                  cf_minutes=round(cf_minutes, 2), cf_verdict=cfv["verdict"])
        recipe = m["fix_recipe"]
        if v["verdict"] == "ADOPT":
            db.insert("notes", worker=w, change_id=cid, bug_class=ch["bug_class"], text=recipe)
            return
    if ch["bug_class"] in own_classes:
        recipe = db.query("SELECT fix_recipe FROM checks WHERE owner_worker=? AND bug_class=? ORDER BY id DESC LIMIT 1",
                          w, ch["bug_class"])[0]["fix_recipe"]
        db.insert("notes", worker=w, change_id=cid, bug_class=ch["bug_class"], text=recipe)
        return
    rid = f"learn-{cid}-writer"
    out = writer.write_check(cid, run_id=rid)
    v = gate.evaluate(out["check_id"], w, original=out["original"], run_id=rid)
    ti, to = cost(rid)
    print(f"[gate] worker {w} check #{out['check_id']}: {v['verdict']} — original "
          f"{'caught' if v.get('catches_original') else 'missed'}, variants {v['variants_caught']}/{v['variants_total']} "
          f"(Wilson {v['catch_lo']:.2f}-{v['catch_hi']:.2f}), known-good flagged {v['goods_flagged']}/{v['goods_total']} "
          f"-> check-set v{v['check_set_version'] or gate.current_set(w)[0]}  [{ti + to:,} tokens]")
    db.insert("learn_events", worker=w, change_id=cid, bug_class=ch["bug_class"], path="writer",
              check_id=out["check_id"], verdict=v["verdict"], tokens_in=ti, tokens_out=to,
              minutes=round((time.monotonic() - t0) / 60, 2))
    db.insert("notes", worker=w, change_id=cid, bug_class=ch["bug_class"], text=out["fix_recipe"])
    if v["verdict"] == "ADOPT":
        broadcast(out["check_id"], w)


def main():
    train_changes()
    evs = learning_events()
    done = {r["change_id"] for r in db.query("SELECT change_id FROM learn_events")} | \
           {r["change_id"] for r in db.query("SELECT change_id FROM notes")}
    print(f"\n[learn] {len(evs)} distinct failures to learn from")
    for ev in evs:
        if ev["id"] in done:
            continue
        try:
            learn_one(ev)
        except llm.LLMError as e:
            print(f"[learn] change {ev['id']}: LLM error {e}")
    print("\n[registry]", json.dumps(registry_core.stats()))


if __name__ == "__main__":
    main()
