"""Read a SQL Server database and *propose* a mapping.

The output is a draft for a person to read, never something the bridge loads on its
own. That is deliberate, and it is what the connector specification asks for: real
iBase deployments differ by version, configuration and local customisation, and i2
does not publish the physical column names, so anything inferred here is a
suggestion with a confidence attached.

The rule for telling a line from a dot is simpler than it first looks:

    A table that points at two others, and that nothing points back at,
    is almost certainly a link. Everything else is almost certainly a record.

"Nothing points at it" is the strong half. A link table is a leaf — no foreign key
targets it. Counting columns is a bad test, because a perfectly ordinary link table
like `Employment` also carries a job title and two dates.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

SYSTEM_PREFIX = "_"
EXCLUDE_FAMILIES = ("_AL_", "_FTS_")

# Audit, security and attachment tables are never proposed. The specification is
# explicit that this data is not extracted unless an operator maps it deliberately.
NEVER_PROPOSE = re.compile(r"^_?(AL_|FTS_|Audit|Security|SecurityGroup|User|Password|"
                           r"Attachment|Case|Folder)", re.IGNORECASE)

TYPE_MAP = {
    "bigint": "bigint", "int": "int", "smallint": "smallint", "tinyint": "tinyint",
    "bit": "bit", "decimal": "decimal", "numeric": "decimal", "money": "decimal",
    "float": "float", "real": "float",
    "date": "date", "datetime": "datetime2", "datetime2": "datetime2",
    "smalldatetime": "datetime2", "time": "time", "datetimeoffset": "datetimeoffset",
    "char": "char", "varchar": "varchar", "nchar": "nchar", "nvarchar": "nvarchar",
    "text": "text", "ntext": "ntext", "uniqueidentifier": "uniqueidentifier",
}

CATALOG_SQL = {
    "tables": """
SELECT s.name AS [schema], t.name AS [table],
       SUM(CASE WHEN p.index_id IN (0,1) THEN p.rows ELSE 0 END) AS [rows]
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
LEFT JOIN sys.partitions p ON p.object_id = t.object_id
GROUP BY s.name, t.name
ORDER BY s.name, t.name""",
    "columns": """
SELECT s.name AS [schema], t.name AS [table], c.name AS [column],
       ty.name AS [type], c.max_length AS [max_length], c.is_nullable AS [nullable],
       c.collation_name AS [collation], c.column_id AS [ordinal]
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
ORDER BY s.name, t.name, c.column_id""",
    "primary_keys": """
SELECT s.name AS [schema], t.name AS [table], c.name AS [column], ic.key_ordinal AS [ordinal]
FROM sys.indexes i
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE i.is_primary_key = 1
ORDER BY s.name, t.name, ic.key_ordinal""",
    "foreign_keys": """
