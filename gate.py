"""Adoption gate. A check is adopted by a worker only if it proves itself on THAT
worker's material, labelled by the hidden reference suite (never by a model):

  * catches the original bug (fails on the buggy change, passes on the fixed repo)  [origin only]
  * catches >= 6/8 seeded bug variants of its class in this worker's area
  * flags <= 1/20 known-good changes from this worker's history
  * safe: every run < 10 s, no sandbox violations
Verdicts: ADOPT (new check-set version) / REJECT (with numbers) / NOT_APPLICABLE.
Wilson bounds (z = 1.28) for catch and false-alarm rates are stored with every verdict.
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor

import config
import db
import llm
import sandbox
import workers

AREA = {"A": ["billing/money.py", "billing/line_items.py", "billing/tax.py"],
        "B": ["billing/totals.py", "billing/invoices.py"],
        "C": ["billing/reports.py"]}


def wilson(k: int, n: int, z: float = config.WILSON_Z) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def run_many(check_code: str, file_sets: list[dict]) -> list[sandbox.SandboxResult]:
    def one(files):
        repo = sandbox.materialize(files)
        try:
            return sandbox.run_check(check_code, repo)
        finally:
            sandbox.cleanup(repo)
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(one, file_sets))


# ---------------------------------------------------------------- gate material
db.connect().executescript("""
CREATE TABLE IF NOT EXISTS gate_material (
  id INTEGER PRIMARY KEY, ts REAL, worker TEXT, bug_class TEXT, kind TEXT,
  files TEXT, ref_label TEXT, source TEXT);
