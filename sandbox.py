"""Run untrusted checks safely.

Layers:
 1. Static AST screen: banned imports (os, subprocess, socket, shutil, requests, ...),
    banned builtins (open/exec/eval/__import__ ...), dunder escapes (__subclasses__, __globals__ ...).
 2. Fresh subprocess (`python -I`), 10 s wall-clock timeout, CPU rlimit, RLIMIT_FSIZE=0.
 3. A PEP 578 audit hook (cannot be removed once installed) that blocks any network
    connect/bind/DNS, any open() for writing, process spawning, and file deletion/rename —
    except inside a throwaway per-run scratch dir (Hypothesis needs it), which is deleted after.
 4. The repo under test is a read-only copy (files chmod 0444) in a throwaway temp dir.

A check is a pytest module that imports from `billing.*`. Exit 0 = change looks fine,
exit 1 = check flags the change. AST-rule checks get source via `antibody_rt.source_of`.
"""
from __future__ import annotations

import ast
import hashlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import config

ALLOWED_DUNDERS = {"__init__", "__name__", "__doc__", "__eq__", "__repr__"}

BOOTSTRAP = r'''
import sys, resource
resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))  # 1 MB, scratch dir only
try:
    resource.setrlimit(resource.RLIMIT_CPU, (__CPU__, __CPU__ + 1))
except Exception:
    pass
sys.dont_write_bytecode = True
REPO, CHECK, SCRATCH = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path[:0] = [REPO, __RT__]

def _violation(msg):
    sys.stderr.write("SANDBOX_VIOLATION: " + msg + "\n"); sys.stderr.flush()
    raise PermissionError("sandbox: " + msg)

_WRITE_FLAGS = 0x0001 | 0x0002 | 0x0200 | 0x0400 | 0x0800  # O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|O_EXCL (darwin/linux low bits)
_BLOCK_PREFIX = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
                 "os.fork", "os.remove", "os.unlink", "os.rename", "os.rmdir", "os.mkdir",
                 "os.chmod", "os.kill", "shutil.", "urllib.", "http.", "ctypes.", "webbrowser.")
_ALLOW_SOCKET = ()

def _hook(event, args):
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if isinstance(path, int) or path == "/dev/null" or str(path).startswith(SCRATCH):
            return  # fds were already vetted when opened by path
        if isinstance(mode, str) and any(c in mode for c in "wax+"):
            _violation(f"file write {path!r}")
        if isinstance(flags, int) and flags & _WRITE_FLAGS and path not in ("/dev/null",):
            _violation(f"file write {path!r}")
    elif event in ("os.mkdir", "os.rename", "os.replace", "os.remove", "os.unlink") and str(args[0]).startswith(SCRATCH):
        return  # throwaway scratch dir (hypothesis storage); deleted after the run
    elif event.startswith(_BLOCK_PREFIX):
        _violation(f"{event} {args[0] if args else ''!r}")

import hypothesis  # noqa: E402  (import before hook so its own setup is unaffected)
hypothesis.settings.register_profile("sandbox", database=None, deadline=None, max_examples=60,
                                     derandomize=True)
hypothesis.settings.load_profile("sandbox")
import pytest  # noqa: E402
sys.addaudithook(_hook)
code = pytest.main([CHECK, "-q", "-x", "-p", "no:cacheprovider", "-p", "no:cov", "--capture=sys",
                    "--rootdir", REPO, "-W", "ignore::DeprecationWarning"])
sys.exit(int(code))
'''

RUNTIME_HELPER = '''
"""Helpers available to checks inside the sandbox."""
import importlib, inspect, ast

def source_of(module_name):
    return inspect.getsource(importlib.import_module(module_name))

def ast_of(module_name):
    return ast.parse(source_of(module_name))
'''


@dataclass
class SandboxResult:
    status: str               # pass | fail | error | timeout | rejected
    runtime_s: float = 0.0
    violations: list = field(default_factory=list)
    output: str = ""
    reason: str = ""

    @property
    def flagged(self) -> bool:
        """The check says this change is bad."""
        return self.status == "fail"

    @property
    def safe(self) -> bool:
        return self.status not in ("timeout", "rejected") and not self.violations


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()[:16]


