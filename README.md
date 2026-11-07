# Antibody

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)

**Stack Overflow for coding agents, where every answer is executable and verified.**

A team of coding agents works in parallel on one tightly coupled codebase. Every failure becomes a
versioned, **executable check** (a pytest or Hypothesis test), not a note in a prompt. A check is
adopted only if it proves itself on held-out material; otherwise it is rejected, or rolled back later.
Proven checks, each with a fix recipe, are shared through a **Fetch.ai Registry agent**. A teammate's
agent adapts a proven check to its own module and re-gates it, instead of debugging the same bug class
again. The Registry is usable directly from **ASI:One**.

## How it works

```
 Worker A/B/C (Claude Haiku 5.5)  ──change──▶  hidden reference suite labels it (good / buggy)
        │ buggy
        ▼
 ask Registry: proven check for this bug signature? ──yes──▶ adapt to my module ─▶ gate ─▶ adopt
        │ no
        ▼
 Antibody Writer (Claude Opus 5.5): general check for the BUG CLASS + fix recipe
        │  CEGIS: run on bug variants (must fail) and benign look-alikes (must pass);
        │         feed misses and false alarms back, up to 3 rounds
        ▼
 Adoption gate (per worker, on ITS OWN material, in the sandbox)
   • catches the original bug (fails on buggy change, passes on fixed)    [origin worker]
   • catches ≥ 6/8 seeded variants of the class in this worker's area
   • flags ≤ 1/20 known-good changes from this worker's history
   • safe: < 10 s, no network, no file writes
   → ADOPT (check set v+1) · REJECT (with numbers) · NOT_APPLICABLE
   Wilson 80% bounds (z = 1.28) stored for catch and false-alarm rates
        ▼
 Registry (uAgent, Agent Chat Protocol, Agentverse mailbox, ASI:One)
   stores code, hash, bug class, signature, fix recipe, evidence and every verdict
 Rollback monitor: adopted checks re-run on new reference-labelled changes;
   ≥ 2 false alarms in the last 10 → roll back one check-set version
```

**What keeps it honest**
- **Correctness comes from the reference suite.** The hidden suite (`repos/billing/reference_tests/`,
  62 tests) labels every change: worker output, seeded variant, look-alike and retry. No model labels
  a change good or buggy.
- **Gate material is independent of the check's author.** Seeded variants come from a separate
  "seeder" prompt that never sees the check, and they count only if the reference suite confirms
  they're buggy. Known-good changes are the worker's real past changes labelled good. If the history
  has fewer than 20 distinct ones, it's topped up with independent rewrites that the reference suite
  confirms correct.
- **Checks are untrusted code.**
  - `sandbox.py` rejects banned imports (os, subprocess, socket, shutil, requests and similar),
    dangerous builtins and dunder escapes before anything runs.
  - Each check runs in a fresh `python -I` subprocess with a 10 s timeout, a CPU rlimit and a
    file-size rlimit.
  - A PEP 578 audit hook blocks network, writes, spawning and deletes. The only exception is a
    throwaway scratch directory that Hypothesis needs.
  - The repo under test is a read-only copy.
  - Model-written worker code is also run only inside the sandbox.
- **Every number in the demo comes from SQLite** (`antibody.db`): LLM calls with input, output and
  cached tokens and latency, changes, checks, verdicts, check-set versions, monitor runs, lookups
  and metrics. Gate thresholds are frozen in `config.py`.

## Results

_Filled from `run_compare.py` and `reuse.py`; see "Reproduce"._

RESULTS_PLACEHOLDER

## Agents

| Agent | Role | Address |
|---|---|---|
| `antibody-registry` | Registry: Chat Protocol and mailbox, reachable from ASI:One | `agent1qgq5apjfckrmhxmxqp259s8lhdlh7evezu8da92zzllqmayylcpdk502yt0` |
| Worker A / B / C | Coding workers (Claude Haiku 5.5), each owning modules of `repos/billing` | run in-process by `learn.py` / `run_compare.py` |
| Antibody Writer | Turns a failure into a check plus fix recipe (Claude Opus 5.5, CEGIS) | `writer.py` |