""")

SEED_SCHEMA = {
    "type": "object",
    "properties": {"variants": {"type": "array", "items": {
        "type": "object",
        "properties": {"file": {"type": "string"}, "function": {"type": "string"},
                       "function_source": {"type": "string"}, "what": {"type": "string"}},
        "required": ["file", "function", "function_source", "what"], "additionalProperties": False}}},
    "required": ["variants"], "additionalProperties": False,
}

SEEDER_SYSTEM = """You are a mutation-testing engineer. You write realistic buggy re-implementations
of existing functions, the kind a hurried engineer or coding agent would plausibly ship, each
exhibiting ONE specific bug class. Keep the signature. Each variant must be a complete function
definition (module-level), different from the others, and compile. No comments that reveal the bug."""


def seed_variants(worker: str, bug_class: str, want: int = config.GATE_N_VARIANTS,
                  run_id: str | None = None) -> list[dict]:
    """Seeded bug variants of `bug_class` in the worker's area, confirmed buggy by the
    reference suite. Cached in gate_material."""
    have = db.query("SELECT files FROM gate_material WHERE worker=? AND bug_class=? AND kind='variant' "
                    "AND ref_label='buggy' ORDER BY id", worker, bug_class)
    if len(have) >= want:
        return [json.loads(r["files"]) for r in have[:want]]
    tried = db.query("SELECT COUNT(*) n FROM gate_material WHERE worker=? AND bug_class=? AND kind='variant'",
                     worker, bug_class)[0]["n"]
    if tried >= 3 * want:  # already tried hard; class can't be seeded here
        return [json.loads(r["files"]) for r in have]
    src = "\n".join(f"### FILE: {f}\n```python\n{(config.BILLING_REPO / f).read_text()}```" for f in AREA[worker])
    r = llm.call("adapter", SEEDER_SYSTEM,
                 f"Bug class: {bug_class} ({dict(l.split(': ', 1) for l in workers.BUG_CLASS_GUIDE.replace('- ', '').splitlines()).get(bug_class, '')})\n"
                 f"Write {want + 4} distinct variants, spread over the functions below where this bug class can occur. "
                 f"If it cannot occur in a function, skip that function.",
                 context=src, schema=SEED_SCHEMA, run_id=run_id)
    cands = []
    for v in r["data"]["variants"]:
        if v["file"] not in AREA[worker]:
            continue
        try:
            base = (config.BILLING_REPO / v["file"]).read_text()
            cands.append({v["file"]: workers.splice(base, v["function"], v["function_source"], [])})
        except Exception:
            continue

    def label(files):
        return workers.reference_label(files)["label"]
    with ThreadPoolExecutor(6) as ex:
        labels = list(ex.map(label, cands))
    for files, lab in zip(cands, labels):
        db.insert("gate_material", worker=worker, bug_class=bug_class, kind="variant",
                  files=files, ref_label=lab, source=f"seeder call {r['call_id']}")
    return seed_variants(worker, bug_class, want, run_id)


GOOD_SYSTEM = """You write realistic, behaviour-preserving rewrites of existing functions (the kind of
refactor a teammate would ship): renamed locals, restructured control flow, helper extraction
inside the function, equivalent expressions. They must keep EXACT behaviour, including rounding,
types, validation and ordering. Each is a complete module-level function with its signature."""


def synthetic_good(worker: str, need: int, run_id=None) -> list[dict]:
    """Top-up known-good changes: independent rewrites (never see any check), kept only if
    the reference suite labels them good. Cached in gate_material(kind='good')."""
    have = db.query("SELECT files FROM gate_material WHERE worker=? AND kind='good' AND ref_label='good' ORDER BY id", worker)
    if len(have) >= need:
        return [json.loads(r["files"]) for r in have[:need]]
    src = "\n".join(f"### FILE: {f}\n```python\n{(config.BILLING_REPO / f).read_text()}```" for f in AREA[worker])
    r = llm.call("adapter", GOOD_SYSTEM, f"Write {need + 6} rewrites spread across the functions below.",
                 context=src, schema=SEED_SCHEMA, run_id=run_id)
    cands = []
    for v in r["data"]["variants"]:
        if v["file"] in AREA[worker]:
            try:
                base = (config.BILLING_REPO / v["file"]).read_text()
                cands.append({v["file"]: workers.splice(base, v["function"], v["function_source"], [])})
            except Exception:
                pass
    with ThreadPoolExecutor(6) as ex:
        labels = list(ex.map(lambda f: workers.reference_label(f)["label"], cands))
    for files, lab in zip(cands, labels):
        db.insert("gate_material", worker=worker, bug_class=None, kind="good", files=files, ref_label=lab,
                  source=f"rewrite call {r['call_id']}")
    have = db.query("SELECT files FROM gate_material WHERE worker=? AND kind='good' AND ref_label='good' ORDER BY id", worker)
    return [json.loads(r["files"]) for r in have[:need]]


def known_good(worker: str, n: int = config.GATE_N_GOOD, exclude_ids: tuple = ()) -> list[dict]:
    """The worker's real past changes that the reference suite labelled good, topped up with
    reference-confirmed independent rewrites if the history has fewer than n distinct ones."""
    rows = db.query("SELECT id, files FROM changes WHERE worker=? AND label='good' AND files IS NOT NULL "
                    "AND run_id='train' ORDER BY id", worker)
    seen, out = set(), []
    for r in rows:
        if r["id"] in exclude_ids or r["files"] in seen:
            continue
        seen.add(r["files"])
        out.append(json.loads(r["files"]))
        if len(out) == n:
            break
    if len(out) < n:
        out += synthetic_good(worker, n - len(out))
    return out


# ---------------------------------------------------------------- the gate
def current_set(worker: str) -> tuple[int, list[int]]:
    row = db.query("SELECT version, check_ids FROM check_sets WHERE worker=? ORDER BY version DESC LIMIT 1", worker)
    if not row:
        return 0, []
    return row[0]["version"], json.loads(row[0]["check_ids"])


def new_version(worker: str, check_ids: list[int], reason: str) -> int:
    v, _ = current_set(worker)
    db.insert("check_sets", worker=worker, version=v + 1, check_ids=sorted(set(check_ids)), reason=reason)
    return v + 1


def evaluate(check_id: int, worker: str, *, original: dict | None = None, run_id=None, adopt=True,
             record=True) -> dict:
    """Gate one check for one worker. `original` = {"buggy": files} for the origin worker."""
    chk = db.query("SELECT * FROM checks WHERE id=?", check_id)[0]
    code, bug_class = chk["code"], chk["bug_class"]

    ok, why = sandbox.static_screen(code)
    if not ok:
        return _record(check_id, worker, "REJECT", reason=f"static screen: {why}", adopt=False, record=record)

    variants = seed_variants(worker, bug_class, run_id=run_id)
    goods = known_good(worker)
    min_variants = math.ceil(config.GATE_MIN_VARIANTS_CAUGHT / config.GATE_N_VARIANTS * len(variants))
    if len(variants) < 5:
        return _record(check_id, worker, "NOT_APPLICABLE",
                       reason=f"only {len(variants)} confirmed {bug_class} variants seedable in this area",
                       variants_total=len(variants), adopt=False, record=record)

    sets = ([original["buggy"], {}] if original else []) + variants + goods
    res = run_many(code, sets)
    k = 2 if original else 0
    orig_ok = None
    if original:
        orig_ok = res[0].flagged and res[1].status == "pass"
    v_res, g_res = res[k:k + len(variants)], res[k + len(variants):]
    caught = sum(r.flagged for r in v_res)
    flagged = sum(r.status != "pass" for r in g_res)   # an error on a good change is a false alarm too
    runtime = max((r.runtime_s for r in res), default=0)
    safe = all(r.safe for r in res) and runtime < config.GATE_MAX_RUNTIME_S
    violations = sorted({v for r in res for v in r.violations})

    reasons = []
    if original and not orig_ok:
        reasons.append(f"does not catch the original bug (buggy:{res[0].status}, fixed:{res[1].status})")
    if caught < min_variants:
        reasons.append(f"caught {caught}/{len(variants)} variants (< {min_variants})")
    if flagged > config.GATE_MAX_GOOD_FLAGGED:
        reasons.append(f"flagged {flagged}/{len(goods)} known-good changes (> {config.GATE_MAX_GOOD_FLAGGED})")
    if not safe:
        reasons.append(f"unsafe: runtime {runtime:.1f}s, violations {violations}")
    verdict = "REJECT" if reasons else "ADOPT"
    return _record(check_id, worker, verdict, catches_original=orig_ok, variants_caught=caught,
                   variants_total=len(variants), goods_flagged=flagged, goods_total=len(goods),
                   runtime_s=runtime, reason="; ".join(reasons) or "all criteria met", adopt=adopt, record=record)


def _record(check_id, worker, verdict, *, catches_original=None, variants_caught=0, variants_total=0,
            goods_flagged=0, goods_total=0, runtime_s=0.0, reason="", adopt=True, record=True) -> dict:
    clo, chi = wilson(variants_caught, variants_total)
    flo, fhi = wilson(goods_flagged, goods_total)
    version = None
    if verdict == "ADOPT" and adopt and record:
        _, ids = current_set(worker)
        version = new_version(worker, ids + [check_id], f"ADOPT check {check_id}")
    row = dict(check_id=check_id, worker=worker, verdict=verdict,
               catches_original=None if catches_original is None else int(catches_original),
               variants_caught=variants_caught, variants_total=variants_total,
               goods_flagged=goods_flagged, goods_total=goods_total, runtime_s=round(runtime_s, 2),
               catch_lo=round(clo, 3), catch_hi=round(chi, 3), fa_lo=round(flo, 3), fa_hi=round(fhi, 3),
               reason=reason, check_set_version=version)
    if record:
        row["verdict_id"] = db.insert("verdicts", **row)
    return row


# ---------------------------------------------------------------- rollback monitor
def monitor(worker: str, change_id: int, files: dict, ref_label: str) -> list[int]:
    """Run the worker's adopted checks on a new change whose reference label is known.
    A check flagging a reference-good change is a false alarm. If a check has >= 2 false
    alarms in its last 10 monitored runs, roll back to a check set without it."""
    _, ids = current_set(worker)
    rolled = []
    for cid in ids:
        code = db.query("SELECT code FROM checks WHERE id=?", cid)[0]["code"]
        res = run_many(code, [files])[0]
        fa = int(ref_label == "good" and res.status != "pass")
        db.insert("monitor_runs", check_id=cid, worker=worker, change_id=change_id, false_alarm=fa)
        last = db.query("SELECT false_alarm FROM monitor_runs WHERE check_id=? AND worker=? ORDER BY id DESC LIMIT ?",
                        cid, worker, config.ROLLBACK_WINDOW)
        if sum(r["false_alarm"] for r in last) >= config.ROLLBACK_MAX_FALSE_ALARMS:
            _, cur = current_set(worker)
            if cid in cur:
                v = new_version(worker, [c for c in cur if c != cid],
                                f"ROLLBACK check {cid}: {sum(r['false_alarm'] for r in last)} false alarms in last {len(last)}")
                db.insert("verdicts", check_id=cid, worker=worker, verdict="ROLLBACK", check_set_version=v,
                          reason=f"{sum(r['false_alarm'] for r in last)} false alarms in last {len(last)} monitored runs")
                rolled.append(cid)
    return rolled