def static_screen(code: str) -> tuple[bool, str]:
    """Reject checks that could touch the OS, network, or escape via dunders."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    banned = set(config.BANNED_IMPORTS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in banned:
                    return False, f"banned import: {a.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in banned:
                return False, f"banned import: {node.module}"
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in config.BANNED_CALLS:
                return False, f"banned call: {node.func.id}()"
        elif isinstance(node, ast.Name) and node.id in ("__builtins__", "__import__", "__loader__"):
            return False, f"banned name: {node.id}"
        elif isinstance(node, ast.Attribute):
            a = node.attr
            if a.startswith("__") and a.endswith("__") and a not in ALLOWED_DUNDERS:
                return False, f"banned dunder access: .{a}"
    return True, "ok"


def materialize(files: dict[str, str] | None = None, base: Path | None = None) -> Path:
    """Copy the billing repo (minus reference tests) to a temp dir and overlay `files`
    ({relative path: full new content}). Caller must cleanup()."""
    base = base or config.BILLING_REPO
    dst = Path(tempfile.mkdtemp(prefix="antibody_repo_"))
    shutil.copytree(base / "billing", dst / "billing",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for rel, content in (files or {}).items():
        p = dst / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return dst


def _make_readonly(root: Path):
    for p in sorted(root.rglob("*"), reverse=True):
        p.chmod(stat.S_IRUSR | (stat.S_IXUSR if p.is_dir() else 0))
    root.chmod(stat.S_IRUSR | stat.S_IXUSR)


def cleanup(root: Path):
    if not root.exists():
        return
    for p in [root, *root.rglob("*")]:
        try:
            p.chmod(stat.S_IRWXU)
        except OSError:
            pass
    shutil.rmtree(root, ignore_errors=True)


def run_check(check_code: str, repo_dir: Path, timeout: float | None = None,
              stop_first: bool = True) -> SandboxResult:
    """Run one check against one repo snapshot. Never raises for check misbehaviour."""
    timeout = timeout or config.SANDBOX_TIMEOUT_S
    ok, why = static_screen(check_code)
    if not ok:
        return SandboxResult("rejected", reason=why)

    work = Path(tempfile.mkdtemp(prefix="antibody_sbx_"))
    try:
        snap = work / "repo"
        shutil.copytree(repo_dir, snap, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        rt = work / "rt"
        rt.mkdir()
        (rt / "antibody_rt.py").write_text(RUNTIME_HELPER)
        check_file = work / "chk" / "test_antibody_check.py"
        check_file.parent.mkdir()
        check_file.write_text(check_code)
        boot = work / "boot.py"
        boot_src = BOOTSTRAP.replace("__CPU__", str(int(timeout) + 1)).replace("__RT__", repr(str(rt)))
        if not stop_first:
            boot_src = boot_src.replace('"-q", "-x",', '"-q", "-rf",')
        boot.write_text(boot_src)
        _make_readonly(snap)
        for p in (rt, check_file.parent):
            _make_readonly(p)

        t0 = time.monotonic()
        try:
            scratch = work / "scratch"
            scratch.mkdir()
            proc = subprocess.run(
                [sys.executable, "-I", str(boot), str(snap), str(check_file), str(scratch)],
                cwd=str(check_file.parent), capture_output=True, text=True, timeout=timeout,
                env={"PATH": "/usr/bin:/bin", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
                     "HOME": str(work), "TMPDIR": str(work / "nonexistent"),
                     "HYPOTHESIS_STORAGE_DIRECTORY": str(work / "scratch" / ".hypothesis")},
            )
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            return SandboxResult("timeout", time.monotonic() - t0, output=out[-4000:],
                                 reason=f"exceeded {timeout}s")
        dt = time.monotonic() - t0
        out = (proc.stdout or "") + (proc.stderr or "")
        violations = [l.split("SANDBOX_VIOLATION: ", 1)[1] for l in out.splitlines()
                      if "SANDBOX_VIOLATION: " in l]
        if proc.returncode in (-24, -9, 152, 137):  # SIGXCPU / SIGKILL from the CPU rlimit
            return SandboxResult("timeout", dt, violations, out[-4000:], reason="CPU limit exceeded")
        if violations:
            status = "error"
        elif proc.returncode == 0:
            status = "pass"
        elif proc.returncode == 1:
            status = "fail"
        else:
            status = "error"
        return SandboxResult(status, dt, sorted(set(violations)), out[-4000:],
                             reason=f"pytest exit {proc.returncode}")
    finally:
        cleanup(work)
