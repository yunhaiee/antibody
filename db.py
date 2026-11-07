"""SQLite store. Every number shown in the demo is read from here."""
import json
import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY, ts REAL, provider TEXT, model TEXT, role TEXT, run_id TEXT,
  input_tokens INT, output_tokens INT, cached_tokens INT, reasoning_tokens INT,
  latency_ms INT, prompt TEXT, output TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, kind TEXT, arm TEXT, started REAL, finished REAL, meta TEXT);
CREATE TABLE IF NOT EXISTS changes (
  id INTEGER PRIMARY KEY, ts REAL, run_id TEXT, arm TEXT, worker TEXT, issue_id TEXT, seed INT,
  diff TEXT, label TEXT, bug_class TEXT, failing_trace TEXT, ref_passed INT, ref_failed INT,
  blocked_by TEXT);
CREATE TABLE IF NOT EXISTS checks (
  id INTEGER PRIMARY KEY, ts REAL, hash TEXT UNIQUE, origin_worker TEXT, owner_worker TEXT,
  parent_id INT, bug_class TEXT, signature TEXT, kind TEXT, code TEXT, fix_recipe TEXT,
  cegis_rounds INT, evidence TEXT);
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY, ts REAL, check_id INT, worker TEXT, verdict TEXT,
  catches_original INT, variants_caught INT, variants_total INT,
  goods_flagged INT, goods_total INT, runtime_s REAL,
  catch_lo REAL, catch_hi REAL, fa_lo REAL, fa_hi REAL, reason TEXT, check_set_version INT);
CREATE TABLE IF NOT EXISTS check_sets (
  id INTEGER PRIMARY KEY, ts REAL, worker TEXT, version INT, check_ids TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS monitor_runs (
  id INTEGER PRIMARY KEY, ts REAL, check_id INT, worker TEXT, change_id INT, false_alarm INT);
CREATE TABLE IF NOT EXISTS lookups (
  id INTEGER PRIMARY KEY, ts REAL, worker TEXT, signature TEXT, method TEXT,
  matched_check_id INT, candidates INT, latency_ms INT, tokens INT);
CREATE TABLE IF NOT EXISTS metrics (
  id INTEGER PRIMARY KEY, ts REAL, run_id TEXT, arm TEXT, key TEXT, value REAL, detail TEXT);
"""


def connect(path=None):
    conn = sqlite3.connect(str(path or config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path=None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert(table: str, **row) -> int:
    row.setdefault("ts", time.time())
    row = {k: (json.dumps(v) if isinstance(v, (dict, list, tuple)) else v) for k, v in row.items()}
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    with session() as c:
        cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(row.values()))
        return cur.lastrowid


def query(sql: str, *args):
    with session() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
