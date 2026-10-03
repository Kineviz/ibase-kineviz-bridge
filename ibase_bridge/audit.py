"""An audit trail of every query and everything it returned, in one SQLite file.

Turned on with `--audit-db <file>`. Off by default, because the file it writes holds
the records themselves, not just counts: anyone who can read it can read every row
an analyst has looked at. It is created readable by its owner only.

Unlike logs/queries.jsonl, which is a debugging aid and gives up quietly, this log
**fails closed**: if a row cannot be written, the caller gets an error instead of
the data. Data that was not recorded is never handed out.

Each row carries a SHA-256 hash of itself and of the row before it, so editing,
deleting or reordering a row in the middle breaks the chain, and `verify` names
the first row that no longer matches. Triggers also refuse UPDATE and DELETE. Both
stop accidents and casual edits; neither stops someone who can replace the whole
file. Copy the file somewhere the bridge's own account cannot write if that matters.

    python -m ibase_bridge.audit verify logs/audit.sqlite
    python -m ibase_bridge.audit tail logs/audit.sqlite -n 20
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Hashed in this order. Changing the list changes every hash, so add to the end.
_FIELDS = ["ts", "source", "db", "client_ip", "remote_user", "origin", "user_agent",
           "query", "params", "sql", "status", "result_type", "error", "elapsed_ms",
           "node_count", "rel_count", "row_count", "response"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,     -- UTC, ISO 8601
    source      TEXT NOT NULL,     -- 'query' (Kineviz) or 'studio-sample' / 'studio-preview'
    db          TEXT,
    client_ip   TEXT,
    remote_user TEXT,              -- set only if an authenticating proxy passes it on
    origin      TEXT,
    user_agent  TEXT,
    query       TEXT,              -- the Cypher, or what the studio was asked for
    params      TEXT,              -- JSON
    sql         TEXT,              -- the T-SQL that actually ran
    status      TEXT NOT NULL,     -- 'ok' | 'refused' | 'error'
    result_type TEXT,              -- GRAPH | TABLE | SCHEMA
    error       TEXT,
    elapsed_ms  INTEGER,
    node_count  INTEGER,
    rel_count   INTEGER,
    row_count   INTEGER,
    response    TEXT,              -- JSON, exactly what was sent back
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END;
"""

_GENESIS = "0" * 64

# Headers an authenticating reverse proxy commonly uses to say who signed in. The
# bridge has no login of its own, so this is the only way a name can reach the log.
USER_HEADERS = ("x-forwarded-user", "x-remote-user", "x-auth-request-user",
                "x-forwarded-email", "remote-user")


class AuditWriteFailed(Exception):
    """The row could not be written, so the data must not be returned."""


def _row_hash(prev_hash: str, row: Dict[str, Any]) -> str:
    body = json.dumps([row.get(f) for f in _FIELDS], default=str,
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def _json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


def counts(response: Any) -> Dict[str, Optional[int]]:
    """Node, edge and row counts from a response, for reading the log without parsing it."""
    out: Dict[str, Optional[int]] = {"node_count": None, "rel_count": None, "row_count": None}
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict) and data.get("type") == "GRAPH":
        inner = data.get("data") or {}
        out["node_count"] = len(inner.get("nodes") or [])
        out["rel_count"] = len(inner.get("relationships") or [])
    elif isinstance(data, dict) and data.get("type") == "TABLE":
        out["row_count"] = max(0, len(data.get("data") or []) - 1)
    elif isinstance(response, dict) and isinstance(response.get("rows"), list):
        out["row_count"] = len(response["rows"])  # the studio's sample and preview
    return out


def client_info(request) -> Dict[str, Optional[str]]:
    """Who asked, as far as the bridge can tell, from a Starlette request."""
    h = request.headers
    user = next((h.get(k) for k in USER_HEADERS if h.get(k)), None)
    return {"client_ip": request.client.host if request.client else None,
            "remote_user": user, "origin": h.get("origin"),
            "user_agent": h.get("user-agent")}


class AuditLog:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if not os.path.exists(path):
            # Create it owner-only before SQLite opens it, so there is no moment
            # when the file exists with the default, wider permissions.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        last = self._conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        self._prev = last[0] if last else _GENESIS

    def record(self, *, source: str, db: Optional[str], query: Optional[str],
               status: str, response: Any = None, params: Any = None,
               sql: Optional[str] = None, result_type: Optional[str] = None,
               error: Optional[str] = None, elapsed_ms: Optional[float] = None,
               client: Optional[Dict[str, Optional[str]]] = None) -> int:
        """Append one row and return its id. Raises AuditWriteFailed on any failure."""
        row: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "source": source, "db": db, "query": query, "params": _json(params),
            "sql": sql or None, "status": status, "result_type": result_type,
            "error": error, "elapsed_ms": int(elapsed_ms) if elapsed_ms is not None else None,
            "response": _json(response),
        }
        row.update(client or {})
        row.update(counts(response))
        try:
            with self._lock:
                h = _row_hash(self._prev, row)
                cols = _FIELDS + ["prev_hash", "hash"]
                cur = self._conn.execute(
                    "INSERT INTO audit ({}) VALUES ({})".format(
                        ",".join(cols), ",".join("?" * len(cols))),
                    [row.get(f) for f in _FIELDS] + [self._prev, h])
                self._prev = h
                return cur.lastrowid
        except Exception as exc:
            raise AuditWriteFailed(str(exc)) from exc

    def close(self):
        self._conn.close()


def verify(path: str) -> Dict[str, Any]:
    """Walk the chain. Returns {"ok": True, "rows": n} or where it first breaks."""
    conn = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    conn.row_factory = sqlite3.Row
    prev, n = _GENESIS, 0
    for r in conn.execute("SELECT * FROM audit ORDER BY id"):
        row = dict(r)
        if row["prev_hash"] != prev:
            return {"ok": False, "rows": n, "bad_id": row["id"],
                    "reason": "a row before this one was removed, added or reordered"}
        if _row_hash(prev, row) != row["hash"]:
            return {"ok": False, "rows": n, "bad_id": row["id"],
                    "reason": "this row was changed after it was written"}
        prev, n = row["hash"], n + 1
    conn.close()
    return {"ok": True, "rows": n, "last_hash": prev}


def main(argv=None):
    p = argparse.ArgumentParser(description="Check or read the bridge's audit log.")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="check that no row was changed, removed or reordered")
    v.add_argument("path")
    t = sub.add_parser("tail", help="show the most recent rows, without the returned data")
    t.add_argument("path")
    t.add_argument("-n", type=int, default=20)
    args = p.parse_args(argv)
    if not os.path.isfile(args.path):
        print("No audit log at {}. Is that the file given to --audit-db?".format(args.path))
        return 2

    if args.cmd == "verify":
        res = verify(args.path)
        if res["ok"]:
            print("ok: {} rows, chain intact. Last hash {}".format(res["rows"], res["last_hash"]))
            return 0
        print("BROKEN at row {}: {} ({} rows before it check out)"
              .format(res["bad_id"], res["reason"], res["rows"]))
        return 1

    conn = sqlite3.connect("file:{}?mode=ro".format(args.path), uri=True)
    rows = conn.execute(
        "SELECT id, ts, source, client_ip, remote_user, status, node_count, rel_count, "
        "row_count, substr(replace(query, char(10), ' '), 1, 80) "
        "FROM audit ORDER BY id DESC LIMIT ?", (args.n,)).fetchall()
    for r in reversed(rows):
        print("{:>6}  {}  {:<14} {:<15} {:<12} {:<7} n={} e={} r={}  {}".format(
            *[("-" if x is None else x) for x in r]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
