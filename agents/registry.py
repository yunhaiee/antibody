"""Antibody Registry — a Fetch.ai uAgent speaking the Agent Chat Protocol.

Reachable from ASI:One (mailbox) and from other agents. Every answer is read from the
SQLite store or produced by a real gate run; the LLM only routes the request.

Commands (natural language is fine; ASI:One sends free text):
  find   – "Is there a proven check for floating-point money bugs?"  / paste a failure trace
  show   – "show check 3"
  gate   – "gate check 3 for worker C"   (runs adapt + the real adoption gate)
  learned– "what has the system learned?"
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uagents import Agent, Context, Protocol  # noqa: E402
from uagents_core.contrib.protocols.chat import (  # noqa: E402
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)

import config  # noqa: E402
import db  # noqa: E402
import gate  # noqa: E402
import llm  # noqa: E402
import registry_core  # noqa: E402
import signature as sigmod  # noqa: E402
import workers  # noqa: E402

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "enum": ["find", "show", "gate", "learned", "help"]},
        "bug_class": {"type": "string", "enum": workers.BUG_CLASSES + ["unknown"]},
        "check_id": {"type": "integer"},
        "worker": {"type": "string", "enum": ["A", "B", "C", "none"]},
    },
    "required": ["command", "bug_class", "check_id", "worker"], "additionalProperties": False,
}

ROUTER_SYSTEM = """You route messages sent to the Antibody Registry, a registry of proven executable
checks (tests) for bugs that coding agents made in a billing service. Pick one command:
- find: the user asks whether a proven check exists for some kind of bug, or pastes a failure/trace.
- show: the user wants the code/evidence of a specific check id.
- gate: the user wants a check (re)gated/tried for a worker (A, B or C).
- learned: the user asks what the system learned, its history, versions, stats or savings.
- help: anything else.
Also classify the bug class the message is about (or unknown):
""" + workers.BUG_CLASS_GUIDE + "\nUse check_id=0 and worker='none' when not given."


def fmt_verdicts(vs: list[dict]) -> str:
    lines = ["| worker | verdict | orig | variants caught | known-good flagged | catch rate (Wilson 80%) |",
             "|---|---|---|---|---|---|"]
    for v in vs:
        if v["verdict"] == "ROLLBACK":
            lines.append(f"| {v['worker']} | ROLLBACK | | | | {v['reason']} |")
            continue
        if v["verdict"] == "NOT_APPLICABLE":
            lines.append(f"| {v['worker']} | NOT_APPLICABLE | – | – | – | – |")
            continue
        orig = {1: "yes", 0: "no"}.get(v["catches_original"], "–")
        lines.append(f"| {v['worker']} | {v['verdict']} | {orig} | {v['variants_caught']}/{v['variants_total']} | "
                     f"{v['goods_flagged']}/{v['goods_total']} | {v['catch_lo']:.2f}–{v['catch_hi']:.2f} |")
    return "\n".join(lines)


def answer_find(text: str, bug_class: str) -> str:
    sig = sigmod.from_text(text, None if bug_class == "unknown" else bug_class)
    matches = registry_core.lookup(sig, min_score=0.3, worker="asi1")
    via = "keyword signature match"
    if not matches:  # free text: let SubQ Snap 1.1 decide which proven check (if any) applies
        matches = registry_core.snap_lookup(text, worker="asi1")
        via = "SubQ Snap 1.1 decision"
    if not matches:
        return (f"No **proven** check matches that ({bug_class}). Proven = adopted by at least one worker "
                f"after passing the gate. Registry: {json.dumps(registry_core.stats())}")
    m = matches[0]
    state = {}
    for v in m["verdicts"]:  # current adoption per worker (ROLLBACK undoes an ADOPT)
        if v["verdict"] in ("ADOPT", "ROLLBACK"):
            state[v["worker"]] = v["verdict"] == "ADOPT"
    adopted = sorted(w for w, on in state.items() if on)
    rolled = sorted(w for w, on in state.items() if not on)
    code = "\n".join(m["code"].splitlines()[:45])
    more = "" if len(matches) == 1 else "\n\nOther matches: " + ", ".join(
        f"#{x['check_id']} ({x['bug_class']}, score {x['score']})" for x in matches[1:4])
    return (f"Yes — check **#{m['check_id']}** ({m['bug_class']}), written by Worker {m['origin_worker']}'s "
            f"Antibody Writer; currently adopted by worker(s) {', '.join(adopted) or '–'}"
            + (f", rolled back by {', '.join(rolled)}" if rolled else "") + f". Matched by {via} (score {m['score']}).\n\n"
            f"**Evidence (every number from a sandboxed run, labelled by the hidden reference suite):**\n"
            f"{fmt_verdicts(m['verdicts'])}\n\n**Fix recipe:** {m['fix_recipe']}\n\n"
            f"```python\n{code}\n```\n(sha {m['hash']}){more}")


def answer_show(check_id: int) -> str:
    try:
        e = registry_core.evidence(check_id)
    except IndexError:
        return f"No check #{check_id}."
    return (f"Check **#{check_id}** — {e['bug_class']} ({e['kind']}), origin worker {e['origin_worker']}, "
            f"owner {e['owner_worker']}, proven={registry_core.is_proven(check_id)}\n\n"
            f"{fmt_verdicts(e['verdicts'])}\n\n**Fix recipe:** {e['fix_recipe']}\n\n```python\n{e['code']}\n```")


def answer_gate(check_id: int, worker: str) -> str:
    if worker not in config.WORKERS:
        return "Which worker should I gate it for (A, B or C)?"
    try:
        e = registry_core.evidence(check_id)
    except IndexError:
        return f"No check #{check_id}."
    if e["owner_worker"] == worker:
        v = gate.evaluate(check_id, worker, run_id="asi1-gate", adopt=False)
        note = "(re-gated on this worker's material; check set unchanged)"
    else:
        v = registry_core.adopt_from_registry(check_id, worker, run_id="asi1-gate", log=lambda *a: None)
        note = f"(adapted to worker {worker}'s module as check #{v.get('adapted_check_id')}, then gated)"
    return (f"Gate result for check #{check_id} on worker {worker} {note}: **{v['verdict']}**\n\n"
            f"- original bug: {({1: 'caught', 0: 'missed'}).get(v.get('catches_original'), 'n/a (not origin)')}\n"
            f"- seeded variants caught: {v.get('variants_caught')}/{v.get('variants_total')} "
            f"(Wilson 80%: {v.get('catch_lo', 0):.2f}–{v.get('catch_hi', 1):.2f})\n"
            f"- known-good changes flagged: {v.get('goods_flagged')}/{v.get('goods_total')}\n"
            f"- max runtime: {v.get('runtime_s')}s\n- reason: {v.get('reason')}"
            + (f"\n- worker {worker} check set is now v{v['check_set_version']}" if v.get("check_set_version") else ""))


def answer_learned() -> str:
    lines = ["**Check-set versions**"]
    for w in config.WORKERS:
        hist = db.query("SELECT version, reason FROM check_sets WHERE worker=? ORDER BY version", w)
        lines.append(f"- Worker {w}: v{gate.current_set(w)[0]} — " + "; ".join(f"v{h['version']}: {h['reason']}" for h in hist))
    vc = db.query("SELECT verdict, COUNT(*) n FROM verdicts v JOIN checks c ON c.id=v.check_id "
                  "WHERE COALESCE(c.parent_id, 0) != -1 GROUP BY verdict")
    lines.append("\n**Verdicts:** " + ", ".join(f"{r['verdict']} {r['n']}" for r in vc))
    le = db.query("SELECT worker, bug_class, tokens_in+tokens_out t, minutes, cf_tokens_in+cf_tokens_out cft, cf_minutes "
                  "FROM learn_events WHERE path='registry'")
    for r in le:
        lines.append(f"- Registry reuse by worker {r['worker']} ({r['bug_class']}): {r['t']:,} tokens / {r['minutes']} min "
                     f"vs {r['cft']:,} tokens / {r['cf_minutes']} min writing it from scratch")
    m = db.query("SELECT arm, key, value FROM metrics WHERE run_id=(SELECT run_id FROM metrics ORDER BY id DESC LIMIT 1)")
    if m:
        lines.append("\n**Latest held-out comparison:** " + "; ".join(f"{r['arm']} {r['key']}={r['value']:.3g}" for r in m))
    return "\n".join(lines)


HELP = ("I'm the **Antibody Registry**: coding agents publish checks for bugs they hit; a check is "
        "*proven* only after it passes an adoption gate on held-out material (catches seeded variants, "
        "doesn't flag known-good changes, runs safely in a sandbox). Try:\n"
        "- *Is there a proven check for floating-point money bugs?*\n- *show check 1*\n"
        "- *gate check 1 for worker B*\n- *What has the system learned?*")


def handle_text(text: str) -> str:
    try:
        r = llm.call("router", ROUTER_SYSTEM, text, schema=ROUTE_SCHEMA, run_id="asi1")["data"]
    except llm.LLMError as e:
        return f"Router error: {e}"
    try:
        if r["command"] == "find":
            return answer_find(text, r["bug_class"])
        if r["command"] == "show":
            return answer_show(r["check_id"])
        if r["command"] == "gate":
            return answer_gate(r["check_id"], r["worker"])
        if r["command"] == "learned":
            return answer_learned()
    except Exception as e:  # never leave an ASI:One user without a reply
        return f"Sorry, that failed: {type(e).__name__}: {e}"
    return HELP


agent = Agent(
    name="antibody-registry",
    seed=config.REGISTRY_SEED,
    port=config.REGISTRY_PORT,
    mailbox=True,
    publish_agent_details=True,
    readme_path=str(Path(__file__).with_name("REGISTRY_README.md")),
    description="Registry of proven, executable bug checks for coding agents (Antibody).",
)
proto = Protocol(spec=chat_protocol_spec)


@proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = "".join(c.text for c in msg.content if isinstance(c, TextContent)).strip()
    if not text:
        return
    ctx.logger.info(f"<- {sender[:20]}…: {text[:200]}")
    reply = await asyncio.to_thread(handle_text, text)
    ctx.logger.info(f"-> {reply[:200]}")
    await ctx.send(sender, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=reply),
                                                EndSessionContent(type="end-session")]))


@proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(proto, publish_manifest=True)


@agent.on_event("startup")
async def hello(ctx: Context):
    ctx.logger.info(f"Antibody Registry address: {agent.address}")
    Path(config.ROOT / "runs").mkdir(exist_ok=True)
    (config.ROOT / "runs" / "registry_address.txt").write_text(agent.address + "\n")


if __name__ == "__main__":
    if "--ask" in sys.argv:  # local test without the network: python agents/registry.py --ask "..."
        print(handle_text(" ".join(sys.argv[sys.argv.index("--ask") + 1:])))
    else:
        agent.run()
