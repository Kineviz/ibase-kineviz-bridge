"""A small web page for the two things a schema cannot tell you.

Discovery can read your tables, keys and foreign keys, and it can even ask the data
which record types a link joins. Two things it structurally cannot know:

  **names**      - a table called `Employment` is a link called `WORKS_FOR` only
                   because a person says so.
  **direction**  - nothing in SQL Server says whether a Person works for an
                   Organisation or the other way round.

Direction is the dangerous one. Get it backwards and the bridge does not fail: it
returns *no rows*, which reads as "nothing matched". So this page is built around
making direction **visible** rather than merely editable. Every link shows real
rows from your database, refreshed the moment you flip the arrow. If flipping
makes data appear, you had it backwards; if it makes data vanish, you just broke it.

Everything is served from this file, with no external assets, so it works on a
machine that cannot reach the internet - which is the normal situation for a
database like this.
"""

from __future__ import annotations

import copy
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- draft model
# The page edits a "draft": discovery's findings plus the human's decisions. It is
# turned into a real mapping only to preview or to save, so a half-finished draft
# can never be loaded by the bridge.

def draft_from_discovery(found: Dict[str, Any], schema: str = "dbo",
                         link_end: Optional[Dict[str, Any]] = None,
                         connection_env: str = "IBASE_CONNECTION_STRING") -> Dict[str, Any]:
    nodes = []
    for n in found.get("nodes", []):
        props = [c for c in n.get("columns", []) if c != n["key"]]
        nodes.append({
            "table": n["table"], "label": n["table"], "key": n["key"],
            "properties": props, "types": n.get("types", {}),
            "rows": n.get("rows", 0), "include": True,
            "display": _display_column(props, n.get("types", {})),
            "why": n.get("why", ""), "confidence": n.get("confidence", ""),
        })
    edges = []
    for e in found.get("edges", []):
        fks = e.get("fks", [])
        pairs = []
        if e.get("resolution") == "link_end":
            for ep in e.get("endpoints", []):
                pairs.append({"src_label": ep["src_label"], "dst_label": ep["dst_label"],
                              "src_column": "", "dst_column": "",
                              "src_prefix": ep.get("src_prefix"), "dst_prefix": ep.get("dst_prefix"),
                              "rows": ep.get("rows", 0)})
        elif len(fks) >= 2:
            pairs.append({"src_label": fks[0]["ref_table"], "dst_label": fks[1]["ref_table"],
                          "src_column": fks[0]["column"], "dst_column": fks[1]["column"],
                          "src_prefix": None, "dst_prefix": None, "rows": e.get("rows", 0)})
        edges.append({
            "table": e["table"], "type": _suggest_type_name(e["table"]), "key": e.get("key"),
            "resolution": e.get("resolution", "fk"),
            "link_end": (link_end or {}).get("guessed") if e.get("resolution") == "link_end" else None,
            "pairs": pairs,
            "properties": e.get("properties", []), "types": e.get("types", {}),
            "rows": e.get("rows", 0), "include": bool(e.get("key")) and bool(pairs),
            "why": e.get("why", ""), "confidence": e.get("confidence", ""),
            "self_referencing": e.get("self_referencing", False),
        })
    return {"schema": schema, "connection_env": connection_env,
            "nodes": nodes, "edges": edges,
            "ambiguous": found.get("ambiguous", []), "skipped": found.get("skipped", [])}


def _display_column(properties: List[str], types: Dict[str, str]) -> Optional[str]:
    """Pick the column a person would recognise a record by.

    Used only for the preview, so that a link reads "Avery Chen -> Northwind
    Logistics" rather than "1001 -> 2001". A row of numbers tells you nothing about
    whether the direction is right.
    """
    preferred = ("name", "full_name", "fullname", "surname", "title", "description",
                 "label", "account_number", "registration")
    lower = {p.lower(): p for p in properties}
    for want in preferred:
        if want in lower:
            return lower[want]
    for p in properties:
        t = (types.get(p) or "").lower()
        if t in ("nvarchar", "varchar", "nchar", "char", "text"):
            return p
    return properties[0] if properties else None


def _suggest_type_name(table: str) -> str:
    """A first guess at a link's name. Still just the table name - the point of the
    page is that a person improves it."""
    return table.upper()