SELECT s.name AS [schema], t.name AS [table], pc.name AS [column],
       rs.name AS [ref_schema], rt.name AS [ref_table], rc.name AS [ref_column],
       fk.name AS [fk_name]
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.tables t  ON t.object_id  = fk.parent_object_id
JOIN sys.schemas s ON s.schema_id  = t.schema_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id
JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
ORDER BY s.name, t.name, fk.name""",
}


def read_catalog(conn, schema: str = "dbo") -> Dict[str, Any]:
    """Plain facts about the database. No interpretation, no iBase assumptions."""
    out: Dict[str, Any] = {}
    for name, sql in CATALOG_SQL.items():
        rows = conn.run(sql, [])
        out[name] = [r for r in rows if (r.get("schema") or "dbo") == schema]
    return out


def _index(catalog) -> Tuple[Dict, Dict, Dict, Dict]:
    cols: Dict[str, List[Dict]] = {}
    for r in catalog["columns"]:
        cols.setdefault(r["table"], []).append(r)
    pks: Dict[str, List[str]] = {}
    for r in catalog["primary_keys"]:
        pks.setdefault(r["table"], []).append(r["column"])
    fks_out: Dict[str, List[Dict]] = {}
    referenced: Dict[str, int] = {}
    for r in catalog["foreign_keys"]:
        fks_out.setdefault(r["table"], []).append(r)
        referenced[r["ref_table"]] = referenced.get(r["ref_table"], 0) + 1
    return cols, pks, fks_out, referenced


def classify(catalog: Dict[str, Any], ibase_mode: bool = True) -> Dict[str, Any]:
    """Sort tables into probable nodes, probable edges, and things to look at."""
    cols, pks, fks_out, referenced = _index(catalog)
    rows_of = {r["table"]: (r.get("rows") or 0) for r in catalog["tables"]}

    nodes, edges, ambiguous, skipped = [], [], [], []

    for t in catalog["tables"]:
        name = t["table"]
        if name.startswith(SYSTEM_PREFIX):
            skipped.append({"table": name, "why": "system table (name begins with '_')"})
            continue
        if ibase_mode and NEVER_PROPOSE.match(name):
            skipped.append({"table": name,
                            "why": "audit, security or attachment data is never proposed"})
            continue

        out_fks = fks_out.get(name, [])
        # Count foreign key COLUMNS, not distinct target tables. A transfer table
        # points at Account twice - two ends, one table - and counting tables would
        # call it a record. Self-referencing links (transfers, associations,
        # communications) are among the most common shapes there are.
        targets = sorted({f["ref_table"] for f in out_fks})
        fk_cols = [f for f in out_fks]
        n_ends = len(fk_cols)
        self_ref = len(targets) == 1 and n_ends == 2
        is_referenced = referenced.get(name, 0) > 0
        pk = pks.get(name, [])

        if n_ends == 2 and not is_referenced:
            edges.append({"table": name, "key": pk[0] if pk else None,
                          "targets": targets, "fks": out_fks, "rows": rows_of.get(name, 0),
                          "self_referencing": self_ref,
                          "confidence": "high" if pk else "medium",
                          "why": ("both ends point at {} and nothing points back at it "
                                  "- an undirected query on this will look at the table "
                                  "twice".format(targets[0])) if self_ref else
                                 "points at {} and {}, and nothing points back at it"
                                 .format(targets[0], targets[-1])})
        elif n_ends == 2 and is_referenced:
            ambiguous.append({"table": name, "targets": targets, "rows": rows_of.get(name, 0),
                              "why": "has two foreign keys but other tables point at it too "
                                     "- this may be a record that happens to have two links"})
        elif n_ends > 2:
            ambiguous.append({"table": name, "targets": targets, "rows": rows_of.get(name, 0),
                              "why": "has {} foreign keys; a link has exactly two ends, so "
                                     "this is probably a record".format(n_ends)})
        else:
            if not pk:
                ambiguous.append({"table": name, "rows": rows_of.get(name, 0),
                                  "why": "no primary key, so there is no stable id to build on"})
                continue
            if len(pk) > 1:
                ambiguous.append({"table": name, "rows": rows_of.get(name, 0),
                                  "why": "composite primary key ({}), which is not supported yet"
                                         .format(", ".join(pk))})
                continue
            nodes.append({"table": name, "key": pk[0], "rows": rows_of.get(name, 0),
                          "columns": [c["column"] for c in cols.get(name, [])],
                          "types": {c["column"]: TYPE_MAP.get(c["type"], c["type"])
                                    for c in cols.get(name, [])},
                          "confidence": "high" if is_referenced else "medium",
                          "why": "referenced by other tables" if is_referenced
                                 else "not a link candidate"})

    # Fill in edge column details now that we know which tables are nodes.
    by_table = {n["table"]: n for n in nodes}
    ordinal = {(r["table"], r["column"]): r["ordinal"] for r in catalog["columns"]}
    for e in edges:
        # The catalog returns foreign keys in no meaningful order, so put them in
        # the order the columns appear in the table. That is arbitrary too, but it
        # is at least stable between runs and usually matches how a person reads the
        # table (Employment lists person_id before organization_id). The direction
        # is still a guess, and the draft says so.
        e["fks"] = sorted(e["fks"], key=lambda f: ordinal.get((e["table"], f["column"]), 0))
        e["types"] = {c["column"]: TYPE_MAP.get(c["type"], c["type"])
                      for c in cols.get(e["table"], [])}
        fk_cols = {f["column"] for f in e["fks"]}
        e["properties"] = [c["column"] for c in cols.get(e["table"], [])
                           if c["column"] not in fk_cols and c["column"] != e["key"]]
        e["resolvable"] = all(t in by_table for t in e["targets"])
        if e.get("self_referencing"):
            e["note"] = ("both ends are {} - check that src and dst are the right way "
                         "round before using this".format(e["targets"][0]))
    return {"nodes": nodes, "edges": edges, "ambiguous": ambiguous, "skipped": skipped}


def apply_link_end_evidence(found: Dict[str, Any], link_tables: List[str],
                            pairs_by_table: Optional[Dict[str, List[Dict]]] = None,
                            prefix_to_label: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Move tables that `_LinkEnd` names as links out of the "records" pile.

    In a real iBase database the endpoints are usually not foreign keys, so the
    foreign-key rule cannot see a link table at all and calls it a record. But
    `_LinkEnd` states the link type of every endpoint row it holds. That is
    evidence from the data itself, and it beats an inference from the schema.
    """
    names = set(link_tables or [])
    if not names:
        return found
    keep, moved = [], []
    for n in found["nodes"]:
        (moved if n["table"] in names else keep).append(n)
    found["nodes"] = keep

    pairs_by_table = pairs_by_table or {}
    prefix_to_label = prefix_to_label or {}
    for n in moved:
        observed = pairs_by_table.get(n["table"]) or []
        endpoints = []
        for row in observed:
            if not isinstance(row, dict):
                continue
            src = prefix_to_label.get(row.get("src_prefix"))
            dst = prefix_to_label.get(row.get("dst_prefix"))
            if src and dst:
                endpoints.append({"src_label": src, "dst_label": dst,
                                  "src_prefix": row.get("src_prefix"),
                                  "dst_prefix": row.get("dst_prefix"),
                                  "rows": row.get("n", 0)})
        found["edges"].append({
            "table": n["table"], "key": n["key"], "targets": [],
            "fks": [], "rows": n["rows"], "resolution": "link_end",
            "types": n.get("types", {}),
            "properties": [c for c in n.get("columns", []) if c != n["key"]],
            "endpoints": endpoints, "resolvable": bool(endpoints),
            "confidence": "high",
            "why": "_LinkEnd names it as a link table" +
                   (" and the data shows {} endpoint pair(s)".format(len(endpoints))
                    if endpoints else " (but no endpoint rows were found)")})
    found["edges"].sort(key=lambda e: e["table"])
    return found


