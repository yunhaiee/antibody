"""Antibody Registry core (the uAgent in agents/registry.py is a thin Chat Protocol
front-end over these functions). Stores checks + every verdict in SQLite; answers
signature lookups with PROVEN checks only (>= 1 ADOPT verdict, not rolled back)."""
from __future__ import annotations

import json
import time

import config
import db
import gate
import llm
import sandbox
import signature as sigmod
import workers


def is_proven(check_id: int) -> bool:
    """A check family is proven while at least one worker still has a member of it adopted
    (ADOPT not followed by a ROLLBACK for that worker)."""
    root = db.query("SELECT COALESCE(parent_id, id) r FROM checks WHERE id=?", check_id)[0]["r"]
    fam = [r["id"] for r in db.query("SELECT id FROM checks WHERE id=? OR parent_id=?", root, root)]
    v = db.query(f"SELECT check_id, worker, verdict FROM verdicts WHERE check_id IN ({','.join('?' * len(fam))}) "
                 "AND verdict IN ('ADOPT','ROLLBACK') ORDER BY id", *fam)
    state = {}
    for r in v:
        state[(r["check_id"], r["worker"])] = r["verdict"] == "ADOPT"
    return any(state.values())


def evidence(check_id: int) -> dict:
    c = db.query("SELECT id, origin_worker, owner_worker, parent_id, bug_class, title_or_kind, code, fix_recipe, "
                 "signature, evidence, hash FROM (SELECT *, kind AS title_or_kind FROM checks) WHERE id=?", check_id)[0]
    root = c["parent_id"] or c["id"]
    family = [r["id"] for r in db.query("SELECT id FROM checks WHERE id=? OR parent_id=?", root, root)]
    verdicts = db.query(f"SELECT check_id, worker, verdict, catches_original, variants_caught, variants_total, "
                        f"goods_flagged, goods_total, catch_lo, catch_hi, fa_lo, fa_hi, reason, check_set_version "
                        f"FROM verdicts WHERE check_id IN ({','.join('?' * len(family))}) ORDER BY id", *family)
    return {"check_id": c["id"], "root_id": root, "hash": c["hash"], "origin_worker": c["origin_worker"],
            "owner_worker": c["owner_worker"], "bug_class": c["bug_class"], "kind": c["title_or_kind"],
            "fix_recipe": c["fix_recipe"], "signature": sigmod.loads(c["signature"]),
            "code": c["code"], "verdicts": verdicts}


def lookup(query_sig: dict, *, exclude_owner: str | None = None, min_score: float = 0.45,
           worker: str | None = None, method: str = "keyword") -> list[dict]:
    """Proven checks ranked by signature similarity (structured keyword match)."""
    t0 = time.monotonic()
    rows = db.query("SELECT id, owner_worker, signature FROM checks WHERE parent_id IS NULL")
    scored = []
    for r in rows:
        if exclude_owner and r["owner_worker"] == exclude_owner:
            continue
        if not is_proven(r["id"]):
            continue
        s = sigmod.score(query_sig, sigmod.loads(r["signature"]))
        if s >= min_score:
            scored.append((s, r["id"]))
    scored.sort(reverse=True)
    db.insert("lookups", worker=worker, signature=query_sig, method=method,
              matched_check_id=scored[0][1] if scored else None, candidates=len(scored),
              latency_ms=int((time.monotonic() - t0) * 1000), tokens=0)
    return [{"score": s, **evidence(cid)} for s, cid in scored]


# ------------------------------------------------------------------ adaptation
ADAPT_SYSTEM = """You adapt a PROVEN executable check from one team's module to another team's module
in the same billing service. Keep the same bug class and the same intent, but target the public
functions/types of the TARGET area where this bug class can occur. Same sandbox rules as the
original (pytest/hypothesis/decimal/datetime/zoneinfo/math/collections/itertools/inspect/ast and
`antibody_rt`; no os/subprocess/socket/shutil/requests/pathlib/importlib/open/eval/exec/getattr/
dunders). It must PASS on the target area's current (correct) code. If this bug class cannot
occur in the target area at all, set not_applicable=true and explain why."""

ADAPT_SCHEMA = {
    "type": "object",
    "properties": {"not_applicable": {"type": "boolean"}, "why": {"type": "string"},
                   "check_code": {"type": "string"}, "apis": {"type": "array", "items": {"type": "string"}}},
    "required": ["not_applicable", "why", "check_code", "apis"], "additionalProperties": False,
}


