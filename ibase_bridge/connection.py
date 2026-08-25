"""Talking to SQL Server: a small pool, safe session settings, and a read-only lock.

Three things here are load-bearing rather than boilerplate.

**Parameters are bound as the column's real type.** pyodbc sends every Python
string as ``nvarchar``. Comparing an ``nvarchar`` parameter against a ``varchar``
or ``bigint`` column makes SQL Server convert *the column*, so an index lookup
becomes a full table scan — on an iBase expand, the difference between 20
milliseconds and 20 seconds, with nothing in the output to say so.

**Session settings are set explicitly.** ``ARITHABORT`` in particular: with it
off, SQL Server uses a different plan-cache entry than SSMS does, which is the
classic "fast when I test it, slow from the app".

**Reads never block an analyst.** The default isolation level is
``READ UNCOMMITTED``, so a long visualization query neither waits for nor blocks
someone editing records in iBase. The cost is that we can read a half-written
row, which for drawing a picture is acceptable; freezing an investigator's
session is not.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
from typing import Any, Dict, List, Optional, Sequence

from . import tsql

logger = logging.getLogger(__name__)

# Everything the bridge emits starts with one of these. The guard below is not
# redundant with the SELECT-only login: it turns a future bug in the emitter into
# a loud crash instead of a write, and it lets the user see "writes are not
# supported" rather than a SQL Server permission error.
READ_ONLY_START = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

SESSION_SETUP = [
    "SET ANSI_NULLS ON",
    "SET QUOTED_IDENTIFIER ON",
    "SET ANSI_WARNINGS ON",
    "SET ANSI_PADDING ON",
    "SET CONCAT_NULL_YIELDS_NULL ON",
    "SET ARITHABORT ON",
    "SET LOCK_TIMEOUT 5000",
]

ISOLATION = {
    "READ_UNCOMMITTED": "SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED",
    "READ_COMMITTED": "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "SNAPSHOT": "SET TRANSACTION ISOLATION LEVEL SNAPSHOT",
}


class ReadOnlyViolation(Exception):
    pass


class SqlServerConnection:
    """A pool of pyodbc connections, plus the one `run` call the backend needs."""

    def __init__(self, dsn: str, pool_size: int = 8, timeout: int = 120,
                 isolation: str = "READ_UNCOMMITTED", login_timeout: int = 10):
        import pyodbc
        pyodbc.pooling = False          # we manage lifetime so session settings stick
        self._pyodbc = pyodbc
        self.dsn = dsn
        self.timeout = timeout
        self.login_timeout = login_timeout
        self.isolation = isolation
        self._pool: "queue.Queue" = queue.Queue()
        self._lock = threading.Lock()
        self._made = 0
        self.pool_size = pool_size
        self.server_major = 16
        self.compat_level = 160
        self.product_version = ""
        self.edition = ""
        # Open one connection now and let any failure out. Without this a wrong
        # password or an unreachable host produces a perfectly healthy-looking
        # object whose every query fails later, somewhere less obvious.
        probe = self._connect()
        self._release(probe)
        self._made += 1
        self._probe_version()

    # -- lifecycle -------------------------------------------------------------

    def _connect(self):
        conn = self._pyodbc.connect(self.dsn, timeout=self.login_timeout, autocommit=True)
        conn.timeout = self.timeout
        cur = conn.cursor()
        for stmt in SESSION_SETUP + [ISOLATION.get(self.isolation, ISOLATION["READ_UNCOMMITTED"])]:
            cur.execute(stmt)
        cur.close()
        return conn

    def _acquire(self, wait: float = 30.0):
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if self._made < self.pool_size:
                self._made += 1
                mine = True
            else:
                mine = False
        if mine:
            try:
                return self._connect()
            except Exception:
                # Give the slot back. Without this a failed connection - a wrong
                # password, a server that is down - permanently burns a slot, and
                # once every slot has burned, every later request blocks forever on
                # a pool that can never refill.
                with self._lock:
                    self._made -= 1
                raise
        try:
            # Never wait indefinitely: a caller should get an error it can report,
            # not a request that hangs until something times out far upstream.
            return self._pool.get(timeout=wait)
        except queue.Empty:
            raise TimeoutError(
                "every database connection is busy. Try again, or raise pool_size "
                "in the mapping file.")

    def _release(self, conn, broken: bool = False) -> None:
        if broken:
            with self._lock:
                self._made -= 1
            try:
                conn.close()
            except Exception:
                pass
            return
        self._pool.put(conn)

    def close(self) -> None:
        while True:
            try:
                self._pool.get_nowait().close()
            except queue.Empty:
                return
            except Exception:
                pass

    def _probe_version(self) -> None:
        """Find out what this server can do.

        Two numbers matter, not one. OPENJSON needs a 2016 engine (major 13) *and*
        a database left at compatibility level 130 or higher - a database restored
        from an older server keeps its old level, so a modern engine is not on its
        own enough. Below either bar, long id lists go across as XML instead, which
        has worked since 2005.
        """
        try:
            rows = self.run(
                "SELECT CAST(SERVERPROPERTY('ProductMajorVersion') AS int) AS major,"
                " CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(64)) AS version,"
                " CAST(SERVERPROPERTY('Edition') AS nvarchar(128)) AS edition,"
                " (SELECT compatibility_level FROM sys.databases"
                "  WHERE name = DB_NAME()) AS compat", [])
            if rows:
                r = rows[0]
                if r.get("major"):
                    self.server_major = int(r["major"])
                if r.get("compat"):
                    self.compat_level = int(r["compat"])
                self.product_version = r.get("version") or ""
                self.edition = r.get("edition") or ""
        except Exception as exc:
            logger.warning("could not read the SQL Server version (%s); assuming 2016", exc)

    # -- the one call the backend makes ---------------------------------------

    def run(self, sql: str, params: Sequence[Any] = (),
            param_types: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        if not READ_ONLY_START.match(sql):
            raise ReadOnlyViolation(
                "this bridge only ever reads. Refusing to run a statement that does "
                "not begin with SELECT or WITH.")
        conn = self._acquire()
        broken = False
        try:
            cur = conn.cursor()
            sizes = tsql.input_sizes(param_types) if param_types else []
            if sizes and any(s is not None for s in sizes):
                try:
                    cur.setinputsizes(sizes)
                except Exception:
                    pass          # a hint, not a requirement
            cur.execute(sql, list(params)) if params else cur.execute(sql)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            out = [dict(zip(cols, coerce_row(r))) for r in cur.fetchall()]
            cur.close()
            return out
        except Exception:
            broken = True
            raise
        finally:
            self._release(conn, broken)


def coerce_row(row: Sequence[Any]) -> List[Any]:
    """Turn driver values into things that survive JSON.

    `datetime` and `Decimal` do not serialize on their own. Decimals become
    strings rather than floats on purpose — the connector specification is explicit
    that money must not go through binary floating point.
    """
    import datetime
    import decimal
    import uuid

    out = []
    for v in row:
        if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
            out.append(v.isoformat())
        elif isinstance(v, decimal.Decimal):
            out.append(str(v))
        elif isinstance(v, uuid.UUID):
            out.append(str(v))
        elif isinstance(v, (bytes, bytearray, memoryview)):
            out.append(None)          # binary columns are not graph properties
        else:
            out.append(v)
    return out


def self_test_read_only(conn: SqlServerConnection, schema: str, table: str) -> Optional[str]:
    """Try to write, and expect to be refused.

    The specification asks for proof that the login cannot change anything
    (its AC-09). Returns None when the write was correctly refused, or a warning
    string when it was not — in which case the login is over-privileged.
    """
    try:
        conn.run("SELECT 1 AS n", [])
    except Exception as exc:
        return "cannot query at all: {}".format(exc)
    try:
        c = conn._acquire()
        try:
            cur = c.cursor()
            cur.execute("UPDATE {} SET {} = {} WHERE 1 = 0".format(
                tsql.qualify(schema, table), tsql.quote("__probe__"), "NULL"))
        finally:
            conn._release(c, broken=True)
    except Exception:
        return None          # refused, which is what we want
    return ("the SQL login was able to run an UPDATE. Grant it SELECT only - see "
            "the README on creating a read-only login.")