def map_prefixes_to_labels(conn, schema: str, nodes: List[Dict[str, Any]],
                           prefixes: List[str], prefix_len: int = 3) -> Dict[str, str]:
    """Work out which record type each id prefix belongs to, by asking the data.

    For each prefix, look for a table whose primary key actually contains ids
    starting with it. No naming conventions assumed.
    """
    out: Dict[str, str] = {}
    for prefix in [p for p in prefixes if p]:
        for n in nodes:
            try:
                rows = conn.run("SELECT TOP 1 1 AS hit FROM [{}].[{}] WHERE [{}] LIKE ?"
                                .format(schema, n["table"], n["key"]), [prefix + "%"])
            except Exception:
                continue
            if rows:
                out[prefix] = n["table"]
                break
    return out


# ---------------------------------------------------------------- iBase extras

def link_tables_from_link_end(conn, schema: str = "dbo", table: str = "_LinkEnd",
                              link_table_column: str = "LinkTable") -> List[str]:
    """Ask `_LinkEnd` which tables are link tables.

    In a real iBase database the endpoints usually are not foreign keys at all, so
    the foreign-key heuristic finds nothing. But `_LinkEnd` names the link type of
    every endpoint row it holds, which is a direct, data-backed answer rather than
    an inference.
    """
    try:
        rows = conn.run("SELECT DISTINCT [{}] AS t FROM [{}].[{}]".format(
            link_table_column, schema, table), [])
    except Exception:
        return []
    return sorted({r["t"] for r in rows if r.get("t")})