def adapt(check_id: int, worker: str, run_id: str | None = None) -> dict:
    """Muse-style adaptation (here: Claude): rewrite a peer's check for `worker`'s area."""
    src = evidence(check_id)
    area = workers.area_source(gate.AREA[worker])
    r = llm.call("adapter", ADAPT_SYSTEM,
                 f"Bug class: {src['bug_class']}\nFix recipe: {src['fix_recipe']}\n"
                 f"Original check (written for worker {src['origin_worker']}):\n```python\n{src['code']}\n```\n\n"
                 f"TARGET area: worker {worker}. Adapt the check.",
                 context=area, schema=ADAPT_SCHEMA, run_id=run_id)
    d = r["data"]
    out = {"call_id": r["call_id"], "usage": r["usage"], "latency_ms": r["latency_ms"]}
    if d["not_applicable"]:
        return {**out, "not_applicable": True, "why": d["why"]}
    sig = dict(src["signature"])
    sig["apis"] = sorted({a.split(".")[-1] for a in d["apis"]})
    h = sandbox.code_hash(d["check_code"])
    ex = db.query("SELECT id FROM checks WHERE hash=?", h)
    new_id = ex[0]["id"] if ex else db.insert(
        "checks", hash=h, origin_worker=src["origin_worker"], owner_worker=worker, parent_id=src["root_id"],
        bug_class=src["bug_class"], signature=sig, kind="adapted", code=d["check_code"],
        fix_recipe=src["fix_recipe"], cegis_rounds=0, evidence={"adapted_from": check_id, "why": d["why"]})
    return {**out, "not_applicable": False, "check_id": new_id}


def adopt_from_registry(check_id: int, worker: str, run_id: str | None = None, log=print) -> dict:
    """Adapt a peer's proven check to `worker`, then gate the ADAPTED version on the
    worker's own material."""
    t0 = time.monotonic()
    a = adapt(check_id, worker, run_id)
    if a["not_applicable"]:
        v = gate._record(check_id, worker, "NOT_APPLICABLE", reason=f"adapter: {a['why']}", adopt=False)
    else:
        v = gate.evaluate(a["check_id"], worker, run_id=run_id)
    v["adapted_check_id"] = a.get("check_id")
    v["minutes"] = round((time.monotonic() - t0) / 60, 2)
    log(f"[registry] worker {worker} <- check #{check_id}: {v['verdict']} "
        f"(variants {v.get('variants_caught')}/{v.get('variants_total')}, known-good flagged "
        f"{v.get('goods_flagged')}/{v.get('goods_total')}) {v.get('reason', '')}")
    return v


def stats() -> dict:
    q = db.query
    return {
        "checks": q("SELECT COUNT(*) n FROM checks WHERE parent_id IS NULL")[0]["n"],
        "adapted": q("SELECT COUNT(*) n FROM checks WHERE parent_id IS NOT NULL")[0]["n"],
        "verdicts": {r["verdict"]: r["n"] for r in q(
            "SELECT verdict, COUNT(*) n FROM verdicts v JOIN checks c ON c.id=v.check_id "
            "WHERE COALESCE(c.parent_id, 0) != -1 GROUP BY verdict")},
        "check_sets": {w: gate.current_set(w)[0] for w in config.WORKERS},
    }


