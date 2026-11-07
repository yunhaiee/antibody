"""Antibody configuration. Gate thresholds are FROZEN before the full run —
do not edit them after run_compare.py has produced results."""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("ANTIBODY_DB", ROOT / "antibody.db"))
BILLING_REPO = ROOT / "repos" / "billing"
ISSUES_DIR = ROOT / "issues"

# ---- LLM: Claude (Anthropic API). One model; effort is the per-role dial so every
# role shares one prompt-cache namespace. ----
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
# Workers are a cheap, fast fleet (Opus 5.5 workers passed 18/18 pilot issues, leaving
# nothing to learn from); the expensive model writes and adapts the checks.
MODEL_BY_ROLE = {"worker": os.environ.get("WORKER_MODEL", "claude-haiku-5-5")}
NO_FALLBACK_MODELS = ("claude-haiku-5-5",)  # Haiku 5.5 has no server-side fallback
# Workers see code without docstrings/comments (rules live in the hidden tests).
WORKER_CONTEXT_STRIP_DOCS = os.environ.get("WORKER_CONTEXT_STRIP_DOCS", "1") == "1"
# Each worker sees only the file it edits (not the modules it imports): cross-module
# contracts are invisible to it, as for an agent with a narrow context window.
WORKER_CONTEXT_OWN_FILE = os.environ.get("WORKER_CONTEXT_OWN_FILE", "1") == "1"
HELDOUT_SEEDS = 5
EFFORT = {            # per-role reasoning effort
    "writer": "high",     # turns a failure into a general check + variants + look-alikes
    "adapter": "medium",  # rewrites a peer's check for this worker's module
    "worker": "low",      # implements an issue
    "router": "low",      # parses ASI:One chat into a command
}
LLM_MAX_TOKENS = 16000

# ---- Subquadratic SubQ Snap 1.1 (Decisions API; routing/matching only) ----
SUBQ_BASE_URL = "https://api.subq.ai/v1"
SUBQ_MODEL = "subq-snap-1.1"
SUBQ_API_KEY = os.environ.get("SUBQ_API_KEY", "")

# ---- Fetch.ai ----
AGENTVERSE_KEY = os.environ.get("AGENTVERSE_KEY", "")
REGISTRY_SEED = os.environ.get("REGISTRY_SEED", "CHANGE-ME-registry-seed")
REGISTRY_PORT = int(os.environ.get("REGISTRY_PORT", "8101"))
WORKER_PORTS = {"A": 8111, "B": 8112, "C": 8113}

# ---- Sandbox ----
SANDBOX_TIMEOUT_S = 10
BANNED_IMPORTS = ("os", "subprocess", "socket", "shutil", "requests",
                  "urllib", "http", "ctypes", "multiprocessing", "importlib", "pathlib")
BANNED_CALLS = ("open", "exec", "eval", "compile", "__import__", "breakpoint", "input",
                "getattr", "setattr", "delattr", "globals", "vars", "locals")

# ---- Adoption gate (FROZEN) ----
GATE_N_VARIANTS = 8
GATE_MIN_VARIANTS_CAUGHT = 6
GATE_N_GOOD = 20
GATE_MAX_GOOD_FLAGGED = 1
GATE_MAX_RUNTIME_S = 10
WILSON_Z = 1.28
CEGIS_MAX_ROUNDS = 3

# ---- Rollback (FROZEN) ----
ROLLBACK_WINDOW = 10
ROLLBACK_MAX_FALSE_ALARMS = 2

# ---- Experiment ----
WORKERS = ("A", "B", "C")
ISSUES_PER_WORKER = 20
HELDOUT_FRACTION = 1 / 3
SEED = 7