def probe_link_end(conn, schema: str = "dbo", table: str = "_LinkEnd") -> Optional[Dict[str, Any]]:
    """Look at the real `_LinkEnd`, if there is one, and report its actual columns.

    We never assume these names. i2 confirms the table exists and holds link
    endpoints but does not publish its columns, and they vary between versions.
    """
    rows = conn.run("""
SELECT c.name AS [column], ty.name AS [type]
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
JOIN sys.schemas s ON s.schema_id = t.schema_id
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE s.name = ? AND t.name = ?
ORDER BY c.column_id""", [schema, table])
    if not rows:
        return None
    names = [r["column"] for r in rows]

    def guess(*candidates):
        for c in candidates:
            for n in names:
                if n.lower() == c.lower():
                    return n
        return None

    return {
        "table": table,
        "columns": names,
        "guessed": {
            "link_table_column": guess("LinkTable", "LinkType", "TableName"),
            "link_id_column": guess("LinkId", "LinkID", "Link_ID"),
            "end_column": guess("End", "EndNum", "EndIndex"),
            "record_id_column": guess("RecordId", "RecordID", "EntityId"),
        },
        "note": ("These are guesses matched against the column names actually present. "
                 "Check them against your iBase design report before using this mapping."),
    }


def discover_endpoint_pairs(conn, schema: str, link_table: str, le: Dict[str, str],
                            prefix_len: int = 3) -> List[Dict[str, Any]]:
    """Ask the data which kinds of record a link type actually joins.

    This is the query that turns a polymorphic iBase link into a concrete list of
    endpoint pairs, with counts, so a person can prune the long tail (a pair with
    three rows out of two million is a typo, not a schema fact).
    """
    sql = """
SELECT LEFT(a.[{rid}], {n}) AS src_prefix,
       LEFT(b.[{rid}], {n}) AS dst_prefix,
       COUNT_BIG(*) AS n
FROM [{s}].[{le}] a
JOIN [{s}].[{le}] b ON b.[{lid}] = a.[{lid}] AND b.[{lt}] = a.[{lt}]
WHERE a.[{lt}] = ? AND a.[{end}] = 1 AND b.[{end}] = 2
GROUP BY LEFT(a.[{rid}], {n}), LEFT(b.[{rid}], {n})
ORDER BY COUNT_BIG(*) DESC""".format(
        rid=le["record_id_column"], lid=le["link_id_column"], lt=le["link_table_column"],
        end=le["end_column"], s=schema, le=le["table"], n=prefix_len)
    return conn.run(sql, [link_table])


# ------------------------------------------------------------- writing it out

