"""Bug signatures: error type + failing assertion text + APIs/types involved + bug class.
Matching is structured keyword overlap (deterministic, explainable)."""
from __future__ import annotations

import json
import re

STOP = {"assert", "the", "and", "for", "with", "none", "true", "false", "self", "def", "return",
        "where", "from", "import", "test", "billing"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z_][a-z0-9_]{2,}", (text or "").lower()) if t not in STOP}


def build(trace: str, failed_tests: list[str], apis: list[str], bug_class: str) -> dict:
    err = re.findall(r"\b([A-Z][A-Za-z]+(?:Error|Exception))\b", trace or "")
    asserts = [l.strip()[2:].strip() for l in (trace or "").splitlines() if l.strip().startswith("E ")]
    return {
        "bug_class": bug_class,
        "error_type": err[0] if err else "AssertionError",
        "assertion_text": " | ".join(asserts[:3])[:400],
        "apis": sorted({a.split(".")[-1] for a in apis}),
        "tests": failed_tests,
    }


def score(query: dict, candidate: dict) -> float:
    """0..1 similarity between two signatures."""
    s = 0.0
    if query.get("bug_class") and query.get("bug_class") == candidate.get("bug_class"):
        s += 0.45
    if query.get("error_type") == candidate.get("error_type"):
        s += 0.1
    qa, ca = set(query.get("apis", [])), set(candidate.get("apis", []))
    if qa and ca:
        s += 0.2 * len(qa & ca) / len(qa | ca)
    qt = _tokens(query.get("assertion_text", "")) | _tokens(" ".join(query.get("tests", [])))
    ct = _tokens(candidate.get("assertion_text", "")) | _tokens(" ".join(candidate.get("tests", [])))
    if qt and ct:
        s += 0.25 * len(qt & ct) / len(qt | ct)
    return round(s, 3)


def from_text(text: str, bug_class: str | None = None) -> dict:
    """Signature from free text (e.g. an ASI:One question or a pasted trace)."""
    return {"bug_class": bug_class, "error_type": (re.findall(r"\b([A-Z][A-Za-z]+Error)\b", text) or ["AssertionError"])[0],
            "assertion_text": text[:400], "apis": sorted(set(re.findall(r"\b([a-z]+_[a-z_]+)\(", text))), "tests": []}


def loads(s) -> dict:
    return json.loads(s) if isinstance(s, str) else s
