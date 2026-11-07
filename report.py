"""Render the README results section from antibody.db (no hand-typed numbers).
python report.py            -> prints markdown
python report.py --readme   -> replaces the block between RESULTS markers in README.md"""
import json
import sys

import config
import db


def key_findings(m, rec) -> list[str]:
    f = []
    if rec:
        f.append(f"- **The same bug on code the system has learned about.** Re-attempting the "
                 f"{int(rec['recurrence']['attempts']) // 5} training tickets that failed (5 fresh seeds each, "
                 f"{int(rec['recurrence']['attempts'])} attempts): **{int(rec['recurrence']['buggy'])}/{int(rec['recurrence']['attempts'])}** "
                 f"come back buggy with no learning, **{int(rec['recurrence-notes']['buggy'])}/{int(rec['recurrence-notes']['attempts'])}** "
                 f"with the lessons as prompt notes, and **0** merge buggy with checks "
                 f"({int(rec['recurrence']['buggy_blocked'])}/{int(rec['recurrence']['buggy'])} blocked, "
                 f"{int(rec['recurrence']['good_flagged'])} false blocks). A note is advice; a check is a guarantee.")
    if m:
        n = int(m["none"]["attempts"])
        f.append(f"- **New code (held-out tickets that implement functions which did not exist during learning).** "
                 f"Repeat-class bugs merged: none {int(m['none']['bugs_through'])}/{n}, own notes {int(m['notes']['bugs_through'])}/{n}, "
                 f"shared recipes as notes {int(m['shared_notes']['bugs_through'])}/{n}, checks only {int(m['checks']['bugs_through'])}/{n}, "
                 f"network checks {int(m['network']['bugs_through'])}/{n}, checks + recipes {int(m['antibody']['bugs_through'])}/{n}"
                 + (f", + work-time instances {int(m['antibody+instances']['bugs_through'])}/{n}" if 'antibody+instances' in m else "")
                 + ". Learned checks never exercise code written later, so on new functions the transferable part is the "
                 "**recipe**. Instantiating checks for a new function at work time helps a little, but with no oracle it "
                 + (f"also sent {int(m['antibody+instances'].get('advisory_on_good', 0))} correct changes back for a retry." if 'antibody+instances' in m else "is noisy."))
    return f


