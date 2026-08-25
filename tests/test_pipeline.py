"""Tests for the iBase bridge. No database required.

Every test here runs against the mapping files and a tiny fake connection, so the
whole suite finishes in a second with nothing installed but PyYAML. The real
database is exercised separately by `scripts/probe_queries.py`.

Run either way:
    python3 tests/test_pipeline.py
    python3 -m pytest
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ibase_bridge import cypher_translator, element_id, mapping, predicate, tsql
from ibase_bridge.mssql_backend import BranchLimitExceeded, MssqlBackend
from ibase_bridge.node_id import NodeIdCodec
from ibase_bridge.query_processor import QueryProcessor

try:
    import pytest
except ImportError:                        # the plain runner needs no pytest
    pytest = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.path.join(HERE, "config", "mapping.demo.yml")
IBASE = os.path.join(HERE, "config", "mapping.ibase.yml")

IBASE_SAMPLES = {"Person": ["PER0000001"], "Organization": ["ORG0000001"],
                 "Vehicle": ["VEH0000001"], "Event": ["EVT0000001"],
                 "Associate": ["ASS0000001"], "Involved_In": ["INV0000001"]}


class FakeConnection:
    """Returns canned rows so the whole pipeline can run with no SQL Server."""

    server_major = 16

    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def run(self, sql, params, param_types=()):
        self.calls.append((sql, list(params), list(param_types)))
        return self.rows


def demo_backend(rows=None):
    m = mapping.load(DEMO)
    codec = NodeIdCodec()
    codec.register(m.label_order(), key_types=m.key_types())
    return MssqlBackend(m, codec, connection=FakeConnection(rows)), m, codec


def ibase_backend(rows=None):
    m = mapping.load(IBASE)
    codec = NodeIdCodec()
    codec.register(m.label_order(), key_types=m.key_types(), samples=IBASE_SAMPLES)
    return MssqlBackend(m, codec, connection=FakeConnection(rows)), m, codec


def sql_for(b, cypher, which=0):
    plan = cypher_translator.translate(cypher)
    return b.compile(b.concrete_plans(plan)[which])


# ---------------------------------------------------------------- node ids

def test_node_id_round_trips_for_integer_keys():
    _, _, codec = demo_backend()
    eid = codec.encode("Person", (1001,))
    assert eid == "0:1001", eid
    t, o = eid.split(":")
    assert codec.decode(int(t), int(o)) == ("Person", (1001,))


def test_node_id_round_trips_for_ibase_record_ids():
    _, _, codec = ibase_backend()
    eid = codec.encode("Person", ("PER0000123",))
    assert eid == "0:123", eid
    assert codec.decode(0, 123) == ("Person", ("PER0000123",))


def test_node_ids_survive_a_restart():
    """The whole point of computing the id rather than remembering it: a brand-new
    process must decode an id minted by an older one."""
    _, _, first = ibase_backend()
    minted = first.encode("Vehicle", ("VEH0000042",))
    _, _, second = ibase_backend()          # a fresh codec, as after a restart
    t, o = minted.split(":")
    assert second.decode(int(t), int(o)) == ("Vehicle", ("VEH0000042",))


def test_inconsistent_key_formats_fall_back_instead_of_colliding():
    """PER123 and PER0000123 both end in 123. If we used arithmetic we could not
    tell which string to rebuild, so the codec must notice and keep a registry."""
    codec = NodeIdCodec()
    codec.register(["Odd"], key_types={"Odd": "nvarchar"},
                   samples={"Odd": ["PER123", "PER0000123"]})
    assert codec.strategy_of("Odd") == "registry"
    a = codec.encode("Odd", ("PER123",))
    b = codec.encode("Odd", ("PER0000123",))
    assert a != b
    t, o = a.split(":")
    assert codec.decode(int(t), int(o)) == ("Odd", ("PER123",))


def test_element_id_is_insensitive_to_key_spelling():
    """1001 and "1001" are the same row and must get the same id, or Expand
    silently returns nothing for it."""
    assert element_id.encode("Person", (1001,)) == element_id.encode("Person", ("1001",))
    assert element_id.decode(element_id.encode("Person", ("PER1",))) == ("Person", ("PER1",))
    assert element_id.decode("not one of ours") is None


# ------------------------------------------------------- pattern -> T-SQL

def test_single_node_scan():
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (n:Person) RETURN n LIMIT 25").sql
    assert "SELECT TOP (25)" in sql
    assert "FROM [dbo].[Person] AS v0" in sql
    assert "v0.[person_id] AS __gx_v0_k0" in sql


def test_one_hop_joins_through_the_link_table():
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p,r,o LIMIT 10").sql
    assert "JOIN [dbo].[Employment] AS e0 ON e0.[person_id] = v0.[person_id]" in sql
    assert "JOIN [dbo].[Organization] AS v1 ON v1.[organization_id] = e0.[organization_id]" in sql


def test_reverse_arrow_swaps_the_join_columns_not_the_meaning():
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (o:Organization)<-[r:WORKS_FOR]-(p:Person) RETURN p,r,o").sql
    assert "FROM [dbo].[Organization] AS v0" in sql
    assert "e0.[organization_id] = v0.[organization_id]" in sql
    assert "v1.[person_id] = e0.[person_id]" in sql


def test_reverse_edge_endpoint_ids_still_match_their_nodes():
    """Endpoint ids follow the mapping's source/target, not the pattern's left and
    right — otherwise a reverse arrow produces edges whose ends point nowhere."""
    b, _, _ = demo_backend()
    c = sql_for(b, "MATCH (o:Organization)<-[r:WORKS_FOR]-(p:Person) RETURN p,r,o")
    e = c.manifest["edges"][0]
    assert e["src_alias"] == "Person" and e["dst_alias"] == "Organization"
    assert e["start_var"] == "p" and e["end_var"] == "o"


def test_undirected_same_label_edge_becomes_two_clean_joins():
    b, _, _ = demo_backend()
    plan = cypher_translator.translate(
        "MATCH (a:Account)-[r:TRANSFERRED_TO]-(b:Account) RETURN a,r,b LIMIT 100")
    plans = b.concrete_plans(plan)
    assert len(plans) == 2, len(plans)
    sqls = [b.compile(cp).sql for cp in plans]
    assert any("e0.[from_account_id] = v0.[account_id]" in s for s in sqls)
    assert any("e0.[to_account_id] = v0.[account_id]" in s for s in sqls)
    # An OR-join would work but stops SQL Server using an index.
    assert not any(" OR " in s for s in sqls)


def test_multi_hop_forbids_reusing_the_same_edge():
    """Cypher guarantees a relationship appears at most once in a path. A plain
    JOIN does not, so without this guard every a->b->a bounce-back comes back as a
    two-hop path."""
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (a:Account)-[r1:TRANSFERRED_TO]->(x:Account)"
                     "-[r2:TRANSFERRED_TO]->(c:Account) RETURN a,x,c").sql
    assert "e1.[transfer_id] <> e0.[transfer_id]" in sql


def test_two_different_link_tables_need_no_uniqueness_guard():
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (p:Person)-[:OWNS]->(a:Account)"
                     "-[:TRANSFERRED_TO]->(x:Account) RETURN p,a,x").sql
    assert "<>" not in sql


# ------------------------------------------------------------- edge ids

def test_edge_id_comes_from_the_link_tables_own_key():
    """Minting an edge id from its two endpoints collapses parallel links — and
    iBase lets the same two records be linked repeatedly."""
    b, _, _ = demo_backend()
    c = sql_for(b, "MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p,r,o")
    assert "e0.[employment_id] AS __gx_e0_k0" in c.sql
    assert c.manifest["edges"][0]["key_cols"] == ["__gx_e0_k0"]


def test_parallel_links_between_the_same_pair_stay_distinct():
    from ibase_bridge.result_converter import convert_rows
    b, _, codec = demo_backend()
    c = sql_for(b, "MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p,r,o")
    # Same two records, two different Employment rows.
    rows = [{"__gx_v0_k0": 1, "__gx_v1_k0": 9, "__gx_e0_k0": 100, "__gx_e0_p0": "Analyst"},
            {"__gx_v0_k0": 1, "__gx_v1_k0": 9, "__gx_e0_k0": 101, "__gx_e0_p0": "Director"}]
    out = convert_rows(rows, c.manifest, id_fn=codec.encode)
    ids = {r.id for r in out.relationships}
    assert len(ids) == 2, "parallel links collapsed onto one id: %r" % ids


# --------------------------------------------------------------- paging

def test_skip_uses_offset_and_always_supplies_a_sort():
    """T-SQL only allows OFFSET after an ORDER BY, and an unsorted page can repeat a
    row it already returned."""
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (n:Person) RETURN n SKIP 1000 LIMIT 1").sql
    assert "OFFSET 1000 ROWS FETCH NEXT 1 ROWS ONLY" in sql
    assert "ORDER BY" in sql
    assert "TOP (" not in sql
    assert sql.index("ORDER BY") < sql.index("OFFSET")


def test_no_skip_uses_top():
    b, _, _ = demo_backend()
    sql = sql_for(b, "MATCH (n:Person) RETURN n LIMIT 25").sql
    assert "SELECT TOP (25)" in sql and "OFFSET" not in sql


# ----------------------------------------------- the 2100-parameter ceiling

def test_expand_with_three_thousand_ids_is_one_statement_and_one_parameter():
    """SQL Server refuses more than 2100 parameters per statement. In real captured
    traffic 22% of Expand queries carry more ids than that, and one carried 42,214."""
    b, _, _ = demo_backend()
    ids = ",".join("internal_id(0, %d)" % (1000 + i) for i in range(3000))
    c = sql_for(b, "MATCH (n)-[r]-(m) WHERE id(n) IN [%s] RETURN n,r,m LIMIT 1000" % ids)
    assert len(c.params) == 1, len(c.params)
    assert "OPENJSON(?)" in c.sql
    assert c.sql.count("SELECT") >= 1


def test_openjson_declares_the_key_column_type():
    """If OPENJSON returns nvarchar and the key column is bigint, SQL Server converts
    THE COLUMN and the index lookup silently becomes a full scan."""
    b, _, _ = demo_backend()
    ids = ",".join("internal_id(0, %d)" % (1000 + i) for i in range(500))
    c = sql_for(b, "MATCH (n:Person) WHERE id(n) IN [%s] RETURN n" % ids)
    assert "WITH ([value] bigint '$')" in c.sql, c.sql


def test_small_id_list_stays_a_plain_in():
    b, _, _ = demo_backend()
    c = sql_for(b, "MATCH (n:Person) WHERE id(n) IN [internal_id(0, 1), internal_id(0, 2)] RETURN n")
    assert "IN (?, ?)" in c.sql and "OPENJSON" not in c.sql


# ------------------------------------------------------------ WHERE clause

def _where(b, cypher):
    c = sql_for(b, cypher)
    line = [l for l in c.sql.splitlines() if l.startswith("WHERE")]
    return (line[0] if line else ""), c.params


def test_contains_escapes_wildcards_the_user_did_not_intend():
    """CONTAINS '50%' must not become LIKE '%50%%', which matches anything with 50."""
    b, _, _ = demo_backend()
    w, params = _where(b, "MATCH (p:Person) WHERE p.full_name CONTAINS '50%' RETURN p")
    assert "LIKE ? ESCAPE" in w
    assert params == ["%50\\%%"], params


def test_starts_with_stays_a_prefix_match():
    b, _, _ = demo_backend()
    _, params = _where(b, "MATCH (p:Person) WHERE p.full_name STARTS WITH 'Av' RETURN p")
    assert params == ["Av%"]


def test_boolean_property_compares_against_one_not_true():
    b, _, _ = demo_backend()
    ctx = predicate.SqlCtx(gen={"p": "v0"}, label_of={"p": "Person"},
                           node_pk={"Person": "person_id"}, registry=None, params=[],
                           dialect=tsql.Dialect(16), param_types=[])
    assert predicate.to_sql(predicate.parse("p.flagged"), ctx) == "v0.[flagged] = 1"


def test_and_or_not_and_null_all_compile():
    b, _, _ = demo_backend()
    w, params = _where(b, "MATCH (p:Person) WHERE (p.risk_score > 80 OR p.country_code IS NULL) "
                          "AND NOT p.full_name ENDS WITH 'Ltd' RETURN p")
    assert " OR " in w and " AND " in w and "NOT (" in w and "IS NULL" in w
    assert 80 in params


def test_regex_is_refused_rather_than_quietly_dropped():
    b, _, _ = demo_backend()
    try:
        _where(b, "MATCH (p:Person) WHERE p.full_name =~ '^Av' RETURN p")
    except tsql.UnsupportedByTSql as e:
        assert "regular-expression" in str(e)
    else:
        raise AssertionError("regex should be refused, not silently mistranslated")


# ------------------------------------------------------- polymorphic links

def test_one_link_type_fans_out_to_one_query_per_endpoint_pair():
    b, _, _ = ibase_backend()
    plans = b.concrete_plans(cypher_translator.translate(
        "MATCH (a)-[r:Associate]->(z) RETURN a,r,z"))
    got = {(cp.labels["a"], cp.labels["z"]) for cp in plans}
    assert got == {("Person", "Person"), ("Person", "Organization"),
                   ("Organization", "Vehicle")}, got


def test_an_explicit_label_prunes_the_pairs_that_cannot_match():
    b, _, _ = ibase_backend()
    n = len(b.concrete_plans(cypher_translator.translate(
        "MATCH (a:Organization)-[r:Associate]->(z:Vehicle) RETURN a,r,z")))
    assert n == 1
    none = b.concrete_plans(cypher_translator.translate(
        "MATCH (a:Person)-[r:Associate]->(z:Vehicle) RETURN a,r,z"))
    assert none == [], "Person->Vehicle is not a declared pair and must match nothing"


def test_link_end_resolution_joins_through_the_system_table():
    b, _, _ = ibase_backend()
    c = sql_for(b, "MATCH (p:Person)-[r:Involved_In]->(e:Event) RETURN p,r,e")
    assert "[dbo].[_LinkEnd] AS e0s" in c.sql and "[dbo].[_LinkEnd] AS e0d" in c.sql
    assert "e0.[Involved_In_ID] = e0s.[LinkId]" in c.sql
    # The link type and the end markers are parameters, never pasted into the text.
    assert c.params[:2] == ["Involved_In", 1]


def test_prefix_filter_is_dropped_when_an_id_set_already_drives_the_query():
    b, _, _ = ibase_backend()
    with_prefix = sql_for(b, "MATCH (a:Person)-[r:Associate]->(z:Person) RETURN a,r,z").sql
    assert "LIKE ?" in with_prefix
    ids = ",".join("internal_id(0, %d)" % i for i in range(5))
    scoped = sql_for(b, "MATCH (a:Person)-[r:Associate]->(z:Person) "
                        "WHERE id(a) IN [%s] RETURN a,r,z" % ids).sql
    assert "LIKE ?" not in scoped, "the prefix filter is pure overhead once ids drive the plan"


def test_schema_reports_a_representative_pair_and_lists_the_rest():
    b, m, _ = ibase_backend()
    resp = b.schema_response("ibase")["ibase"]
    assoc = resp["relationships"]["Associate"]
    assert (assoc["startCategory"], assoc["endCategory"]) == ("Person", "Person")
    assert len(assoc["endpointPairs"]) == 3


def test_branch_fan_out_is_capped_rather_than_run():
    b, _, _ = ibase_backend()
    b.max_branches = 2
    try:
        b.concrete_plans(cypher_translator.translate("MATCH (n)-[r]-(m) RETURN n,r,m"))
    except BranchLimitExceeded as e:
        assert "Add a label" in str(e)
    else:
        raise AssertionError("an unprunable fan-out should be refused")


# ------------------------------------------------------- envelope + limits

def test_schema_envelope_is_wrapped_once():
    b, _, _ = demo_backend()
    out = QueryProcessor(b, "demo").execute("CALL schema()")
    assert out.type == "SCHEMA"
    assert "categories" in out.data["demo"]


def test_unsupported_queries_fail_loudly():
    b, _, _ = demo_backend()
    qp = QueryProcessor(b, "demo")
    for q, expect in [
        ("MATCH (a)-[r*1..3]->(b) RETURN a", "variable"),
        ("OPTIONAL MATCH (a:Person) RETURN a", "OPTIONAL MATCH"),
        ("MATCH (a:Person) WITH a RETURN a", "WITH"),
        ("UNWIND [1,2] AS x RETURN x", "UNWIND"),
        ("CREATE (n:Person) RETURN n", ""),
    ]:
        out = qp.execute(q)
        body = str(out.data)
        assert out.type == "TABLE" and ("error" in body.lower() or expect.lower() in body.lower()), \
            "%r produced %r" % (q, body)


def test_starts_with_is_not_mistaken_for_the_with_clause():
    b, _, _ = demo_backend()
    out = QueryProcessor(b, "demo").execute(
        "MATCH (p:Person) WHERE p.full_name STARTS WITH 'A' RETURN p LIMIT 5")
    assert out.type == "GRAPH", out.data


def test_a_refusal_reaches_the_user_instead_of_becoming_an_empty_graph():
    """The branch loop skips branches that simply match nothing. It must NOT skip a
    branch that raised because we refuse to translate it - the user would get an
    empty canvas and read it as "no results" rather than "this is not supported"."""
    b, _, _ = demo_backend(rows=[])
    qp = QueryProcessor(b, "demo")
    try:
        qp.execute("MATCH (p:Person) WHERE p.full_name =~ '^Av' RETURN p")
    except tsql.UnsupportedByTSql:
        pass                        # reaches the server, which turns it into status 1
    else:
        raise AssertionError("a refusal was swallowed into an empty result")


def test_limit_is_applied_across_branches_not_per_branch():
    from ibase_bridge.result_converter import convert_rows
    rows = [{"__gx_v0_k0": i, "__gx_v1_k0": i + 100, "__gx_e0_k0": i + 500} for i in range(10)]
    b, _, _ = demo_backend(rows=rows)
    plan = cypher_translator.translate(
        "MATCH (a:Account)-[r:TRANSFERRED_TO]-(x:Account) RETURN a,r,x LIMIT 10")
    out = b.execute(plan)          # two branches, ten rows each
    assert len(out.relationships) <= 10, len(out.relationships)


# ------------------------------------------------------------- discovery

def _fake_catalog():
    """A catalog shaped like the demo database, so classify() can be tested with
    no SQL Server involved."""
    def col(t, c, ty, i):
        return {"schema": "dbo", "table": t, "column": c, "type": ty,
                "max_length": 8, "nullable": 0, "collation": None, "ordinal": i}
    return {
        "tables": [{"schema": "dbo", "table": t, "rows": n} for t, n in
                   [("Person", 12), ("Organization", 5), ("Account", 10),
                    ("Employment", 9), ("AccountTransfer", 23), ("_AL_Audit", 1)]],
        "columns": [col("Person", "person_id", "bigint", 1), col("Person", "full_name", "nvarchar", 2),
                    col("Organization", "organization_id", "bigint", 1),
                    col("Account", "account_id", "bigint", 1),
                    col("Employment", "employment_id", "bigint", 1),
                    col("Employment", "person_id", "bigint", 2),
                    col("Employment", "organization_id", "bigint", 3),
                    col("Employment", "job_title", "nvarchar", 4),
                    col("AccountTransfer", "transfer_id", "bigint", 1),
                    col("AccountTransfer", "from_account_id", "bigint", 2),
                    col("AccountTransfer", "to_account_id", "bigint", 3),
                    col("_AL_Audit", "AuditId", "bigint", 1)],
        "primary_keys": [{"schema": "dbo", "table": t, "column": c, "ordinal": 1} for t, c in
                         [("Person", "person_id"), ("Organization", "organization_id"),
                          ("Account", "account_id"), ("Employment", "employment_id"),
                          ("AccountTransfer", "transfer_id")]],
        "foreign_keys": [
            {"schema": "dbo", "table": "Employment", "column": "person_id",
             "ref_schema": "dbo", "ref_table": "Person", "ref_column": "person_id", "fk_name": "fk1"},
            {"schema": "dbo", "table": "Employment", "column": "organization_id",
             "ref_schema": "dbo", "ref_table": "Organization", "ref_column": "organization_id", "fk_name": "fk2"},
            {"schema": "dbo", "table": "AccountTransfer", "column": "from_account_id",
             "ref_schema": "dbo", "ref_table": "Account", "ref_column": "account_id", "fk_name": "fk3"},
            {"schema": "dbo", "table": "AccountTransfer", "column": "to_account_id",
             "ref_schema": "dbo", "ref_table": "Account", "ref_column": "account_id", "fk_name": "fk4"},
        ],
    }


def test_discovery_tells_records_from_links():
    from ibase_bridge import discovery
    f = discovery.classify(_fake_catalog())
    assert {n["table"] for n in f["nodes"]} == {"Person", "Organization", "Account"}
    assert {e["table"] for e in f["edges"]} == {"Employment", "AccountTransfer"}


def test_discovery_recognises_a_self_referencing_link():
    """Both of AccountTransfer's keys point at Account. Counting distinct target
    TABLES sees one and calls it a record; counting key COLUMNS sees two ends."""
    from ibase_bridge import discovery
    f = discovery.classify(_fake_catalog())
    t = [e for e in f["edges"] if e["table"] == "AccountTransfer"][0]
    assert t["self_referencing"] is True
    assert t["targets"] == ["Account"]


def test_discovery_never_proposes_system_tables():
    from ibase_bridge import discovery
    f = discovery.classify(_fake_catalog())
    names = {n["table"] for n in f["nodes"]} | {e["table"] for e in f["edges"]}
    assert "_AL_Audit" not in names
    assert any(s["table"] == "_AL_Audit" for s in f["skipped"])


def test_the_proposed_mapping_loads_and_is_valid():
    """A draft nobody can load is not a draft, it is a wasted step."""
    import yaml
    from ibase_bridge import discovery
    text = discovery.to_yaml(discovery.classify(_fake_catalog()))
    m = mapping.from_dict(yaml.safe_load(text))
    assert set(m.node_schemas) == {"Person", "Organization", "Account"}
    assert set(m.rel_schemas) == {"EMPLOYMENT", "ACCOUNTTRANSFER"}
    assert "CHECK THE DIRECTION" in text
    assert "REVIEW BEFORE USE" in text


def test_proposed_edge_endpoints_follow_column_order():
    """Catalog order is arbitrary; column order is at least stable and usually
    matches how a person reads the table."""
    from ibase_bridge import discovery
    f = discovery.classify(_fake_catalog())
    emp = [e for e in f["edges"] if e["table"] == "Employment"][0]
    assert [x["column"] for x in emp["fks"]] == ["person_id", "organization_id"]


# ---------------------------------------------------- the schema editor

def _studio_draft():
    from ibase_bridge import discovery, studio
    return studio.draft_from_discovery(discovery.classify(_fake_catalog()), "dbo")


def test_draft_picks_a_readable_column_to_show():
    """A preview of "1001 -> 2001" tells you nothing about whether the direction is
    right. "Avery Chen -> Northwind Logistics" does."""
    d = _studio_draft()
    person = [n for n in d["nodes"] if n["table"] == "Person"][0]
    assert person["display"] == "full_name"


def test_flip_reverses_labels_and_columns_together():
    from ibase_bridge import studio
    d = _studio_draft()
    e = [x for x in d["edges"] if x["table"] == "Employment"][0]
    before = (e["pairs"][0]["src_label"], e["pairs"][0]["src_column"])
    studio.flip(e)
    after = (e["pairs"][0]["dst_label"], e["pairs"][0]["dst_column"])
    assert before == after, "flipping must move the column with its label"
    studio.flip(e)
    assert (e["pairs"][0]["src_label"], e["pairs"][0]["src_column"]) == before


def test_renaming_a_record_updates_every_link_that_points_at_it():
    from ibase_bridge import studio
    d = _studio_draft()
    for n in d["nodes"]:
        if n["table"] == "Person":
            n["label"] = "Individual"
    raw = studio.draft_to_mapping_dict(d)
    m = mapping.from_dict(raw)
    assert "Individual" in m.node_schemas and "Person" not in m.node_schemas
    labels = {lbl for r in m.rel_schemas.values() for e in r.endpoints
              for lbl in (e.src_label, e.dst_label)}
    assert "Person" not in labels, "a link still points at the old name"


def test_the_draft_becomes_a_mapping_the_bridge_accepts():
    from ibase_bridge import studio
    d = _studio_draft()
    for e in d["edges"]:
        e["type"] = {"Employment": "WORKS_FOR", "AccountTransfer": "TRANSFERRED_TO"}[e["table"]]
    m = mapping.from_dict(studio.draft_to_mapping_dict(d))
    assert set(m.rel_schemas) == {"WORKS_FOR", "TRANSFERRED_TO"}


def test_excluded_tables_do_not_reach_the_mapping():
    from ibase_bridge import studio
    d = _studio_draft()
    for e in d["edges"]:
        if e["table"] == "Employment":
            e["include"] = False
    m = mapping.from_dict(studio.draft_to_mapping_dict(d))
    assert "EMPLOYMENT" not in m.rel_schemas


def test_a_link_whose_endpoint_was_excluded_is_dropped_not_broken():
    """Turning off Organization must not leave Employment pointing at a label that
    no longer exists - the mapping would refuse to load and the editor would look
    broken for a reason the user cannot see."""
    from ibase_bridge import studio
    d = _studio_draft()
    for n in d["nodes"]:
        if n["table"] == "Organization":
            n["include"] = False
    m = mapping.from_dict(studio.draft_to_mapping_dict(d))     # must not raise
    assert "EMPLOYMENT" not in m.rel_schemas


def test_saved_yaml_round_trips():
    from ibase_bridge import studio
    import yaml as _yaml
    d = _studio_draft()
    text = studio.to_yaml(d)
    m = mapping.from_dict(_yaml.safe_load(text))
    assert set(m.node_schemas) == {"Person", "Organization", "Account"}
    assert "set by a person, not guessed" in text


def test_preview_only_needs_the_link_being_looked_at():
    """A half-finished definition elsewhere must not stop you checking the one in
    front of you."""
    from ibase_bridge import studio
    d = _studio_draft()
    for e in d["edges"]:
        if e["table"] == "AccountTransfer":
            e["key"] = None                       # deliberately broken
    raw = studio.draft_to_mapping_dict(d, only_edge="Employment")
    m = mapping.from_dict(raw)
    assert set(m.rel_schemas) == {"EMPLOYMENT"}


def test_sample_rows_label_every_column_with_its_role():
    """Two names for one thing is where confusion lives: the database calls it
    dbo.Employment.person_id, Kineviz shows it as the source end of WORKS_FOR."""
    from ibase_bridge import studio

    class FakeConn:
        server_major = 16
        def run(self, sql, params=(), param_types=()):
            return [{"employment_id": 9001, "person_id": 1001, "organization_id": 2001,
                     "job_title": "Director", "start_date": "2021-02-01",
                     "end_date": None, "source_updated_at": "2026-01-04T09:00:00"}]

    d = _studio_draft()
    for e in d["edges"]:
        if e["table"] == "Employment":
            e["type"] = "WORKS_FOR"
            e["properties"] = ["job_title"]        # deliberately leave the rest out
    r = studio.sample_rows(d, "Employment", "edge", FakeConn())
    roles = {c["column"]: c["role"] for c in r["columns"]}
    assert roles["employment_id"] == "key"
    assert roles["person_id"] == "source end"
    assert roles["organization_id"] == "target end"
    assert roles["job_title"] == "property"
    # A column nobody mapped is invisible in Kineviz, and the usual way to find that
    # out is to go looking for it later. Say so up front.
    assert roles["source_updated_at"] == "not mapped"
    assert "source_updated_at" in r["unmapped"]
    assert r["rows"] and r["rows"][0][0] == "9001"


def test_sample_rows_shows_where_a_column_ends_up():
    from ibase_bridge import studio

    class FakeConn:
        server_major = 16
        def run(self, sql, params=(), param_types=()):
            return [{"person_id": 1001, "full_name": "Avery Chen"}]

    d = _studio_draft()
    for n in d["nodes"]:
        if n["table"] == "Person":
            n["label"] = "Individual"
    r = studio.sample_rows(d, "Person", "node", FakeConn())
    assert r["name"] == "Individual"
    assert r["table"].endswith("Person")


# ------------------------------------------- reaching the bridge from a browser

class _Skip(Exception):
    """This test needs something the bare suite deliberately does not install."""


def _client(allow=None):
    # These four tests are the only ones that need the web layer. The rest of the
    # suite runs with PyYAML alone, and that is worth protecting - a contributor
    # should be able to clone and run the tests with nothing installed.
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        raise _Skip("fastapi is not installed")
    import ibase_server
    args = ibase_server.parse_args(["--mapping", DEMO, "--compile-only"])
    state = ibase_server.build_state(args)
    return TestClient(ibase_server.create_app(state, studio=True, allow_origins=allow or []))


def _needs_web():
    try:
        import fastapi  # noqa: F401
    except ImportError:
        if pytest is not None:
            pytest.skip("fastapi is not installed; covered by the integration job")
        raise _Skip("fastapi is not installed")


def _preflight(c, origin):
    return c.options("/ibase/demo", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Private-Network": "true"})


def test_kineviz_in_the_browser_may_reach_a_bridge_on_this_machine():
    _needs_web()
    """Chrome sends an extra preflight before a public https page may reach a private
    address. Without an answer to it the request never completes, and the symptom is a
    schema that never loads with nothing to explain it."""
    r = _preflight(_client(), "https://graphxr.kineviz.com")
    assert r.status_code == 200, r.text
    assert r.headers.get("access-control-allow-private-network") == "true"
    assert r.headers.get("access-control-allow-origin") == "https://graphxr.kineviz.com"


def test_a_random_website_may_not():
    _needs_web()
    """Answering that preflight relaxes the one thing stopping arbitrary pages from
    reaching this bridge, and the bridge has no password of its own."""
    r = _preflight(_client(), "https://evil.example.com")
    assert r.status_code == 403, r.status_code
    assert "allow-origin" in r.text.lower() or "kineviz" in r.text.lower()


def test_the_schema_editor_on_this_machine_still_works():
    _needs_web()
    r = _preflight(_client(), "http://localhost:7073")
    assert r.status_code == 200


def test_a_self_hosted_kineviz_can_be_allowed():
    _needs_web()
    r = _preflight(_client(allow=["https://kineviz.example.com"]), "https://kineviz.example.com")
    assert r.status_code == 200
    assert _preflight(_client(), "https://kineviz.example.com").status_code == 403


def _run_all():
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    skipped = 0
    for name, fn in fns:
        try:
            fn()
            print("ok   - %s" % name)
        except _Skip as exc:
            skipped += 1
            print("skip - %s (%s)" % (name, exc))
        except BaseException as exc:
            # pytest.skip() raises its own Skipped, which is not an Exception
            # subclass. Treat it as a skip here too, so the same file runs both
            # under pytest and on its own.
            if type(exc).__name__ != "Skipped":
                raise
            skipped += 1
            print("skip - %s (%s)" % (name, exc))
        except Exception as exc:
            failed += 1
            print("FAIL - %s: %s" % (name, exc))
    print("\n%d passed, %d skipped, %d failed, %d total"
          % (len(fns) - failed - skipped, skipped, failed, len(fns)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