def to_yaml(found: Dict[str, Any], connection_env: str = "IBASE_CONNECTION_STRING",
            schema: str = "dbo", link_end_spec: Optional[Dict[str, str]] = None) -> str:
    """A draft mapping, with a review banner and the reasoning left in as comments."""
    L: List[str] = []
    add = L.append
    add("# ============================================================")
    add("#  DRAFT - REVIEW BEFORE USE")
    add("#")
    add("#  This was guessed from the database's own catalog. It is a starting")
    add("#  point, not an answer. Check every line, especially which tables were")
    add("#  called links, before pointing the bridge at real data.")
    add("# ============================================================")
    add("version: 2")
    add("")
    add("source:")
    add("  dialect: sqlserver")
    add('  driver: "ODBC Driver 18 for SQL Server"')
    add("  connection_env: {}".format(connection_env))
    add("  schema: {}".format(schema))
    add("  query_timeout_seconds: 120")
    add("  isolation_level: READ_UNCOMMITTED")
    add("")
    add("nodes:")
    for n in found["nodes"]:
        add("  # {} rows - {}".format(n["rows"], n["why"]))
        add("  - label: {}".format(n["table"]))
        add("    table: {}".format(n["table"]))
        add("    key: {}".format(n["key"]))
        props = [c for c in n["columns"] if c != n["key"]]
        add("    properties: [{}]".format(", ".join(props)))
        add("    types: {{{}}}".format(", ".join(
            "{}: {}".format(k, v) for k, v in n["types"].items())))
        add("")
    add("edges:")
    for e in found["edges"]:
        add("  # {} rows - {}".format(e["rows"], e["why"]))
        if not e.get("resolvable"):
            add("  # WARNING: one of its endpoints is not a node above. Fix before use.")
        fks = e["fks"]
        if len(fks) >= 2 and e.get("resolution") != "link_end":
            # Direction cannot be inferred from a schema - only a person knows whether
            # a Person works for an Organization or the other way round. Getting it
            # backwards does not error; the query simply returns nothing, which is
            # much harder to notice. So say it plainly, on every edge.
            add("  # CHECK THE DIRECTION: this reads as {} -> {}. If that is backwards,"
                .format(fks[0]["ref_table"], fks[1]["ref_table"]))
            add("  # swap src and dst below. A backwards edge returns no rows rather")
            add("  # than an error, so it is easy to miss.")
            add("  # Rename the type to something readable too, e.g. WORKS_FOR.")
        add("  - type: {}".format(e["table"].upper()))
        add("    table: {}".format(e["table"]))
        add("    key: {}".format(e["key"]))
        if e.get("resolution") == "link_end":
            add("    resolution: link_end")
            add("    link_end:")
            add("      table: _LinkEnd")
            for k, v in (link_end_spec or {}).items():
                if k != "table" and v:
                    add("      {}: {}".format(k, v))
            add("      src_end_value: 1")
            add("      dst_end_value: 2")
            add("    endpoints:")
            for ep in e.get("endpoints", []):
                add("      # {} rows observed".format(ep.get("rows", 0)))
                add("      - src: {{label: {}, prefix: {}}}".format(ep["src_label"], ep["src_prefix"]))
                add("        dst: {{label: {}, prefix: {}}}".format(ep["dst_label"], ep["dst_prefix"]))
                add("        row_estimate: {}".format(ep.get("rows", 0)))
            if not e.get("endpoints"):
                add("      # NONE FOUND - fill these in by hand before using this edge.")
        else:
            add("    resolution: fk")
            add("    endpoints:")
            fks = e["fks"]
            if len(fks) >= 2:
                add("      - src: {{label: {}, column: {}}}".format(fks[0]["ref_table"], fks[0]["column"]))
                add("        dst: {{label: {}, column: {}}}".format(fks[1]["ref_table"], fks[1]["column"]))
        add("    properties: [{}]".format(", ".join(e.get("properties", []))))
        add("")
    if found["ambiguous"]:
        add("# ---- not mapped; please decide ----")
        for a in found["ambiguous"]:
            add("#   {}: {}".format(a["table"], a["why"]))
    if found["skipped"]:
        add("# ---- skipped on purpose ----")
        for s in found["skipped"]:
            add("#   {}: {}".format(s["table"], s["why"]))
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------- CLI