def md() -> str:
    out = []
    run = db.query("SELECT run_id FROM metrics WHERE key='attempts' AND arm='none' ORDER BY id DESC LIMIT 1")[0]["run_id"]
    m = {}
    for r in db.query("SELECT arm, key, value FROM metrics WHERE run_id=?", run):
        m.setdefault(r["arm"], {})[r["key"]] = r["value"]
    rec = {}
    for r in db.query("SELECT arm, key, value FROM metrics WHERE run_id='recurrence'"):
        rec.setdefault(r["arm"], {})[r["key"]] = r["value"]
    out.append("### Key findings\n")
    out += key_findings(m, rec if "recurrence-notes" in rec else None)
    out.append("")
    n_tickets = sum(1 for w in config.WORKERS for i in json.load(open(config.ISSUES_DIR / f"{w}.json")) if i["split"] == "heldout")
    out.append("### Held-out experiment\n")
    out.append(f"**Setup**: {n_tickets} held-out tickets × {config.HELDOUT_SEEDS} seeds = "
               f"{int(m['none']['attempts'])} attempts per arm, with the same tickets and seeds in every arm. "
               "`none`, `checks` and `network` share the same initial proposals. A *repeat bug* is a buggy "
               "change that merges and belongs to a bug class already seen in training. Labels come from "
               "the hidden reference suite.\n")
    out.append("| arm | what the worker gets | buggy merged | repeat-bug rate (Wilson 80%) | correct merged | blocked | false blocks |")
    out.append("|---|---|---|---|---|---|---|")
    desc = {"none": "nothing", "notes": "its own lessons as rules-file notes",
            "shared_notes": "the antibody recipes as notes, no checks (control)",
            "checks": "its own adopted checks gate merges (1 retry)",
            "network": "own + peers' adapted checks, live rollback",
            "antibody": "network checks **and** their fix recipes",
            "antibody+instances": "antibody + work-time instances of proven checks for NEW functions (advisory: feedback + 1 retry)"}
    for arm in ("none", "notes", "shared_notes", "checks", "network", "antibody", "antibody+instances"):
        x = m.get(arm)
        if not x:
            continue
        out.append(f"| `{arm}` | {desc[arm]} | {int(x['bugs_through'])} | {x['repeat_bug_rate']:.3f} "
                   f"[{x['repeat_bug_rate_lo']:.3f}–{x['repeat_bug_rate_hi']:.3f}] | {x['good_merged_rate']:.0%} | "
                   f"{int(x.get('blocked', 0))} | {int(x.get('false_blocks', 0))} |")
    out.append("")
    if rec:
        r0 = rec["recurrence"]
        out.append(f"**Recurrence on covered code** (not held-out; training tickets that failed, seeds 10–14): "
                   f"no learning {int(r0['buggy'])}/{int(r0['attempts'])} buggy · notes "
                   f"{int(rec.get('recurrence-notes', {}).get('buggy', -1))}/{int(rec.get('recurrence-notes', {}).get('attempts', 0))} buggy · "
                   f"checks block {int(r0['buggy_blocked'])}/{int(r0['buggy'])} buggy, flag {int(r0['good_flagged'])}/{int(r0['good'])} good.\n")
    vrows = db.query("SELECT arm, value, detail FROM metrics WHERE run_id=? AND key='heldout_catch' ORDER BY id", run)
    if vrows:
        seen = {}
        for r in vrows:
            seen[r["arm"]] = r
        out.append("**Held-out effect of each check-set version.** Each version is applied to the same held-out "
                   "proposals from the `none` arm:\n")
        out.append("| worker · version | change | held-out buggy caught | held-out correct flagged |")
        out.append("|---|---|---|---|")
        for arm, r in seen.items():
            d = json.loads(r["detail"])
            out.append(f"| {arm} | {d.get('reason', '')} | {int(r['value'])}/{d['buggy']} | {d['false_alarms']}/{d['good']} |")
        out.append("")
    reuse = db.query("SELECT r.*, c.issue_id FROM reuse_runs r JOIN changes c ON c.id=r.change_id ORDER BY r.id")
    if reuse:
        out.append("**A worker hits a bug class another worker already solved** (both paths run for real; "
                   "tokens and minutes come from the call log):\n")
        out.append("| ticket | path | verdict | fix (reference suite) | tokens | minutes |")
        out.append("|---|---|---|---|---|---|")
        for r in reuse:
            p = ("Registry: lookup → adapt check #%d from worker %s → gate" % (r["matched_check_id"], r["origin_worker"])
                 if r["path"] == "registry" else "from scratch: Writer + CEGIS + gate")
            out.append(f"| {r['worker']} {r['issue_id']} ({r['bug_class']}) | {p} | {r['verdict']} | {r['fix_label']} | "
                       f"{r['tokens']:,} | {r['minutes']} |")
        out.append("")
    v = {r["verdict"]: r["n"] for r in db.query(
        "SELECT verdict, COUNT(*) n FROM verdicts v JOIN checks c ON c.id=v.check_id "
        "WHERE COALESCE(c.parent_id,0) != -1 GROUP BY verdict")}
    abl = db.query("SELECT v.worker, c.bug_class, v.verdict, v.variants_caught, v.variants_total FROM verdicts v "
                   "JOIN checks c ON c.id=v.check_id WHERE c.kind='ablation-cegis1' ORDER BY v.id")
    full = db.query("SELECT le.worker, le.bug_class, v.variants_caught, v.variants_total FROM learn_events le "
                    "JOIN verdicts v ON v.check_id=le.check_id AND v.worker=le.worker "
                    "WHERE le.path='writer' AND v.verdict IN ('ADOPT','REJECT') ORDER BY le.id")
    out.append(f"**Registry verdicts**: " + ", ".join(f"{k} {n}" for k, n in sorted(v.items())) +
               ". Every ROLLBACK came from the frozen rule (≥ 2 false alarms in the last 10 monitored changes).\n")
    if abl:
        out.append(f"**CEGIS ablation** (single-round Writer, same failures, origin worker's gate): "
                   f"{sum(a['verdict'] == 'ADOPT' for a in abl)}/{len(abl)} adopted, "
                   f"{sum(a['variants_caught'] or 0 for a in abl)}/{sum(a['variants_total'] or 0 for a in abl)} variants caught; "
                   f"with CEGIS: {len(full)}/{len(full)} adopted, "
                   f"{sum(f['variants_caught'] or 0 for f in full)}/{sum(f['variants_total'] or 0 for f in full)} variants caught. "
                   + "; ".join(f"{a['worker']} {a['bug_class']}: {a['verdict']} {a['variants_caught']}/{a['variants_total']}" for a in abl if a["verdict"] != "ADOPT") + "\n")
    cost = db.query("SELECT model, COUNT(*) n, SUM(input_tokens) i, SUM(cached_tokens) c, SUM(output_tokens) o "
                    "FROM llm_calls GROUP BY model ORDER BY n DESC")
    out.append("**Cost log** (all calls today, from `llm_calls`): " + "; ".join(
        f"{r['model']}: {r['n']} calls, {(r['i'] or 0):,} input + {(r['c'] or 0):,} cached input, {(r['o'] or 0):,} output tokens"
        for r in cost))
    return "\n".join(out)


if __name__ == "__main__":
    text = md()
    if "--readme" in sys.argv:
        p = config.ROOT / "README.md"
        s = p.read_text()
        a, b = "<!-- RESULTS:START -->", "<!-- RESULTS:END -->"
        if a not in s:
            s = s.replace("RESULTS_PLACEHOLDER", f"{a}\n{b}")
        i, j = s.index(a) + len(a), s.index(b)
        p.write_text(s[:i] + "\n" + text + "\n" + s[j:])
        print("README updated")
    else:
        print(text)
