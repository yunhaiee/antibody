"""Ablation: is the CEGIS loop needed? For every failure the system learned from, the
Writer drafts a check with ONE round (no counterexample feedback) and the origin worker's
adoption gate judges it. Verdicts are recorded (kind='ablation-cegis1'); these checks never
enter the Registry or any check set."""
import json
from concurrent.futures import ThreadPoolExecutor

import db
import gate
import writer


def one(ev):
    rid = f"ablation-cegis1-{ev['change_id']}"
    out = writer.write_check(ev["change_id"], run_id=rid, log=lambda *a: None, max_rounds=1)
    with db.session() as c:
        c.execute("UPDATE checks SET kind='ablation-cegis1', parent_id=-1 WHERE id=?", (out["check_id"],))
    v = gate.evaluate(out["check_id"], ev["worker"], original=out["original"], run_id=rid, adopt=False)
    full = db.query("SELECT * FROM verdicts WHERE check_id=? AND worker=?", ev["check_id"], ev["worker"])[0]
    return ev, out, v, full


if __name__ == "__main__":
    evs = db.query("SELECT * FROM learn_events WHERE path='writer' ORDER BY id")
    with ThreadPoolExecutor(5) as ex:
        res = list(ex.map(one, evs))
    print(f"{'worker':6s} {'class':14s} {'1 round (no CEGIS)':42s} {'with CEGIS (adopted)':30s}")
    for ev, out, v, full in res:
        print(f"{ev['worker']:6s} {ev['bug_class']:14s} {v['verdict']:7s} variants {v['variants_caught']}/{v['variants_total']} "
              f"good-flagged {v['goods_flagged']}/{v['goods_total']}   "
              f"{full['verdict']:7s} variants {full['variants_caught']}/{full['variants_total']} "
              f"good-flagged {full['goods_flagged']}/{full['goods_total']}")
        if v["verdict"] != "ADOPT":
            print(f"       reason: {v['reason']}")