def flip(edge: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse every endpoint pair of one link."""
    for p in edge.get("pairs", []):
        p["src_label"], p["dst_label"] = p["dst_label"], p["src_label"]
        p["src_column"], p["dst_column"] = p["dst_column"], p["src_column"]
        p["src_prefix"], p["dst_prefix"] = p.get("dst_prefix"), p.get("src_prefix")
    return edge


# ------------------------------------------------------ draft -> real mapping

def draft_to_mapping_dict(draft: Dict[str, Any], only_edge: Optional[str] = None) -> Dict[str, Any]:
    """Turn the draft into the structure `mapping.from_dict` accepts.

    `only_edge` keeps just one link, which is what the preview uses so that a broken
    definition elsewhere in the draft cannot stop you checking the one in front of you.
    """
    labels = {}
    nodes = []
    for n in draft["nodes"]:
        if not n.get("include"):
            continue
        labels[n["table"]] = n["label"]
        nodes.append({"label": n["label"], "table": n["table"], "key": n["key"],
                      "properties": list(n["properties"]),
                      "types": dict(n.get("types") or {})})
    edges = []
    for e in draft["edges"]:
        if not e.get("include") or (only_edge and e["table"] != only_edge):
            continue
        if not e.get("key") or not e.get("pairs"):
            continue
        endpoints = []
        ok = True
        for p in e["pairs"]:
            src = labels.get(p["src_label"], p["src_label"])
            dst = labels.get(p["dst_label"], p["dst_label"])
            if src not in {n["label"] for n in nodes} or dst not in {n["label"] for n in nodes}:
                ok = False
                break
            ep = {"src": {"label": src}, "dst": {"label": dst}}
            if e.get("resolution") in ("fk", "prefixed_fk"):
                ep["src"]["column"] = p["src_column"]
                ep["dst"]["column"] = p["dst_column"]
            if p.get("src_prefix"):
                ep["src"]["prefix"] = p["src_prefix"]
            if p.get("dst_prefix"):
                ep["dst"]["prefix"] = p["dst_prefix"]
            ep["row_estimate"] = p.get("rows", 0)
            endpoints.append(ep)
        if not ok or not endpoints:
            continue
        edge = {"type": e["type"], "table": e["table"], "key": e["key"],
                "resolution": e.get("resolution", "fk"), "endpoints": endpoints,
                "properties": list(e.get("properties") or []),
                "types": dict(e.get("types") or {})}
        if e.get("resolution") == "link_end" and e.get("link_end"):
            le = dict(e["link_end"])
            le.setdefault("table", "_LinkEnd")
            le.setdefault("src_end_value", 1)
            le.setdefault("dst_end_value", 2)
            edge["link_end"] = le
        edges.append(edge)

    return {"version": 2,
            "source": {"dialect": "sqlserver", "schema": draft.get("schema", "dbo"),
                       "connection_env": draft.get("connection_env", "IBASE_CONNECTION_STRING"),
                       "query_timeout_seconds": 120,
                       "isolation_level": "READ_UNCOMMITTED", "pool_size": 8},
            "tuning": {"max_branches": 64},
            "nodes": nodes, "edges": edges}


def to_yaml(draft: Dict[str, Any]) -> str:
    import yaml
    body = draft_to_mapping_dict(draft)
    header = (
        "# Written by the schema editor at /studio.\n"
        "#\n"
        "# The link names and directions below were set by a person, not guessed -\n"
        "# which matters, because a backwards link returns no rows rather than an\n"
        "# error. Re-open /studio to change them and to see live rows again.\n"
    )
    return header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100)


# ------------------------------------------------------------------ preview

def preview_edge(draft: Dict[str, Any], table: str, conn, limit: int = 5,
                 _compare: bool = True) -> Dict[str, Any]:
    """Run one link against the database, both ways round, and report honestly.

    The naive version of this only ran the current direction and said "returns data,
    looks right". That is misleading, and the misleading case is the common one: for
    an ordinary two-foreign-key link, **both directions join perfectly well**. The
    rows are the same rows; only the sentence changes. No query can tell you whether
    a Person works for an Organisation or the reverse.

    So we run it both ways and say which of four situations you are actually in:

      only this way works    - the other direction returns nothing. Settled.
      only the other works   - you have it backwards. Flip it.
      both work              - the database cannot help. Read the rows and decide
                               which sentence is true.
      neither works          - something else is wrong.

    The first two happen when flipping breaks the join: iBase record-id prefixes,
    `_LinkEnd` end markers, or key columns of different types. The third is the
    ordinary foreign-key case, and there the honest answer is "you decide" — which
    is exactly why the rows are shown with names rather than numbers.
    """
    from . import cypher_translator, mapping as mapping_mod
    from .mssql_backend import MssqlBackend
    from .node_id import NodeIdCodec

    edge = next((e for e in draft["edges"] if e["table"] == table), None)
    if edge is None:
        return {"ok": False, "error": "no link called {!r} in the draft".format(table)}
    if not edge.get("key"):
        return {"ok": False, "error": "this link has no key column of its own. Every link "
                                      "needs one, or two links between the same pair of "
                                      "records collapse into a single line."}
    try:
        raw = draft_to_mapping_dict(draft, only_edge=table)
        m = mapping_mod.from_dict(raw)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if edge["type"] not in m.rel_schemas:
        return {"ok": False, "error": "both ends must be included as record types first"}

    codec = NodeIdCodec()
    codec.register(m.label_order(), key_types=m.key_types())
    backend = MssqlBackend(m, codec, connection=conn)

    display = {n["label"]: n.get("display") for n in draft["nodes"] if n.get("include")}
    label_of_table = {n["table"]: n["label"] for n in draft["nodes"] if n.get("include")}

    out_pairs = []
    total = 0
    sql_shown = None
    for pair in m.rel_schemas[edge["type"]].endpoints:
        cypher = "MATCH (a:{})-[r:{}]->(b:{}) RETURN a,r,b LIMIT {}".format(
            pair.src_label, edge["type"], pair.dst_label, limit)
        try:
            plan = cypher_translator.translate(cypher)
            plans = backend.concrete_plans(plan)
            if not plans:
                out_pairs.append({"src": pair.src_label, "dst": pair.dst_label,
                                  "rows": [], "count": 0})
                continue
            compiled = backend.compile(plans[0])
            sql_shown = sql_shown or compiled.sql
            result = backend.execute(plan)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "sql": sql_shown}

        by_id = {n.id: n for n in result.nodes}
        rows = []
        for rel in result.relationships[:limit]:
            s, d = by_id.get(rel.startNodeId), by_id.get(rel.endNodeId)
            rows.append({
                "src": _describe(s, display), "dst": _describe(d, display),
                "src_label": s.labels[0] if s else "?",
                "dst_label": d.labels[0] if d else "?",
                "properties": {k: v for k, v in list(rel.properties.items())[:3]},
            })
        total += len(result.relationships)
        out_pairs.append({"src": pair.src_label, "dst": pair.dst_label,
                          "rows": rows, "count": len(result.relationships)})

    # How many rows are in the link table at all? A link that returns nothing when
    # its table is full is the tell-tale sign of a backwards direction.
    table_rows = None
    try:
        from . import tsql
        r = conn.run("SELECT COUNT_BIG(*) AS n FROM {}".format(
            tsql.qualify(draft.get("schema", "dbo"), table)), [])
        table_rows = int(r[0]["n"]) if r else None
    except Exception:
        pass

    # What would the other direction give? Run it on a throwaway copy of the draft.
    other = None
    if _compare:
        mirror = copy.deepcopy(draft)
        for e in mirror["edges"]:
            if e["table"] == table:
                flip(e)
        try:
            r = preview_edge(mirror, table, conn, limit, _compare=False)
            other = r.get("total") if r.get("ok") else None
        except Exception:
            other = None

    example = None
    for p in out_pairs:
        if p["rows"]:
            example = p["rows"][0]
            break

    if total and other:
        verdict = "ambiguous"
        note = ("Both directions join, so the database cannot decide this one for you - "
                "only you know which sentence is true. Read a row: ")
        if example:
            note += "\u201c{} \u2192 {}\u201d.".format(example["src"], example["dst"])
        else:
            note += "does the arrow point the way you would say it out loud?"
    elif total and not other:
        verdict = "ok"
        note = ("This direction returns {:,} row(s); the other way round returns none. "
                "Settled.".format(total))
    elif not total and other:
        verdict = "backwards"
        note = ("This direction matches nothing, but flipping it returns {:,} row(s). "
                "You have it backwards.".format(other))
    elif table_rows:
        verdict = "broken"
        note = ("The table holds {:,} rows, but neither direction matches any of them. "
                "The key columns or the endpoint prefixes are probably wrong."
                .format(table_rows))
    else:
        verdict = "empty"
        note = "The link table itself is empty, so there is nothing to check here."

    return {"ok": True, "verdict": verdict, "note": note, "total": total,
            "other_direction": other, "table_rows": table_rows,
            "pairs": out_pairs, "sql": sql_shown}


def _describe(node, display: Dict[str, Optional[str]]) -> str:
    if node is None:
        return "?"
    col = display.get(node.labels[0])
    if col and node.properties.get(col) not in (None, ""):
        return str(node.properties[col])
    return str(next(iter(node.properties.values()), node.id))


# --------------------------------------------------------- sample rows

def sample_rows(draft: Dict[str, Any], table: str, kind: str, conn,
                limit: int = 5) -> Dict[str, Any]:
    """Show real rows next to the names they will be given.

    Two names for one thing is where confusion lives: the database calls it
    `dbo.Person.full_name`, Kineviz will show it as `Person.full_name`, and if you
    rename either one you want to see both side by side while you do it.

    Unmapped columns are listed too. A column you have not included is invisible in
    Kineviz, and the usual way to discover that is to go looking for it later.
    """
    from . import tsql
    schema = draft.get("schema", "dbo")

    if kind == "node":
        item = next((n for n in draft["nodes"] if n["table"] == table), None)
        if item is None:
            return {"ok": False, "error": "no record type from table {!r}".format(table)}
        roles = {item["key"]: ("key", item["key"])}
        for prop in item["properties"]:
            roles.setdefault(prop, ("property", prop))
        name, kind_word = item["label"], "record"
    else:
        item = next((e for e in draft["edges"] if e["table"] == table), None)
        if item is None:
            return {"ok": False, "error": "no link from table {!r}".format(table)}
        roles = {}
        if item.get("key"):
            roles[item["key"]] = ("key", item["key"])
        for p in item.get("pairs", []):
            if p.get("src_column"):
                roles[p["src_column"]] = ("source end", "\u2192 {}".format(p["src_label"]))
            if p.get("dst_column"):
                roles[p["dst_column"]] = ("target end", "\u2192 {}".format(p["dst_label"]))
        for prop in item.get("properties", []):
            roles.setdefault(prop, ("property", prop))
        name, kind_word = item["type"], "link"

    try:
        rows = conn.run("SELECT TOP ({:d}) * FROM {}".format(
            int(limit), tsql.qualify(schema, table)), [])
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    order = list(rows[0].keys()) if rows else list((item.get("types") or {}).keys())
    types = item.get("types") or {}
    columns = []
    for col in order:
        role, shown = roles.get(col, ("not mapped", None))
        columns.append({"column": col, "role": role, "shown_as": shown,
                        "type": types.get(col, ""), "mapped": col in roles})
    return {"ok": True, "table": "{}.{}".format(schema, table), "name": name,
            "kind": kind_word,
            "columns": columns,
            "rows": [[_cell(r.get(c["column"])) for c in columns] for r in rows],
            "unmapped": [c["column"] for c in columns if not c["mapped"]]}


def _cell(v: Any) -> str:
    if v is None:
        return ""
    t = str(v)
    return t if len(t) <= 60 else t[:57] + "\u2026"


# ------------------------------------------------------------------- routes

def _status(state) -> Dict[str, Any]:
    """Everything a person needs to answer "is this thing working?" in one place."""
    problems: List[Dict[str, str]] = []
    db = {"connected": state.connection is not None}
    if state.connection is None:
        problems.append({"level": "warn", "text":
                         "No database connection. The bridge was started with "
                         "--compile-only, so it can show you the SQL it would run but "
                         "cannot answer queries."})
    else:
        try:
            state.connection.run("SELECT 1 AS n", [])
            db["reachable"] = True
            db["server_major"] = state.connection.server_major
            db["openjson"] = state.connection.server_major >= 13
        except Exception as exc:
            db["reachable"] = False
            problems.append({"level": "error", "text":
                             "The database is not answering: {}".format(exc)})
    if state.last_error:
        problems.append({"level": "error",
                         "text": "Last query failed: {}".format(state.last_error)})
    if state.codec is not None and not state.codec.is_stateless():
        problems.append({"level": "warn", "text":
                         "Some record types have keys that are neither numbers nor a "
                         "prefix followed by digits, so their node ids are handed out in "
                         "order and will change if this bridge restarts. Start it with "
                         "--id-state <file> to keep them."})
    if state.backend is not None and not state.backend.rel_schemas:
        problems.append({"level": "warn", "text":
                         "No links are mapped, so Kineviz will show dots with no lines "
                         "between them."})

    level = "error" if any(p["level"] == "error" for p in problems) else (
        "warn" if problems else "ok")
    return {
        "level": level,
        "problems": problems,
        "database": db,
        "mapping_path": state.mapping_path,
        "db_name": state.db_name,
        "url": state.public_url,
        "serving": {"labels": state.backend.labels(),
                    "types": state.backend.rel_types()} if state.backend else None,
        "queries": state.query_count,
        "last_query": state.last_query,
    }


def build_router(state):
    """Mount the editor. `state` is the server's BridgeState (see ibase_server)."""
    from fastapi import APIRouter, Body
    from fastapi.responses import HTMLResponse

    router = APIRouter(prefix="/studio")

    @router.get("", response_class=HTMLResponse)
    @router.get("/", response_class=HTMLResponse)
    async def page():
        return PAGE

    @router.get("/api/state")
    async def get_state():
        return {"draft": state.draft, "mapping_path": state.mapping_path,
                "status": _status(state)}

    @router.get("/api/status")
    async def status():
        return _status(state)

    @router.post("/api/sample")
    async def sample(body: Dict[str, Any] = Body(...)):
        if state.connection is None:
            return {"ok": False, "error": "not connected to a database"}
        if body.get("draft"):
            state.draft = body["draft"]
        return sample_rows(state.draft, body.get("table"), body.get("kind", "node"),
                           state.connection)

    @router.post("/api/discover")
    async def discover():
        from . import discovery
        if state.connection is None:
            return {"ok": False, "error": "the bridge is not connected to a database. "
                                          "Start it without --compile-only."}
        try:
            schema = state.mapping.schema if state.mapping else "dbo"
            catalog = discovery.read_catalog(state.connection, schema)
            found = discovery.classify(catalog, ibase_mode=True)
            le = discovery.probe_link_end(state.connection, schema)
            if le and all(le["guessed"].values()):
                spec = dict(le["guessed"], table=le["table"])
                link_tables = discovery.link_tables_from_link_end(
                    state.connection, schema, le["table"], le["guessed"]["link_table_column"])
                pairs, prefixes = {}, set()
                for lt in link_tables:
                    try:
                        rows = discovery.discover_endpoint_pairs(state.connection, schema, lt, spec)
                        pairs[lt] = rows
                        for r in rows:
                            prefixes.add(r.get("src_prefix"))
                            prefixes.add(r.get("dst_prefix"))
                    except Exception:
                        pairs[lt] = []
                prefix_map = discovery.map_prefixes_to_labels(
                    state.connection, schema, found["nodes"], sorted(p for p in prefixes if p))
                found = discovery.apply_link_end_evidence(found, link_tables, pairs, prefix_map)
            state.draft = draft_from_discovery(found, schema, le,
                                               state.mapping.source.get("connection_env")
                                               if state.mapping else "IBASE_CONNECTION_STRING")
            return {"ok": True, "draft": state.draft}
        except Exception as exc:
            logger.exception("discovery failed")
            return {"ok": False, "error": str(exc)}

    @router.post("/api/draft")
    async def put_draft(draft: Dict[str, Any] = Body(...)):
        state.draft = draft
        return {"ok": True}

    @router.post("/api/flip")
    async def flip_edge(body: Dict[str, Any] = Body(...)):
        table = body.get("table")
        for e in (state.draft or {}).get("edges", []):
            if e["table"] == table:
                flip(e)
                return {"ok": True, "edge": e}
        return {"ok": False, "error": "no link called {!r}".format(table)}

    @router.post("/api/preview")
    async def preview(body: Dict[str, Any] = Body(...)):
        if state.connection is None:
            return {"ok": False, "error": "not connected to a database"}
        if body.get("draft"):
            state.draft = body["draft"]
        return preview_edge(state.draft, body.get("table"), state.connection)

    @router.post("/api/save")
    async def save(body: Dict[str, Any] = Body(...)):
        if body.get("draft"):
            state.draft = body["draft"]
        path = body.get("path") or state.mapping_path
        try:
            text = to_yaml(state.draft)
            from . import mapping as mapping_mod
            import yaml as _yaml
            mapping_mod.from_dict(_yaml.safe_load(text))      # refuse to write something broken
            if os.path.exists(path):
                backup = path + ".bak"
                with open(backup, "w") as fh:
                    fh.write(open(path).read())
            with open(path, "w") as fh:
                fh.write(text)
            return {"ok": True, "path": path, "yaml": text}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.get("/api/yaml")
    async def yaml_preview():
        try:
            return {"ok": True, "yaml": to_yaml(state.draft)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @router.post("/api/reload")
    async def reload():
        try:
            before = state.backend.labels() + state.backend.rel_types() if state.backend else []
            warn = state.reload()
            after = state.backend.labels() + state.backend.rel_types()
            # The first half of every node id is a label's position in this list. If
            # the list changed shape, ids Kineviz is already holding now mean
            # something else, and the canvas has to be reloaded.
            ids_moved = before and (before != after)
            return {"ok": True, "serving": {"labels": state.backend.labels(),
                                            "types": state.backend.rel_types()},
                    "ids_moved": bool(ids_moved), "warning": warn}
        except Exception as exc:
            logger.exception("reload failed")
            return {"ok": False, "error": str(exc)}

    return router


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>iBase Bridge - schema editor</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--ink:#e6e9ef;--dim:#98a0b0;
--ok:#3ecf8e;--warn:#f0a13a;--bad:#f0605d;--accent:#6aa3ff;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
@media(prefers-color-scheme:light){:root{--bg:#f6f7f9;--panel:#fff;--line:#e2e5ea;--ink:#1a1d23;--dim:#666e7d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:16px;flex-wrap:wrap}
h1{font-size:17px;margin:0;font-weight:600}
.sub{color:var(--dim);font-size:13px}
main{padding:20px 24px;max-width:1080px;margin:0 auto}
button{background:var(--panel);color:var(--ink);border:1px solid var(--line);border-radius:7px;
padding:6px 12px;font:inherit;font-size:13px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#08101f;border-color:var(--accent);font-weight:600}
button:disabled{opacity:.45;cursor:default}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.between{justify-content:space-between}
input[type=text]{background:var(--bg);color:var(--ink);border:1px solid var(--line);
border-radius:6px;padding:5px 9px;font:inherit;font-size:13px}
input[type=text].name{font-weight:600;min-width:190px}
.arrow{font-family:var(--mono);font-size:14px;white-space:nowrap}
.pill{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--line);color:var(--dim)}
.ok{color:var(--ok);border-color:var(--ok)} .warn{color:var(--warn);border-color:var(--warn)}
.bad{color:var(--bad);border-color:var(--bad)}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
td,th{text-align:left;padding:5px 10px;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:12px}
.mono{font-family:var(--mono);font-size:12px}
.dim{color:var(--dim)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:12px;
overflow:auto;font-size:12px;max-height:340px}
.note{font-size:13px;padding:8px 12px;border-radius:7px;border:1px solid var(--line);margin-top:10px}
.note.ok{background:rgba(62,207,142,.09);color:var(--ok);border-color:var(--ok)}
.note.backwards{background:rgba(240,161,58,.10);color:var(--warn);border-color:var(--warn)}
.note.ambiguous{background:rgba(106,163,255,.10);color:var(--accent);border-color:var(--accent)}
.note.broken{background:rgba(240,96,93,.10);color:var(--bad);border-color:var(--bad)}
.note.empty{background:rgba(152,160,176,.08);color:var(--dim)}
.note.err{background:rgba(240,96,93,.10);color:var(--bad);border-color:var(--bad)}
h2{font-size:14px;margin:26px 0 4px;font-weight:600}
h2 .dim{font-weight:400}
label.chk{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--dim);cursor:pointer}
.excluded{opacity:.42}
#toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--panel);
border:1px solid var(--line);border-radius:8px;padding:10px 18px;font-size:13px;display:none;z-index:9}
.dot{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:7px;flex:none}
.dot.ok{background:var(--ok)} .dot.warn{background:var(--warn)} .dot.error{background:var(--bad)}
.grid{display:grid;grid-template-columns:150px 1fr;gap:5px 14px;font-size:13px;margin-top:4px}
.grid b{font-weight:500;color:var(--dim)}
.url{font-family:var(--mono);font-size:13px;background:var(--bg);border:1px solid var(--line);
border-radius:6px;padding:6px 10px;user-select:all;display:inline-block}
.steps{font-size:13px;margin:8px 0 0;padding-left:20px} .steps li{margin:3px 0}
.problem{font-size:13px;padding:7px 11px;border-radius:6px;margin-top:7px;border:1px solid}
.problem.warn{color:var(--warn);border-color:var(--warn);background:rgba(240,161,58,.08)}
.problem.error{color:var(--bad);border-color:var(--bad);background:rgba(240,96,93,.08)}
td.unmapped,th.unmapped{opacity:.45}
.role{font-size:11px;color:var(--dim)}
.tabs{display:flex;gap:4px;margin-top:9px}
.tabs button{font-size:12px;padding:4px 10px}
.tabs button[aria-pressed=true]{border-color:var(--accent);color:var(--accent)}
</style></head><body>
<header>
  <h1>iBase Bridge &mdash; schema editor</h1>
  <span class="sub" id="hdr"><span class="dot"></span>loading&hellip;</span>
  <span style="flex:1"></span>
  <button id="btn-discover">Read the database</button>
  <button id="btn-yaml">Show the file</button>
  <button id="btn-save" class="primary">Save mapping</button>
  <button id="btn-reload">Reload bridge</button>
</header>
<main>
  <div class="card" id="status"></div>
  <div class="card" id="connect"></div>
  <div class="card" id="intro">
    <b>Two things your database cannot tell us: what to call a link, and which way it points.</b>
    <div class="sub" style="margin-top:6px">
      Direction is the one that bites. Get it backwards and nothing breaks &mdash; queries just
      come back empty, which reads as &ldquo;nothing matched&rdquo;. So every link below is run
      <b>both ways</b> against your real data. Sometimes only one way returns rows, and then the
      answer is settled. Often both do &mdash; and then no query can decide it, so you read a row
      and choose the sentence that is true.
    </div>
  </div>
  <div id="links"></div>
  <div id="records"></div>
  <div id="leftover"></div>
</main>
<div id="toast"></div>
<script>
let D=null;
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(m,ms=2600){const t=$("#toast");t.textContent=m;t.style.display="block";
  clearTimeout(t._t);t._t=setTimeout(()=>t.style.display="none",ms);}
async function api(p,b){const r=await fetch("/studio/api/"+p,b?{method:"POST",
  headers:{"Content-Type":"application/json"},body:JSON.stringify(b)}:{});return r.json();}

async function boot(){const s=await api("state");D=s.draft;paintStatus(s.status);render();
  setInterval(async()=>{try{paintStatus(await api("status"));}catch(e){}},5000);}

function paintStatus(st){
  if(!st)return;
  const word={ok:"running",warn:"running, with something to look at",error:"there is a problem"}[st.level];
  // Replace the whole header span. Setting .innerHTML here destroys the dot element,
  // so do not hold a reference to it across calls - this polls every few seconds.
  $("#hdr").innerHTML='<span class="dot '+st.level+'"></span>'+esc(word);
  const db=st.database||{};
  $("#status").innerHTML=
    '<div class="row between"><b>Bridge status</b>'+
      '<span class="dim" style="font-size:12px">'+esc(st.queries||0)+' quer'+
      ((st.queries===1)?"y":"ies")+' answered</span></div>'+
    '<div class="grid" style="margin-top:8px">'+
      '<b>state</b><span><span class="dot '+st.level+'"></span>'+esc(word)+"</span>"+
      "<b>database</b><span>"+(db.connected?
        (db.reachable?("answering \u00b7 SQL Server major version "+esc(db.server_major)+
          (db.openjson?"":" \u2014 too old for OPENJSON, so long selections are sent in batches")):
          "connected but not answering"):"not connected (--compile-only)")+"</span>"+
      "<b>serving</b><span>"+(st.serving?
        (st.serving.labels.length+" record types, "+st.serving.types.length+" link types"):"nothing")+"</span>"+
      "<b>mapping file</b><span class=mono>"+esc(st.mapping_path)+"</span>"+
      (st.last_query?"<b>last query</b><span class='mono dim'>"+esc(st.last_query)+"</span>":"")+
    "</div>"+
    (st.problems||[]).map(p=>'<div class="problem '+p.level+'">'+
      (p.level==="error"?"&#10007; ":"&#9888; ")+esc(p.text)+"</div>").join("");

  const u=st.url||"";
  $("#connect").innerHTML='<b>Connect Kineviz to this bridge</b>'+
    '<ol class="steps">'+
    "<li>Open the Kineviz <b>desktop app</b>. A browser tab served over HTTPS will refuse to "+
      "call a plain-http address on this machine, and the failure looks like the bridge is broken.</li>"+
    "<li><b>Create \u2192 Create New Project</b></li>"+
    "<li><b>Database Type:</b> <code>KoreDB Via Proxy API</code> \u2014 not <code>Database Proxy</code>, "+
      "which fails its connection check for reasons on the GraphXR side.</li>"+
    '<li><b>Proxy API URL:</b> <span class="url" id="u">'+esc(u)+
      '</span> <button id="copy">Copy</button></li>'+
    "<li>Confirm. You should see "+(st.serving?st.serving.labels.length:0)+" record types and "+
      (st.serving?st.serving.types.length:0)+" link types in the schema panel.</li></ol>"+
    '<div class="dim" style="font-size:12px;margin-top:7px">No username or password: the bridge '+
    "holds the database credentials and Kineviz never sees them.</div>";
  const c=$("#copy");if(c)c.onclick=()=>{navigator.clipboard.writeText(u);toast("copied "+u);};
}

function render(){
  const L=$("#links"),R=$("#records"),X=$("#leftover");
  if(!D){L.innerHTML='<div class="card dim">Press <b>Read the database</b> to begin.</div>';
    R.innerHTML="";X.innerHTML="";return;}
  L.innerHTML='<h2>Links <span class="dim">&mdash; check the direction of each one</span></h2>'+
    (D.edges.length?D.edges.map(edgeCard).join(""):'<div class="card dim">No link tables found.</div>');
  R.innerHTML='<h2>Records</h2>'+D.nodes.map(nodeCard).join("");
  const bits=[];
  if(D.ambiguous&&D.ambiguous.length)bits.push("<b>Not mapped, needs a decision</b><ul>"+
    D.ambiguous.map(a=>"<li class=mono>"+esc(a.table)+" &mdash; "+esc(a.why)+"</li>").join("")+"</ul>");
  if(D.skipped&&D.skipped.length)bits.push("<b>Skipped on purpose</b><ul>"+
    D.skipped.map(a=>"<li class=mono>"+esc(a.table)+" &mdash; "+esc(a.why)+"</li>").join("")+"</ul>");
  X.innerHTML=bits.length?'<h2>Everything else</h2><div class="card dim" style="font-size:13px">'+bits.join("")+"</div>":"";
}

function edgeCard(e,i){
  const pairs=e.pairs.map(p=>'<span class="arrow">'+esc(p.src_label)+
    ' \\u2500\\u2500\\u25b6 '+esc(p.dst_label)+'</span>').join('<span class="dim"> &nbsp;/&nbsp; </span>');
  return '<div class="card'+(e.include?"":" excluded")+'" data-t="'+esc(e.table)+'">'+
   '<div class="row between"><div class="row">'+
     '<input type="text" class="name" value="'+esc(e.type)+'" data-act="rename-edge" data-i="'+i+'">'+
     '<span class="dim mono">from '+esc(e.table)+'</span>'+
     (e.self_referencing?'<span class="pill">both ends the same</span>':"")+
     (e.resolution!=="fk"?'<span class="pill">'+esc(e.resolution)+'</span>':"")+
     '<span class="pill">'+Number(e.rows||0).toLocaleString()+' rows</span>'+
   '</div><div class="row">'+
     '<label class="chk"><input type="checkbox" data-act="inc-edge" data-i="'+i+'"'+
       (e.include?" checked":"")+'> include</label>'+
     '<button data-act="flip" data-i="'+i+'">&#8646; Flip direction</button>'+
     '<button data-act="preview" data-i="'+i+'">Check direction</button>'+
     '<button data-act="data-edge" data-i="'+i+'">Table data</button>'+
   '</div></div>'+
   '<div class="row" style="margin-top:9px">'+pairs+'</div>'+
   (e.why?'<div class="sub" style="margin-top:6px">'+esc(e.why)+'</div>':"")+
   '<div id="pv-'+i+'"></div></div>';
}

function nodeCard(n,i){
  return '<div class="card'+(n.include?"":" excluded")+'"><div class="row between"><div class="row">'+
   '<input type="text" class="name" value="'+esc(n.label)+'" data-act="rename-node" data-i="'+i+'">'+
   '<span class="dim mono">from '+esc(n.table)+', key '+esc(n.key)+'</span>'+
   '<span class="pill">'+Number(n.rows||0).toLocaleString()+' rows</span>'+
   '</div><div class="row">'+
   '<span class="dim" style="font-size:12px">show as</span>'+
   '<select data-act="display" data-i="'+i+'">'+
     n.properties.map(p=>'<option'+(p===n.display?" selected":"")+'>'+esc(p)+'</option>').join("")+
   '</select>'+
   '<button data-act="data-node" data-i="'+i+'">Table data</button>'+
   '<label class="chk"><input type="checkbox" data-act="inc-node" data-i="'+i+'"'+
     (n.include?" checked":"")+'> include</label>'+
   '</div></div><div id="dn-'+i+'"></div></div>';
}

async function preview(i){
  const e=D.edges[i],box=document.getElementById("pv-"+i);
  box.innerHTML='<div class="note dim">asking the database&hellip;</div>';
  const r=await api("preview",{table:e.table,draft:D});
  if(!r.ok){box.innerHTML='<div class="note err">'+esc(r.error)+"</div>";return;}
  const icon={ok:"&#10003; ",backwards:"&#9888; ",ambiguous:"&#9998; ",broken:"&#10007; ",empty:""}[r.verdict]||"";
  let h='<div class="note '+r.verdict+'">'+icon+esc(r.note)+"</div>";
  for(const p of r.pairs){
    if(!p.rows.length)continue;
    h+='<table><tr><th>'+esc(p.src)+'</th><th></th><th>'+esc(p.dst)+"</th><th></th></tr>"+
      p.rows.map(row=>"<tr><td>"+esc(row.src)+'</td><td class="dim">&#8594;</td><td>'+esc(row.dst)+
        '</td><td class="dim mono">'+esc(Object.entries(row.properties).map(([k,v])=>k+"="+v).join("  "))+
        "</td></tr>").join("")+"</table>";
  }
  box.innerHTML=h;
}

async function showData(kind,i){
  const item=kind==="node"?D.nodes[i]:D.edges[i];
  const box=document.getElementById((kind==="node"?"dn-":"pv-")+i);
  box.innerHTML='<div class="note dim">reading '+esc(item.table)+'&hellip;</div>';
  const r=await api("sample",{table:item.table,kind:kind,draft:D});
  if(!r.ok){box.innerHTML='<div class="note err">'+esc(r.error)+"</div>";return;}
  let h='<div class="note dim">Real rows from <span class="mono">'+esc(r.table)+
    '</span>, with the names Kineviz will use for them. '+
    (r.unmapped.length?("<b>"+r.unmapped.length+"</b> column(s) are not mapped, so they will "+
      "not appear in Kineviz: <span class=mono>"+esc(r.unmapped.join(", "))+"</span>"):
      "Every column is mapped.")+"</div>";
  h+="<table><tr>"+r.columns.map(c=>'<th class="'+(c.mapped?"":"unmapped")+'">'+
      esc(c.column)+'<div class="role">'+esc(c.type||"")+
      (c.mapped?(" \u00b7 "+esc(c.role)+(c.shown_as&&c.shown_as!==c.column?" \u00b7 "+esc(c.shown_as):"")):
        " \u00b7 not mapped")+"</div></th>").join("")+"</tr>";
  h+=r.rows.map(row=>"<tr>"+row.map((v,j)=>'<td class="'+(r.columns[j].mapped?"":"unmapped")+'">'+
      esc(v)+"</td>").join("")+"</tr>").join("")+"</table>";
  box.innerHTML=h;
}

document.addEventListener("click",async ev=>{
  const t=ev.target.closest("[data-act]");if(!t)return;
  const i=+t.dataset.i,a=t.dataset.act;
  if(a==="flip"){D.edges[i]=(await api("flip",{table:D.edges[i].table})).edge;render();preview(i);}
  else if(a==="preview")preview(i);
  else if(a==="data-node")showData("node",i);
  else if(a==="data-edge")showData("edge",i);
  else if(a==="inc-edge"){D.edges[i].include=t.checked;render();}
  else if(a==="inc-node"){D.nodes[i].include=t.checked;render();}
});
document.addEventListener("change",ev=>{
  const t=ev.target.closest("[data-act]");if(!t)return;
  const i=+t.dataset.i,a=t.dataset.act;
  if(a==="rename-edge")D.edges[i].type=t.value.trim()||D.edges[i].type;
  else if(a==="rename-node"){const old=D.nodes[i].label;D.nodes[i].label=t.value.trim()||old;
    D.edges.forEach(e=>e.pairs.forEach(p=>{if(p.src_label===old)p.src_label=D.nodes[i].label;
      if(p.dst_label===old)p.dst_label=D.nodes[i].label;}));render();}
  else if(a==="display")D.nodes[i].display=t.value;
});

$("#btn-discover").onclick=async()=>{toast("reading the database\\u2026",9000);
  const r=await api("discover",{});
  if(!r.ok){toast("could not read it: "+r.error,6000);return;}
  D=r.draft;render();toast("found "+D.nodes.length+" record types and "+D.edges.length+" link types");};

$("#btn-yaml").onclick=async()=>{await api("draft",D);const r=await api("yaml");
  const box=$("#intro");
  box.innerHTML=r.ok?"<b>This is what will be written.</b><pre>"+esc(r.yaml)+"</pre>":
    '<div class="note err">'+esc(r.error)+"</div>";};

$("#btn-save").onclick=async()=>{const r=await api("save",{draft:D});
  toast(r.ok?"saved to "+r.path+" (previous kept as .bak)":"not saved: "+r.error,5000);};

$("#btn-reload").onclick=async()=>{const r=await api("reload",{});
  if(!r.ok){toast("reload failed: "+r.error,6000);return;}
  paintStatus(await api("status"));
  toast(r.ids_moved?"reloaded \u2014 the record types changed, so node ids moved. Reload the graph in Kineviz.":
    "reloaded \u2014 now serving the saved mapping",6000);};

boot();
</script></body></html>
"""
