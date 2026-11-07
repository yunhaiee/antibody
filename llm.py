"""Claude wrapper. Every call is logged to SQLite (tokens in/out, cache read/write,
latency, effort) — the cost numbers in the demo come from the llm_calls table."""
from __future__ import annotations

import json
import time

import anthropic

import config
import db

_client = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(timeout=600.0, max_retries=3)  # reads ANTHROPIC_API_KEY
    return _client


class LLMError(RuntimeError):
    pass


def call(role: str, system: str, user: str, *, schema: dict | None = None,
         context: str | None = None, effort: str | None = None,
         max_tokens: int | None = None, run_id: str | None = None) -> dict:
    """One Claude call.

    system  – stable role instructions (cached).
    context – large stable material such as repo source (cached, placed before the
              volatile user turn so repeated calls hit the cache).
    schema  – JSON schema; when given, the reply is parsed and returned as `data`.
    Returns {"text", "data", "usage", "latency_ms", "call_id"}.
    """
    effort = effort or config.EFFORT.get(role, "medium")
    sys_blocks = [{"type": "text", "text": system}]
    if context:
        sys_blocks.append({"type": "text", "text": context})
    sys_blocks[-1]["cache_control"] = {"type": "ephemeral"}

    output_config = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}

    t0 = time.monotonic()
    err = None
    resp = None
    try:
        resp = client().beta.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=max_tokens or config.LLM_MAX_TOKENS,
            system=sys_blocks,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        err = f"{type(e).__name__} {e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        err = f"connection error: {e}"
    latency_ms = int((time.monotonic() - t0) * 1000)

    text, usage, stop = "", {}, None
    if resp is not None:
        stop = resp.stop_reason
        text = "".join(b.text for b in resp.content if b.type == "text")
        u = resp.usage
        usage = {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cached_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
        }
        if stop == "refusal":
            err = f"refusal: {getattr(resp, 'stop_details', None)}"
        elif stop == "max_tokens":
            err = "truncated at max_tokens"

    call_id = db.insert("llm_calls", provider="anthropic", model=config.CLAUDE_MODEL, role=role,
                        run_id=run_id, effort=effort, stop_reason=stop, latency_ms=latency_ms,
                        prompt=(system + "\n---\n" + (context or "")[:2000] + "\n---\n" + user)[:20000],
                        output=text[:20000], error=err, **usage)
    if err:
        raise LLMError(f"[call {call_id}] {err}")

    data = None
    if schema:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"[call {call_id}] invalid JSON: {e}") from e
    return {"text": text, "data": data, "usage": usage, "latency_ms": latency_ms, "call_id": call_id}


if __name__ == "__main__":
    # Gate 1a: one call returns valid JSON and is logged.
    schema = {"type": "object",
              "properties": {"bug_class": {"type": "string"}, "confidence": {"type": "number"}},
              "required": ["bug_class", "confidence"], "additionalProperties": False}
    r = call("router", "You classify Python billing bugs into a short snake_case bug class.",
             "Failure: AssertionError: assert isinstance(line_subtotal(li), Decimal) -- got float 0.30000000000000004",
             schema=schema)
    print(json.dumps(r["data"]), r["usage"], f"{r['latency_ms']} ms", f"call_id={r['call_id']}")
    print(db.query("SELECT id, role, effort, input_tokens, output_tokens, cached_tokens, latency_ms, error FROM llm_calls ORDER BY id DESC LIMIT 1"))