Try it in ASI:One: mention the registry's handle and ask, for example:
- *Is there a proven check for floating-point money bugs?*
- *show check 1*
- *gate check 1 for worker C* (runs the real gate)
- *What has the system learned?*

## Run it

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env              # ANTHROPIC_API_KEY (required), SUBQ_API_KEY (optional)
.venv/bin/python -m pytest tests repos/billing/reference_tests -q   # sandbox + gate + reference suite
.venv/bin/python learn.py         # workers on train issues -> Writer -> gate -> Registry broadcast
.venv/bin/python run_compare.py compare     # 4-arm held-out experiment
.venv/bin/python run_compare.py compare --versions   # held-out effect per check-set version
.venv/bin/python reuse.py compare # Registry reuse vs writing from scratch (tokens + minutes)
.venv/bin/python demo.py          # the 5 demo beats, read from antibody.db
.venv/bin/python agents/registry.py   # Registry uAgent (open the printed inspector link -> Connect -> Mailbox)
```

## Repo layout

```
config.py      frozen gate thresholds, models, ports
llm.py         Claude wrapper; every call logged (tokens in/out/cached, latency)
sandbox.py     static screen + isolated subprocess + audit hook + read-only repo copy
workers.py     coding workers: propose a change, splice it, label with the hidden suite
writer.py      Antibody Writer with CEGIS
gate.py        adoption gate, seeded variants, known-good material, Wilson bounds, rollback monitor
signature.py   bug signatures (error type + assertion text + APIs + class) and matching
registry_core.py  proven-only lookup, adaptation, adopt-from-registry, evidence
agents/registry.py  the Registry uAgent (Agent Chat Protocol, mailbox, ASI:One)
learn.py       training and learning loop
run_compare.py 4-arm held-out experiment
reuse.py       with vs without Registry, measured
demo.py        prints the 5 demo beats from SQLite
repos/billing/ billing service (money → line_items → tax → totals → invoices → reports) + reference_tests/
issues/        issue queues (build_issues.py → A.json, B.json, C.json)
tests/         test_sandbox.py, test_gate.py
```

## Design decisions and limits (honest)

- **Workers are Claude Haiku 5.5 and see only the file they edit**, with docstrings stripped. With
  Opus 5.5 workers the pilot got 18/18 issues right. Haiku with the full package got only 2–4 of 60
  wrong, which leaves nothing to learn from. The narrow context models an agent with a small window
  on a coupled codebase: cross-module contracts are invisible to it. The Writer, adapter and router
  use Claude Opus 5.5.
- **Issue design.**
  - Tickets are "implement this function from a one-line spec" or realistic edits (refactor,
    decouple from a helper, extend with an optional parameter, speed up). None instructs a bug.
  - The held-out tickets implement 9 new functions that reuse conventions the team already has:
    tie-breaks, the discounted-amount free-shipping rule, aware datetimes, validation, rounding.
    That is what "repeat bug" measures.
  - The split is fixed in `issues/build_issues.py`.
- **Bug classes** are assigned by a Claude classifier from the diff, the failing reference tests and
  the trace, using a fixed taxonomy. It is not perfect: the same `compute_totals` bug has been
  labelled `boundary_off_by_one` and `type_contract` on different runs.
- **The Writer sees the failing CI output** (assertion messages and test names), like a developer
  reading CI, but never the reference test code.
- **Statistics.** Small n (7 held-out tickets per worker × 5 seeds). Wilson intervals are reported;
  read the differences with that in mind.
- **Rollback** is implemented and runs on every held-out change in the network arm. It fires only if
  a check really false-alarms; the demo reports whether it did.
- **Models.** Claude (Anthropic API): Haiku 5.5 workers; Opus 5.5 for the Writer, adapter and router.
- **Fetch.ai.** The Registry runs as a local uAgent with an Agentverse mailbox. Workers call the
  Registry's core functions in-process for speed; ASI:One and other agents use the Chat Protocol.
  Almanac **contract** registration needs testnet funds and was skipped. Almanac API registration
  succeeds.

## Provenance

All code in this repository was written from scratch; the git history shows how it was built.

## License

MIT, see `LICENSE`.
