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

# ---- Meta Model API (Muse Spark), OpenAI-compatible Chat Completions ----
MUSE_BASE_URL = os.environ.get("MUSE_BASE_URL", "https://api.meta.ai/v1")
MUSE_MODEL = os.environ.get("MUSE_MODEL", "muse-spark-1.3")
MUSE_API_KEY = os.environ.get("MODEL_API_KEY", "")
MUSE_REASONING_EFFORT = os.environ.get("MUSE_REASONING_EFFORT", "low")
MUSE_MAX_COMPLETION_TOKENS = int(os.environ.get("MUSE_MAX_COMPLETION_TOKENS", "8000"))
MUSE_TIMEOUT_S = 300

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
BANNED_CALLS = ("open", "exec", "eval", "compile", "__import__", "breakpoint", "input")

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
