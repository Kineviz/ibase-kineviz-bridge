#!/usr/bin/env python3
"""The bridge itself: Cypher in over HTTP, nodes and edges back out.

Kineviz connects to this as a "KoreDB Via Proxy API" project, so to Kineviz it
looks like an ordinary graph database. Point it at:

    http://localhost:7073/ibase/<name>

Run it against a real SQL Server:

    export IBASE_CONNECTION_STRING='DRIVER={ODBC Driver 18 for SQL Server};SERVER=localhost,1433;DATABASE=GraphXRConnectorDemo;UID=ibase_ro;PWD=...;Encrypt=yes;TrustServerCertificate=yes'
    python3 ibase_server.py --mapping config/mapping.demo.yml --port 7073

Or with no database at all, to see the SQL it would generate:

    python3 ibase_server.py --mapping config/mapping.demo.yml --compile-only
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback

import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from ibase_bridge import envelope, mapping as mapping_mod, query_log, tsql
from ibase_bridge.logging_setup import setup_logging
from ibase_bridge.mssql_backend import BranchLimitExceeded, MssqlBackend
from ibase_bridge.node_id import NodeIdCodec
from ibase_bridge.query_processor import QueryProcessor

logger = logging.getLogger("ibase_bridge")


class BridgeState:
    """Everything the server serves from, in one place so it can be swapped.

    The schema editor writes a new mapping file and then asks for a reload. Rather
    than restarting the process - which would kill the page doing the asking - we
    rebuild the mapping, id codec and backend here and hand the query processor a
    new one. The database connection is kept: it is unaffected by what the tables
    are called.
    """

    def __init__(self, mapping_path: str, connection=None, id_state=None, db_name="demo"):
        self.mapping_path = mapping_path
        self.connection = connection
        self.id_state = id_state
        self.db_name = db_name
        self.mapping = None
        self.codec = None
        self.backend = None
        self.processor = None
        self.draft = None
        # Surfaced on the status panel, so "is it working?" has an answer without
        # anyone having to find and read a log file.
        self.last_error = None
        self.last_query = None
        self.query_count = 0
        self.public_url = ""
        self.reload()

    def reload(self):
        """Re-read the mapping file and rebuild. Returns a warning, or None."""
        m = mapping_mod.load(self.mapping_path)
        codec = NodeIdCodec(state_path=self.id_state)
        codec.register(m.label_order(), key_types=m.key_types())
        backend = MssqlBackend(m, codec, connection=self.connection)
        self.mapping, self.codec, self.backend = m, codec, backend
        self.processor = QueryProcessor(backend, self.db_name)
        if not codec.is_stateless():
            return ("Some record types have keys that are neither numbers nor a prefix "
                    "followed by digits, so their node ids are handed out in order and "
                    "will change if this bridge restarts. Pass --id-state <file> to keep "
                    "them.")
        return None


def build_state(args):
    conn = None
    if not args.compile_only:
        from ibase_bridge.connection import SqlServerConnection
        probe = mapping_mod.load(args.mapping)
        conn = SqlServerConnection(
            probe.connection_string(),
            pool_size=int(probe.source.get("pool_size") or 8),
            timeout=probe.query_timeout,
            isolation=(probe.source.get("isolation_level") or "READ_UNCOMMITTED"))
        logger.info("connected to SQL Server (major version %s)", conn.server_major)

    name = args.name or os.path.splitext(os.path.basename(args.mapping))[0].replace("mapping.", "")
    state = BridgeState(args.mapping, connection=conn, id_state=args.id_state, db_name=name)
    warn = state.reload()
    if warn:
        logger.warning(warn)
    return state


# Origins allowed to call this bridge from a browser. Kineviz in the browser is
# served from these; the localhost entries are the bridge's own schema editor.
#
# This is an allowlist rather than "*" for a reason worth stating. The bridge has no
# authentication, so any page a browser can be persuaded to open could otherwise
# read everything the SQL login can read. Until now the only thing preventing that
# was Chrome refusing to let a public site reach localhost at all - and the fix
# below deliberately relaxes exactly that. So the two changes belong together.
DEFAULT_ALLOWED_ORIGINS = [
    r"https://([a-z0-9-]+\.)*kineviz\.com",
    r"https?://localhost(:\d+)?",
    r"https?://127\.0\.0\.1(:\d+)?",
    r"https?://\[::1\](:\d+)?",
]


class PrivateNetworkAccess(BaseHTTPMiddleware):
    """Let a browser page reach this bridge on localhost.

    Chrome treats a request from a public https:// page to localhost as "private
    network access" and sends an extra preflight carrying
    `Access-Control-Request-Private-Network: true`. Ordinary CORS handling does not
    know about that header and rejects the preflight with
    "Disallowed CORS private-network" - which the page sees as a request that never
    completes. The symptom is a schema that never loads, with nothing to explain it.

    Answering the preflight with `Access-Control-Allow-Private-Network: true` is what
    makes Kineviz in the browser work against a bridge on this machine. We answer it
    only for origins on the allowlist, never for any site that asks.
    """

    def __init__(self, app, origin_pattern):
        super().__init__(app)
        self._allowed = re.compile("^(" + "|".join(origin_pattern) + ")$", re.IGNORECASE)

    def _ok(self, origin: str) -> bool:
        return bool(origin) and bool(self._allowed.match(origin))

    async def dispatch(self, request, call_next):
        origin = request.headers.get("origin", "")
        wants_private = request.headers.get(
            "access-control-request-private-network", "").lower() == "true"

        if request.method == "OPTIONS" and wants_private:
            if not self._ok(origin):
                return PlainTextResponse(
                    "This bridge only accepts browser requests from Kineviz or from "
                    "this machine. Start it with --allow-origin <url> to add another.",
                    status_code=403)
            return Response(status_code=200, headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Headers":
                    request.headers.get("access-control-request-headers", "*"),
                "Access-Control-Allow-Private-Network": "true",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
            })

        response = await call_next(request)
        if self._ok(origin):
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


def create_app(state, studio: bool = False, allow_origins=None):
    app = FastAPI(title="iBase to Kineviz bridge", version="0.1.0")
    patterns = list(DEFAULT_ALLOWED_ORIGINS) + [re.escape(o.rstrip("/"))
                                                for o in (allow_origins or [])]
    # Reflect the caller's origin rather than sending "*": a browser rejects a
    # wildcard on a credentialed request and silently drops the response.
    app.add_middleware(CORSMiddleware, allow_origin_regex="^(" + "|".join(patterns) + ")$",
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    # Added last, so it runs first and can answer the private-network preflight
    # before ordinary CORS handling refuses it.
    app.add_middleware(PrivateNetworkAccess, origin_pattern=patterns)
    db_name = state.db_name

    if studio:
        from ibase_bridge.studio import build_router
        app.include_router(build_router(state))

    @app.get("/health")
    async def health():
        return {"status": "ok", "database": db_name,
                "node_labels": state.backend.labels(), "rel_types": state.backend.rel_types(),
                "stateless_ids": state.codec.is_stateless()}

    @app.post("/ibase/{name}")
    async def query(name: str, request: Request):
        started = time.time()
        body = await request.json()
        # Kineviz has used several names for this field over the years.
        q = (body.get("query") or body.get("cypher") or body.get("sql")
             or body.get("gql") or body.get("command"))
        params = body.get("params") or {}
        if not q:
            return envelope.error("query parameter is required.")
        try:
            outcome = state.processor.execute(q)
            state.query_count += 1
            state.last_query = " ".join(q.split())[:160]
            state.last_error = None
            elapsed = (time.time() - started) * 1000
            query_log.record(name, q, params, elapsed,
                             getattr(state.backend, "last_sql", ""), outcome)
            return envelope.success(outcome)
        except BranchLimitExceeded as exc:
            state.last_error = str(exc)
            return envelope.error(str(exc))
        except tsql.UnsupportedByTSql as exc:
            state.last_error = str(exc)
            return envelope.error(str(exc))
        except Exception as exc:
            state.last_error = str(exc)
            logger.error("query failed: %s\n%s", exc, traceback.format_exc())
            return envelope.error(str(exc))

    return app


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Serve an i2 iBase SQL Server database to Kineviz.")
    p.add_argument("--mapping", default="config/mapping.demo.yml",
                   help="the mapping file saying which tables are nodes and which are edges")
    p.add_argument("--name", default=None, help="graph name in the URL (default: the mapping's filename)")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 7073)))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--compile-only", action="store_true",
                   help="do not connect to SQL Server; useful for seeing the generated SQL")
    p.add_argument("--id-state", default=None,
                   help="file for remembered node ids (only needed when a key is neither "
                        "a number nor a prefix followed by digits)")
    p.add_argument("--ssl-cert"), p.add_argument("--ssl-key"), p.add_argument("--ssl-password")
    p.add_argument("--allow-origin", action="append", default=[],
                   help="also accept browser requests from this origin, e.g. "
                        "https://my-kineviz.example.com. Kineviz and this machine are "
                        "allowed already.")
    p.add_argument("--studio", action="store_true",
                   help="also serve the schema editor at /studio, for naming links and "
                        "checking their direction against real rows")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def main():
    args = parse_args()
    setup_logging(debug=args.debug)
    try:
        state = build_state(args)
    except mapping_mod.MappingError as exc:
        print("\nThere is a problem with the mapping file:\n  {}\n".format(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print("\nCould not start: {}\n".format(exc), file=sys.stderr)
        return 3

    name = state.db_name
    host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    url = "http://{}:{}/ibase/{}".format(host, args.port, name)
    state.public_url = url
    app = create_app(state, studio=args.studio, allow_origins=args.allow_origin)
    print("\n  iBase bridge is serving {} record types and {} link types."
          .format(len(state.backend.node_schemas), len(state.backend.rel_schemas)))
    print("\n  In Kineviz: Create -> Create New Project")
    print("      Database Type:  KoreDB Via Proxy API")
    print("      Proxy API URL:  {}".format(url))
    print("\n  Works in Kineviz in the browser as well as in Desktop.")
    if args.studio:
        print("\n  Schema editor (name your links and check their direction):")
        print("      http://{}:{}/studio".format(host, args.port))
    print("")
    if args.compile_only:
        print("  (compile-only: no database is connected, so queries will report an error)\n")

    import uvicorn
    kwargs = {"host": args.host, "port": args.port, "log_level": "debug" if args.debug else "info"}
    if args.ssl_cert and args.ssl_key:
        kwargs.update(ssl_certfile=args.ssl_cert, ssl_keyfile=args.ssl_key,
                      ssl_keyfile_password=args.ssl_password)
    uvicorn.run(app, **kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