# ------------------------------------------------------------------ SubQ Snap matcher
def snap_lookup(failure_text: str, *, worker: str | None = None, threshold: float = 0.5) -> list[dict]:
    """Rank proven checks with Subquadratic SubQ Snap 1.1 (a decision model): one yes/no
    question per candidate, 'would this check catch the bug in this failure?'. Logged to
    llm_calls (provider=subq) and lookups (method=snap)."""
    import requests  # only the host process uses the network; checks never do
    t0 = time.monotonic()
    cands = [r["id"] for r in db.query("SELECT id FROM checks WHERE parent_id IS NULL") if is_proven(r["id"])]
    if not cands or not config.SUBQ_API_KEY:
        return []
    qs = {}
    for cid in cands:
        e = evidence(cid)
        qs[f"c{cid}"] = {
            "type": "noul",
            "instructions": f"Is the bug in this failure the same KIND of bug that check #{cid} guards against, so "
                            f"the check could be adapted to the failing code? Check #{cid}: bug class {e['bug_class']}; "
                            f"originally for {', '.join(e['signature'].get('apis', []))}; fix recipe: {e['fix_recipe'][:400]}",
            "criteria": {"true": "same root-cause kind of bug (possibly in a different function or module)",
                         "false": "a different root cause"},
        }
    body = {"model": config.SUBQ_MODEL, "state": failure_text[:20000], "questions": qs}
    err, out, usage = None, {}, {}
    try:
        r = requests.post(f"{config.SUBQ_BASE_URL}/systemone", json=body, timeout=60,
                          headers={"Authorization": f"Bearer {config.SUBQ_API_KEY}"})
        r.raise_for_status()
        out = r.json()
        usage = out.get("usage", {})
    except Exception as e:  # noqa: BLE001
        err = str(e)[:500]
    latency = int((time.monotonic() - t0) * 1000)
    db.insert("llm_calls", provider="subq", model=config.SUBQ_MODEL, role="matcher", run_id=f"snap-{worker}",
              input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0), cached_tokens=0,
              cache_write_tokens=0, latency_ms=latency, prompt=json.dumps(body)[:20000],
              output=json.dumps(out)[:20000], error=err)
    if err:
        return []
    probs = sorted(((a["noul"], int(k[1:])) for k, a in out.get("answers", {}).items()), reverse=True)
    hits = [(p, cid) for p, cid in probs if p >= threshold]
    db.insert("lookups", worker=worker, signature={"text": failure_text[:300]}, method="snap",
              matched_check_id=hits[0][1] if hits else None, candidates=len(hits), latency_ms=latency,
              tokens=usage.get("input_tokens", 0))
    return [{"score": round(p, 3), **evidence(cid)} for p, cid in hits]


# ------------------------------------------------------------------ instantiation (work time)
INSTANTIATE_SYSTEM = """A teammate is about to implement a NEW function. You hold a PROVEN executable check
(learned from an earlier bug) and its fix recipe. Write an INSTANCE of that check for the NEW
function: same bug class and intent, exercising the new function through its signature and the
ticket's spec, plus the existing code it builds on. You see only the signature and the ticket,
never an implementation, so test only properties the spec and the team's established conventions
(visible in the existing code and the recipe) imply. Do not invent exact values the spec does not
determine. Same sandbox rules as the original check (pytest/hypothesis/decimal/datetime/zoneinfo/
math/collections/itertools/inspect/ast; no os/subprocess/socket/shutil/requests/pathlib/importlib/
open/eval/exec/getattr/dunders). If the bug class cannot apply to this function, set
not_applicable=true."""

db.connect().executescript("""
CREATE TABLE IF NOT EXISTS instances (
  id INTEGER PRIMARY KEY, ts REAL, check_id INT, worker TEXT, issue_id TEXT, not_applicable INT,
  why TEXT, code TEXT, call_id INT);
""")


def signature_of(file: str, fn: str) -> str:
    import ast as _ast
    src = (config.BILLING_REPO / file).read_text()
    for n in _ast.parse(src).body:
        if isinstance(n, _ast.FunctionDef) and n.name == fn:
            n.body = [_ast.Expr(_ast.Constant(Ellipsis))]
            return _ast.unparse(n)
    raise KeyError(fn)


def instantiate(check_id: int, worker: str, issue: dict, run_id: str | None = None) -> dict:
    """Instance of a proven check for a new function (cached per check x ticket)."""
    have = db.query("SELECT * FROM instances WHERE check_id=? AND worker=? AND issue_id=?", check_id, worker, issue["id"])
    if have:
        return have[0]
    e = evidence(check_id)
    r = llm.call("adapter", INSTANTIATE_SYSTEM,
                 f"Bug class: {e['bug_class']}\nFix recipe: {e['fix_recipe']}\nProven check:\n```python\n{e['code']}\n```\n\n"
                 f"NEW function (file {issue['file']}):\n```python\n{signature_of(issue['file'], issue['function'])}\n```\n"
                 f"Ticket: {issue['title']}. {issue['body']}",
                 context=workers.area_source(gate.AREA[worker]), schema=ADAPT_SCHEMA, run_id=run_id)
    d = r["data"]
    code = d["check_code"] if not d["not_applicable"] else ""
    ok, why = sandbox.static_screen(code) if code else (True, "")
    row = dict(check_id=check_id, worker=worker, issue_id=issue["id"], not_applicable=int(d["not_applicable"] or not ok),
               why=d["why"] if ok else f"static screen: {why}", code=code if ok else "", call_id=r["call_id"])
    db.insert("instances", **row)
    return row