def main(argv=None):
    """`python -m ibase_bridge.discovery` — write a draft mapping for review."""
    import argparse
    import json
    import os
    import sys

    ap = argparse.ArgumentParser(
        description="Read a SQL Server database and propose a mapping file. "
                    "The result is a draft for a person to review, never something "
                    "the bridge loads on its own.")
    ap.add_argument("--env", default="IBASE_CONNECTION_STRING",
                    help="environment variable holding the connection string")
    ap.add_argument("--schema", default="dbo")
    ap.add_argument("--mapping-out", default="mapping.proposed.yml")
    ap.add_argument("--facts-out", default="discovery.json",
                    help="raw catalog facts, with no interpretation")
    ap.add_argument("--no-ibase", action="store_true",
                    help="skip the iBase-specific probing (_LinkEnd and record prefixes)")
    args = ap.parse_args(argv)

    dsn = os.environ.get(args.env)
    if not dsn:
        print("environment variable {} is not set. It should hold the SQL Server "
              "connection string for a login with SELECT permission only."
              .format(args.env), file=sys.stderr)
        return 2

    from .connection import SqlServerConnection
    conn = SqlServerConnection(dsn, pool_size=2)

    catalog = read_catalog(conn, args.schema)
    found = classify(catalog, ibase_mode=not args.no_ibase)

    facts = {"schema": args.schema, "catalog": catalog, "classified": found}
    if not args.no_ibase:
        le = probe_link_end(conn, args.schema)
        if le:
            facts["link_end"] = le
            guessed = le["guessed"]
            if all(guessed.values()):
                spec = dict(guessed, table=le["table"])
                link_tables = link_tables_from_link_end(
                    conn, args.schema, le["table"], guessed["link_table_column"])
                facts["ibase_link_tables"] = link_tables
                facts["ibase_endpoint_pairs"] = {}
                seen_prefixes = set()
                for lt in link_tables:
                    try:
                        rows = discover_endpoint_pairs(conn, args.schema, lt, spec)
                        facts["ibase_endpoint_pairs"][lt] = rows
                        for r in rows:
                            seen_prefixes.add(r.get("src_prefix"))
                            seen_prefixes.add(r.get("dst_prefix"))
                    except Exception as exc:
                        facts["ibase_endpoint_pairs"][lt] = {"error": str(exc)}
                # Which record type does each id prefix belong to? Ask the data.
                prefix_map = map_prefixes_to_labels(
                    conn, args.schema, found["nodes"], sorted(p for p in seen_prefixes if p))
                facts["ibase_prefix_to_table"] = prefix_map
                found = apply_link_end_evidence(
                    found, link_tables, facts["ibase_endpoint_pairs"], prefix_map)

    with open(args.facts_out, "w") as fh:
        json.dump(facts, fh, indent=2, default=str)
    with open(args.mapping_out, "w") as fh:
        fh.write(to_yaml(found, connection_env=args.env, schema=args.schema,
                         link_end_spec=(facts.get("link_end") or {}).get("guessed")))

    print("Looked at {} tables in schema {}.".format(len(catalog["tables"]), args.schema))
    print("  probably records : {}".format(", ".join(n["table"] for n in found["nodes"]) or "none"))
    print("  probably links   : {}".format(", ".join(e["table"] for e in found["edges"]) or "none"))
    if found["ambiguous"]:
        print("  needs a decision :")
        for a in found["ambiguous"]:
            print("      {} - {}".format(a["table"], a["why"]))
    if found["skipped"]:
        print("  skipped          : {}".format(", ".join(s["table"] for s in found["skipped"])))
    if facts.get("ibase_link_tables"):
        print("  _LinkEnd names these as link tables: {}"
              .format(", ".join(facts["ibase_link_tables"])))
        for lt, pairs in (facts.get("ibase_endpoint_pairs") or {}).items():
            if isinstance(pairs, list):
                shown = ", ".join("{}->{} ({})".format(p.get("src_prefix"), p.get("dst_prefix"),
                                                       p.get("n")) for p in pairs[:6])
                print("      {} really joins: {}".format(lt, shown))

    print("\nWrote {} and {}.".format(args.mapping_out, args.facts_out))
    print("\nRead the draft before using it. Two things in particular:")
    print("  - every link's DIRECTION is a guess. A backwards edge returns no rows")
    print("    rather than an error, so it is easy to miss.")
    print("  - the link type names are just table names in capitals. Rename them to")
    print("    something an analyst would recognise, like WORKS_FOR.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
