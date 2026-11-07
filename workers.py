"""Coding workers: implement an issue, get labelled by the hidden reference suite.

A worker sees the whole billing package (cached context) and returns ONLY the new
source of the target function. We splice it into the file and run the reference
suite inside the sandbox (LLM-written code is untrusted too).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import config
import db
import llm
import sandbox

BUG_CLASSES = ["float_money", "rounding", "tax_base", "timezone", "boundary_off_by_one",
               "input_mutation", "validation", "ordering", "type_contract", "other"]

_REF_TESTS = (config.BILLING_REPO / "reference_tests" / "test_reference.py").read_text()


def load_issues(worker: str) -> list[dict]:
    return json.loads((config.ISSUES_DIR / f"{worker}.json").read_text())


def package_source(files: dict[str, str] | None = None) -> str:
    """Full billing source, deterministic order -> stable cache prefix."""
    files = files or {}
    parts = []
    for p in sorted((config.BILLING_REPO / "billing").glob("*.py")):
        rel = f"billing/{p.name}"
        parts.append(f"### FILE: {rel}\n```python\n{files.get(rel, p.read_text())}```\n")
    return "<billing_package>\n" + "\n".join(parts) + "</billing_package>"


WORKER_SYSTEM = """You are Worker {worker}, a software engineer on a small billing service.
You implement one ticket at a time. You may change ONLY the single function named in the
ticket. Return the complete new source of that function (def line through end, module-level
indentation) plus any import lines the function newly needs. Do not change other functions.
Keep the function's existing behaviour for all current callers unless the ticket says otherwise.
{extra}"""

CHANGE_SCHEMA = {
    "type": "object",
    "properties": {
        "function_source": {"type": "string"},
        "new_imports": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["function_source", "new_imports", "notes"],
    "additionalProperties": False,
}


def splice(file_text: str, fn_name: str, new_fn_src: str, new_imports: list[str]) -> str:
    """Replace top-level function `fn_name` with `new_fn_src`; add missing imports."""
    new_fn_src = new_fn_src.strip("\n") + "\n"
    ast.parse(new_fn_src)  # raises SyntaxError on garbage
    tree = ast.parse(file_text)
    lines = file_text.splitlines(keepends=True)
    target = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == fn_name), None)
    if target is None:
        raise ValueError(f"function {fn_name} not found")
    start = (target.decorator_list[0].lineno if target.decorator_list else target.lineno) - 1
    out = "".join(lines[:start]) + new_fn_src + "".join(lines[target.end_lineno:])
    imports = [i.strip() for i in new_imports
               if re.match(r"^(from\s+[\w.]+\s+import\s+[\w ,*()]+|import\s+[\w., ]+)$", i.strip())]
    missing = [i for i in imports if i not in out]
    if missing:
        t2 = ast.parse(out)
        last_imp = max((n.end_lineno for n in t2.body if isinstance(n, (ast.Import, ast.ImportFrom))), default=0)
        ls = out.splitlines(keepends=True)
        out = "".join(ls[:last_imp]) + "".join(m + "\n" for m in missing) + "".join(ls[last_imp:])
    ast.parse(out)
    return out


def propose(worker: str, issue: dict, *, notes: list[str] | None = None,
            feedback: str | None = None, run_id: str | None = None) -> dict:
    """Ask the worker for a change. Returns {"files": {path: content}, "call": llm result}."""
    extra = ""
    if notes:
        extra = "\nTeam rules learned from past incidents (follow them):\n" + "\n".join(f"- {n}" for n in notes)
    user = f"Ticket {issue['id']}: {issue['title']}\n\n{issue['body']}\n\nFile: {issue['file']}\nFunction: {issue['function']}"
    if feedback:
        user += f"\n\nYour previous attempt was blocked by an automated check:\n{feedback}\nFix it."
    r = llm.call("worker", WORKER_SYSTEM.format(worker=worker, extra=extra), user,
                 context=package_source(), schema=CHANGE_SCHEMA, run_id=run_id)
    base = (config.BILLING_REPO / issue["file"]).read_text()
    new = splice(base, issue["function"], r["data"]["function_source"], r["data"]["new_imports"])
    return {"files": {issue["file"]: new}, "call": r, "notes": r["data"]["notes"]}


def reference_label(files: dict[str, str]) -> dict:
    """Run the hidden reference suite on a change. good = all pass."""
    repo = sandbox.materialize(files)
    try:
        res = sandbox.run_check(_REF_TESTS, repo, timeout=60, stop_first=False)
    finally:
        sandbox.cleanup(repo)
    failed = sorted(set(re.findall(r"FAILED \S*::(\w+)", res.output)))
    passed = re.search(r"(\d+) passed", res.output)
    label = "good" if res.status == "pass" else "buggy"
    trace = ""
    if label == "buggy":
        e_lines = [l for l in res.output.splitlines() if l.startswith("E ") or "Error" in l]
        trace = "\n".join(e_lines[:12]) or res.output[-1500:]
    return {"label": label, "failed_tests": failed, "n_passed": int(passed.group(1)) if passed else 0,
            "trace": trace, "status": res.status}


def diff_text(files: dict[str, str]) -> str:
    import difflib
    out = []
    for rel, new in files.items():
        old = (config.BILLING_REPO / rel).read_text()
        out += difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}")
    return "".join(out)


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {"bug_class": {"type": "string", "enum": BUG_CLASSES},
                   "explanation": {"type": "string"}},
    "required": ["bug_class", "explanation"], "additionalProperties": False,
}


def classify_bug(diff: str, failed_tests: list[str], trace: str, run_id=None) -> str:
    r = llm.call("router",
                 "Classify the root cause of a regression in a Python billing service into exactly one bug class:\n"
                 + "\n".join(f"- {c}" for c in BUG_CLASSES),
                 f"Diff:\n{diff}\n\nFailing reference tests: {failed_tests}\n\nTrace:\n{trace}",
                 schema=CLASSIFY_SCHEMA, run_id=run_id)
    return r["data"]["bug_class"]


def run_issue(worker: str, issue: dict, arm: str = "none", seed: int = 0, run_id: str | None = None,
              notes: list[str] | None = None) -> dict:
    """Propose + label + store one change (no checks applied)."""
    try:
        prop = propose(worker, issue, notes=notes, run_id=run_id)
    except (llm.LLMError, SyntaxError, ValueError) as e:
        cid = db.insert("changes", run_id=run_id, arm=arm, worker=worker, issue_id=issue["id"], seed=seed,
                        diff="", label="invalid", failing_trace=str(e)[:2000], ref_passed=0, ref_failed=0)
        return {"change_id": cid, "label": "invalid", "error": str(e)}
    lab = reference_label(prop["files"])
    diff = diff_text(prop["files"])
    bug_class = classify_bug(diff, lab["failed_tests"], lab["trace"], run_id) if lab["label"] == "buggy" else None
    cid = db.insert("changes", run_id=run_id, arm=arm, worker=worker, issue_id=issue["id"], seed=seed,
                    diff=diff, label=lab["label"], bug_class=bug_class, failing_trace=lab["trace"],
                    ref_passed=lab["n_passed"], ref_failed=len(lab["failed_tests"]))
    return {"change_id": cid, "label": lab["label"], "bug_class": bug_class,
            "failed_tests": lab["failed_tests"], "files": prop["files"], "diff": diff}


if __name__ == "__main__":
    import sys
    from concurrent.futures import ThreadPoolExecutor
    worker = sys.argv[1] if len(sys.argv) > 1 else "A"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    run_id = f"pilot-{worker}"
    issues = load_issues(worker)[:n]
    with ThreadPoolExecutor(6) as ex:
        for iss, res in zip(issues, ex.map(lambda i: run_issue(worker, i, run_id=run_id), issues)):
            print(f"{iss['id']} {iss['kind']:9s} {iss['function']:18s} -> {res['label']:7s} "
                  f"{res.get('bug_class') or ''} {res.get('failed_tests', '') or res.get('error', '')}")
