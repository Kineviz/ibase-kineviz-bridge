"""Turn a Cypher pattern into ordinary T-SQL.

The PostgreSQL bridge hands the whole pattern to the database, because PostgreSQL
19 understands graph patterns natively. SQL Server does not, so this module writes
the joins itself. The shape is mechanical: walk the pattern left to right, and each
hop adds a join to the link table and a join to the next entity table.

Everything ambiguous is resolved *before* any SQL is written. A `MatchPlan` may
leave the edge type open (`[r]`), offer alternatives (`[r:A|B]`), point either way
(`-[r]-`), or name a link type that connects several kinds of record. Each of those
becomes one or more `ConcretePlan`s, and compiling a ConcretePlan is total: no
runtime choices, no dynamic manifest. That enumeration replaces three separate
pieces of the PostgreSQL bridge (`_resolve_labels`, `_compatible_typed_plans` and
`query_processor._expand_branches`).
"""

from __future__ import annotations

import itertools
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import predicate, tsql
from .backend import (Backend, BoundEdge, ConcretePlan, EdgePat, GraphResult,
                      MatchPlan, NodeSchema, RelSchema, build_schema_response)
from .result_converter import convert_rows

logger = logging.getLogger(__name__)


class CompiledQuery:
    def __init__(self, sql: str, params: List[Any], manifest: Dict[str, Any],
                 param_types: Optional[List[Any]] = None):
        self.sql = sql
        self.params = params
        self.manifest = manifest
        self.param_types = param_types or []


class BranchLimitExceeded(Exception):
    """Too many concrete plans to run. Better to say so than to melt the server."""


class MssqlBackend(Backend):

    def __init__(self, mapping, id_codec, connection=None):
        self.mapping = mapping
        self.node_schemas: Dict[str, NodeSchema] = mapping.node_schemas
        self.rel_schemas: Dict[str, RelSchema] = mapping.rel_schemas
        self.registry = id_codec
        self.connection = connection
        self.schema = mapping.schema
        self.dialect = tsql.Dialect(
            server_major=getattr(connection, "server_major", 16))
        self.max_branches = int(mapping.tuning.get("max_branches") or 64)
        self.last_sql = ""

    # ------------------------------------------------------------------ names

    def _canon(self, name: Optional[str]) -> Optional[str]:
        """Resolve a label to the case the mapping declares, so `:person` finds
        `Person`. SQL Server is usually case-insensitive; our own dictionaries are
        not."""
        if name is None or name in self.node_schemas or name in self.rel_schemas:
            return name
        low = name.lower()
        for k in list(self.node_schemas) + list(self.rel_schemas):
            if k.lower() == low:
                return k
        return name

    def _t(self, table: str) -> str:
        return tsql.qualify(self.schema, table)

    # ------------------------------------------------ resolving the ambiguity

    def _labels_from_ids(self, plan: MatchPlan) -> Dict[str, str]:
        """Labels implied by `id(n) IN [internal_id(t, o)]`.

        This is the strongest hint we get and the cheapest pruning available: the
        ids Kineviz sends back decode to their own labels, so an untyped expand
        over a 30-link-type schema usually collapses to a handful of branches.
        """
        out: Dict[str, str] = {}
        for v in plan.vertices:
            for t, _o in (v.id_refs or []):
                dec = self.registry.decode(t, 0) if self.registry else None
                if dec:
                    out[v.var] = dec[0]
                    break
        return out

    def _edge_candidates(self, e: EdgePat, fixed: Dict[str, str]) -> List[BoundEdge]:
        """Every (type, endpoint pair, orientation) this edge could mean."""
        types = [self._canon(t) for t in e.types] if e.types else list(self.rel_schemas)
        out: List[BoundEdge] = []
        for rtype in types:
            rs = self.rel_schemas.get(rtype)
            if rs is None:
                continue
            for pair in rs.endpoints:
                # "out" means the pattern arrow runs the same way the mapping does;
                # "in" means it runs against it; "both" allows either.
                orientations = {"out": [False], "in": [True], "both": [False, True]}[e.direction]
                for swapped in orientations:
                    src_label = pair.dst_label if swapped else pair.src_label
                    dst_label = pair.src_label if swapped else pair.dst_label
                    # Drop anything the pattern already contradicts.
                    want_src = fixed.get(e.src_var)
                    want_dst = fixed.get(e.dst_var)
                    if want_src and want_src != src_label:
                        continue
                    if want_dst and want_dst != dst_label:
                        continue
                    out.append(BoundEdge(var=e.var, rel_type=rtype, pair=pair,
                                         swapped=swapped, src_var=e.src_var,
                                         dst_var=e.dst_var))
        return out

    def concrete_plans(self, plan: MatchPlan) -> List[ConcretePlan]:
        fixed: Dict[str, str] = {}
        for v in plan.vertices:
            lbl = self._canon(v.label)
            if lbl in self.node_schemas:
                fixed[v.var] = lbl
        # An explicit label wins; an id-implied one fills the gaps.
        for var, lbl in self._labels_from_ids(plan).items():
            fixed.setdefault(var, lbl)

        if not plan.edges:
            var = plan.vertices[0].var
            label = fixed.get(var)
            labels = [label] if label else list(self.node_schemas)
            return [ConcretePlan(plan=plan, labels={var: l}, edges=[]) for l in labels]

        per_edge = [self._edge_candidates(e, fixed) for e in plan.edges]
        if any(not c for c in per_edge):
            return []

        estimate = 1
        for c in per_edge:
            estimate *= len(c)
        if estimate > self.max_branches * 64:
            raise BranchLimitExceeded(
                "this pattern could mean {} different things, which is too many to run. "
                "Add a label to the pattern (for example `(n:Person)`), name the "
                "relationship type, or select some nodes first.".format(estimate))

        out: List[ConcretePlan] = []
        for combo in itertools.product(*per_edge):
            labels = dict(fixed)
            ok = True
            for be in combo:
                src_label = be.pair.dst_label if be.swapped else be.pair.src_label
                dst_label = be.pair.src_label if be.swapped else be.pair.dst_label
                # A variable shared between hops must mean the same thing in both.
                # This is what stops a two-hop pattern multiplying out.
                for var, lbl in ((be.src_var, src_label), (be.dst_var, dst_label)):
                    if labels.setdefault(var, lbl) != lbl:
                        ok = False
                        break
                if not ok:
                    break
            if ok:
                out.append(ConcretePlan(plan=plan, labels=labels, edges=list(combo)))
            if len(out) > self.max_branches:
                raise BranchLimitExceeded(
                    "this pattern matches more than {} different combinations of "
                    "record type. Add a label, name the relationship type, or select "
                    "some nodes first.".format(self.max_branches))
        return out

    # ------------------------------------------------------------- emitting SQL

    def _aliases(self, cp: ConcretePlan) -> Dict[str, str]:
        gen: Dict[str, str] = {}
        for i, v in enumerate(cp.plan.vertices):
            gen[v.var] = "v{}".format(i)
        for i, e in enumerate(cp.plan.edges):
            gen[e.var] = "e{}".format(i)
        return gen

    def _from_join(self, cp: ConcretePlan, gen: Dict[str, str],
                   params: List[Any], ptypes: List[Any]) -> Tuple[str, List[str]]:
        """The FROM/JOIN chain, plus the extra WHERE terms it needs."""
        q = self.dialect.quote
        extra: List[str] = []

        if not cp.edges:
            var = cp.plan.vertices[0].var
            ns = self.node_schemas[cp.labels[var]]
            return "FROM {} AS {}".format(self._t(ns.table), gen[var]), extra

        first = cp.edges[0].src_var
        lines = ["FROM {} AS {}".format(
            self._t(self.node_schemas[cp.labels[first]].table), gen[first])]

        for i, be in enumerate(cp.edges):
            rs = self.rel_schemas[be.rel_type]
            ev, sv, dv = gen[be.var], gen[be.src_var], gen[be.dst_var]
            src_ns = self.node_schemas[cp.labels[be.src_var]]
            dst_ns = self.node_schemas[cp.labels[be.dst_var]]
            pair = be.pair
            # Which column on the link table points back at the vertex we already
            # have, and which points at the next one.
            near_col = pair.dst_col if be.swapped else pair.src_col
            far_col = pair.src_col if be.swapped else pair.dst_col
            near_prefix = pair.dst_prefix if be.swapped else pair.src_prefix
            far_prefix = pair.src_prefix if be.swapped else pair.dst_prefix

            if rs.resolution in ("fk", "prefixed_fk"):
                lines.append("JOIN {} AS {} ON {}.{} = {}.{}".format(
                    self._t(rs.table), ev, ev, q(near_col), sv, q(src_ns.primary_key)))
                lines.append("JOIN {} AS {} ON {}.{} = {}.{}".format(
                    self._t(dst_ns.table), dv, dv, q(dst_ns.primary_key), ev, q(far_col)))
                if rs.resolution == "prefixed_fk":
                    # Redundant against the joins, but it prunes the link table before
                    # the entity tables are probed. Skip it when an id set already
                    # drives the plan, where it is pure overhead.
                    if not self._has_id_filter(cp) and near_prefix:
                        extra.append("{}.{} LIKE {}".format(ev, q(near_col),
                                                            self._lit(near_prefix + "%", params, ptypes)))
                    if not self._has_id_filter(cp) and far_prefix:
                        extra.append("{}.{} LIKE {}".format(ev, q(far_col),
                                                            self._lit(far_prefix + "%", params, ptypes)))
            else:  # link_end
                le = rs.link_end
                s_alias, d_alias = ev + "s", ev + "d"
                lines.append(
                    "JOIN {} AS {} ON {}.{} = {}.{} AND {}.{} = {} AND {}.{} = {}".format(
                        self._t(le.table), s_alias,
                        s_alias, q(le.record_id_column), sv, q(src_ns.primary_key),
                        s_alias, q(le.link_table_column), self._lit(rs.table, params, ptypes),
                        s_alias, q(le.end_column),
                        self._lit(le.dst_end_value if be.swapped else le.src_end_value, params, ptypes)))
                lines.append("JOIN {} AS {} ON {}.{} = {}.{}".format(
                    self._t(rs.table), ev, ev, q(rs.primary_key), s_alias, q(le.link_id_column)))
                lines.append(
                    "JOIN {} AS {} ON {}.{} = {}.{} AND {}.{} = {} AND {}.{} = {}".format(
                        self._t(le.table), d_alias,
                        d_alias, q(le.link_id_column), s_alias, q(le.link_id_column),
                        d_alias, q(le.link_table_column), self._lit(rs.table, params, ptypes),
                        d_alias, q(le.end_column),
                        self._lit(le.src_end_value if be.swapped else le.dst_end_value, params, ptypes)))
                lines.append("JOIN {} AS {} ON {}.{} = {}.{}".format(
                    self._t(dst_ns.table), dv, dv, q(dst_ns.primary_key),
                    d_alias, q(le.record_id_column)))

            # Cypher forbids one relationship appearing twice in a single path.
            # PostgreSQL's graph engine gives that for free; a plain JOIN does not,
            # so without this a two-hop pattern returns every a->b->a bounce-back.
            for j, prev in enumerate(cp.edges[:i]):
                if self.rel_schemas[prev.rel_type].table == rs.table:
                    extra.append("{}.{} <> {}.{}".format(
                        ev, q(rs.primary_key), gen[prev.var], q(rs.primary_key)))
        return "\n".join(lines), extra

    def _has_id_filter(self, cp: ConcretePlan) -> bool:
        return any(v.id_refs for v in cp.plan.vertices)

    def _lit(self, value: Any, params: List[Any], ptypes: List[Any]) -> str:
        params.append(value)
        ptypes.append(None)
        return "?"

    def _columns(self, cp: ConcretePlan, gen: Dict[str, str]) -> Tuple[List[str], Dict[str, Any], List[str]]:
        """The SELECT list, the manifest that reads it back, and the sort keys."""
        q = self.dialect.quote
        cols: List[str] = []
        manifest: Dict[str, Any] = {"vertices": [], "edges": []}
        sort_keys: List[str] = []

        for i, v in enumerate(cp.plan.vertices):
            label = cp.labels.get(v.var)
            if not label:
                continue
            ns = self.node_schemas[label]
            gv = gen[v.var]
            kcol = "__gx_v{}_k0".format(i)
            cols.append("{}.{} AS {}".format(gv, q(ns.primary_key), kcol))
            sort_keys.append("{}.{}".format(gv, q(ns.primary_key)))
            prop_cols: Dict[str, str] = {}
            for j, prop in enumerate(p for p in ns.properties if p != ns.primary_key):
                c = "__gx_v{}_p{}".format(i, j)
                cols.append("{}.{} AS {}".format(gv, q(prop), c))
                prop_cols[c] = prop
            manifest["vertices"].append({"var": v.var, "alias": label, "key_cols": [kcol],
                                         "prop_cols": prop_cols, "pk_prop": ns.primary_key})

        for i, be in enumerate(cp.edges):
            rs = self.rel_schemas[be.rel_type]
            ev = gen[be.var]
            kcol = "__gx_e{}_k0".format(i)
            # The edge's OWN key. Endpoint keys are not enough: iBase lets the same
            # two records be linked several times by one link type, and those edges
            # would otherwise share an id and collapse to a single line on the canvas.
            cols.append("{}.{} AS {}".format(ev, q(rs.primary_key), kcol))
            sort_keys.append("{}.{}".format(ev, q(rs.primary_key)))
            prop_cols = {}
            for j, prop in enumerate(rs.properties):
                c = "__gx_e{}_p{}".format(i, j)
                cols.append("{}.{} AS {}".format(ev, q(prop), c))
                prop_cols[c] = prop
            # start_var/end_var follow the mapping's source and target, not the
            # pattern's left and right, so a reverse arrow still produces endpoint
            # ids that match the node ids.
            start_var = be.dst_var if be.swapped else be.src_var
            end_var = be.src_var if be.swapped else be.dst_var
            manifest["edges"].append({
                "var": be.var, "alias": be.rel_type, "key_cols": [kcol],
                "start_var": start_var, "end_var": end_var,
                "src_alias": be.pair.src_label, "dst_alias": be.pair.dst_label,
                "prop_cols": prop_cols})
        return cols, manifest, sort_keys

    def _where(self, cp: ConcretePlan, gen: Dict[str, str], params: List[Any],
               ptypes: List[Any], extra: Sequence[str]) -> str:
        terms = list(extra)
        if cp.plan.where is not None:
            ctx = predicate.SqlCtx(
                gen=gen, label_of=cp.labels,
                node_pk={l: s.primary_key for l, s in self.node_schemas.items()},
                registry=self.registry, params=params, dialect=self.dialect,
                param_types=ptypes, column_types=self.mapping.column_types,
                node_columns=self.mapping.columns)
            terms.append(predicate.to_sql(cp.plan.where, ctx))
        return "WHERE " + " AND ".join(t for t in terms if t) if terms else ""

    def compile(self, cp: ConcretePlan) -> CompiledQuery:
        gen = self._aliases(cp)
        params: List[Any] = []
        ptypes: List[Any] = []
        from_sql, extra = self._from_join(cp, gen, params, ptypes)
        cols, manifest, sort_keys = self._columns(cp, gen)
        where_sql = self._where(cp, gen, params, ptypes, extra)

        plan = cp.plan
        order = self._order_terms(plan, gen, cp)
        # T-SQL only allows OFFSET after an ORDER BY, and an unsorted page can repeat
        # a row it already returned, so the sort is always supplied.
        order_sql = tsql.order_by(order, sort_keys)

        head = tsql.top_clause(plan.limit, plan.skip)
        parts = [head, "    " + ",\n    ".join(cols), from_sql]
        if where_sql:
            parts.append(where_sql)
        if order_sql:
            parts.append(order_sql)
        tail = tsql.offset_fetch(plan.limit, plan.skip)
        if tail:
            parts.append(tail)
        sql = "\n".join(parts) + ";"
        return CompiledQuery(sql, params, manifest, ptypes)

    def _order_terms(self, plan: MatchPlan, gen: Dict[str, str], cp: ConcretePlan) -> List[str]:
        out = []
        for term, direction in (plan.order or []):
            if "." in term:
                var, prop = term.split(".", 1)
                if var in gen:
                    out.append("{}.{} {}".format(gen[var], self.dialect.quote(prop), direction))
        return out

    # ------------------------------------------------------------- Backend API

    def schema_response(self, db_name: str) -> Dict[str, Any]:
        return build_schema_response(db_name, self.node_schemas, self.rel_schemas)

    def _run(self, sql: str, params: Sequence[Any], param_types: Sequence[Any] = ()):
        if self.connection is None:
            raise NotImplementedError("no database connection - compile-only mode")
        return self.connection.run(sql, params, param_types)

    def execute(self, plan: MatchPlan) -> GraphResult:
        out = GraphResult()
        plans = self.concrete_plans(plan)
        if not plans:
            return out
        for cp in plans:
            compiled = self.compile(cp)
            self.last_sql = compiled.sql
            rows = self._run(compiled.sql, compiled.params, compiled.param_types)
            part = convert_rows(rows, compiled.manifest, id_fn=self.registry.encode)
            out.nodes.extend(part.nodes)
            out.relationships.extend(part.relationships)
        # Each branch applied the LIMIT on its own, so five branches could return five
        # times what was asked for. Trim on the way out. Paging across branches stays
        # best-effort, as it is in the PostgreSQL bridge.
        if plan.limit is not None and len(out.relationships) > plan.limit:
            keep = {r.id for r in out.relationships[:plan.limit]}
            out.relationships = [r for r in out.relationships if r.id in keep][:plan.limit]
            live = {r.startNodeId for r in out.relationships} | {r.endNodeId for r in out.relationships}
            out.nodes = [n for n in out.nodes if n.id in live]
        elif plan.limit is not None and not out.relationships and len(out.nodes) > plan.limit:
            del out.nodes[plan.limit:]
        return out

    def node_count(self, label: Optional[str] = None) -> int:
        labels = [self._canon(label)] if label else list(self.node_schemas)
        total = 0
        for l in labels:
            ns = self.node_schemas.get(l)
            if ns is None:
                continue
            total += self._scalar("SELECT COUNT_BIG(*) AS n FROM {}".format(self._t(ns.table)))
        return total

    def rel_count(self, rel_type: Optional[str]) -> int:
        types = [self._canon(rel_type)] if rel_type else list(self.rel_schemas)
        total = 0
        for t in types:
            rs = self.rel_schemas.get(t)
            if rs is None:
                continue
            total += self._scalar("SELECT COUNT_BIG(*) AS n FROM {}".format(self._t(rs.table)))
        return total

    def _scalar(self, sql: str) -> int:
        self.last_sql = sql
        rows = self._run(sql, [])
        return int(rows[0]["n"]) if rows else 0

    def sample(self, limit: int) -> GraphResult:
        """`MATCH (n) RETURN n` — a spread across labels rather than all of one."""
        out = GraphResult()
        labels = list(self.node_schemas)
        if not labels:
            return out
        each = max(1, limit // len(labels))
        for label in labels:
            ns = self.node_schemas[label]
            q = self.dialect.quote
            cols = ["{}.{} AS __gx_v0_k0".format("v0", q(ns.primary_key))]
            prop_cols = {}
            for j, prop in enumerate(p for p in ns.properties if p != ns.primary_key):
                c = "__gx_v0_p{}".format(j)
                cols.append("v0.{} AS {}".format(q(prop), c))
                prop_cols[c] = prop
            sql = ("SELECT TOP ({}) \n    {}\nFROM {} AS v0\nORDER BY v0.{};"
                   .format(int(each), ",\n    ".join(cols), self._t(ns.table), q(ns.primary_key)))
            self.last_sql = sql
            rows = self._run(sql, [])
            manifest = {"vertices": [{"var": "n", "alias": label, "key_cols": ["__gx_v0_k0"],
                                      "prop_cols": prop_cols, "pk_prop": ns.primary_key}],
                        "edges": []}
            part = convert_rows(rows, manifest, id_fn=self.registry.encode)
            out.nodes.extend(part.nodes)
            if len(out.nodes) >= limit:
                break
        del out.nodes[limit:]
        return out

    def project(self, plan: MatchPlan, projections, distinct: bool = False):
        header = [alias for (_v, _p, alias) in projections]
        rows_out: List[List[Any]] = []
        seen = set()
        for cp in self.concrete_plans(plan):
            gen = self._aliases(cp)
            params: List[Any] = []
            ptypes: List[Any] = []
            from_sql, extra = self._from_join(cp, gen, params, ptypes)
            where_sql = self._where(cp, gen, params, ptypes, extra)
            q = self.dialect.quote
            cols, sort_map, ok = [], {}, True
            for i, (var, prop, alias) in enumerate(projections):
                if var not in gen:
                    ok = False
                    break
                cols.append("{}.{} AS c{}".format(gen[var], q(prop), i))
                sort_map["{}.{}".format(var, prop)] = "c{}".format(i)
                sort_map[alias] = "c{}".format(i)
            if not ok:
                continue
            order = []
            for term, direction in (plan.order or []):
                if term in sort_map:
                    order.append("{} {}".format(sort_map[term], direction))
            head = tsql.top_clause(plan.limit, plan.skip, distinct=distinct)
            parts = [head, "    " + ",\n    ".join(cols), from_sql]
            if where_sql:
                parts.append(where_sql)
            # With DISTINCT the sort may only name selected columns, so no key
            # tiebreakers here; c0 keeps the page order stable enough.
            order_sql = tsql.order_by(order, [] if distinct else ["c0"])
            if order_sql:
                parts.append(order_sql)
            tail = tsql.offset_fetch(plan.limit, plan.skip)
            if tail:
                parts.append(tail)
            sql = "\n".join(parts) + ";"
            self.last_sql = sql
            for r in self._run(sql, params, ptypes):
                row = [r.get("c{}".format(i)) for i in range(len(projections))]
                if distinct:
                    k = tuple(row)
                    if k in seen:
                        continue
                    seen.add(k)
                rows_out.append(row)
        return header, rows_out

    def aggregate(self, plan: MatchPlan, group_keys, aggs):
        header = [a for (_v, _p, a) in group_keys] + [a for (_f, _v, _p, a, _d) in aggs]
        plans = self.concrete_plans(plan)
        if not plans:
            return header, []
        q = self.dialect.quote

        needed: List[Tuple[str, str]] = [(v, p) for (v, p, _a) in group_keys]
        for (fn, v, p, _a, d) in aggs:
            if p:
                needed.append((v, p))
            elif v is not None and d:
                label = None
                for cp in plans:
                    label = cp.labels.get(v)
                    if label:
                        break
                if label and label in self.node_schemas:
                    needed.append((v, self.node_schemas[label].primary_key))
        ordered: List[Tuple[str, str]] = []
        for np in needed:
            if np not in ordered:
                ordered.append(np)
        colmap = {np: "c{}".format(i) for i, np in enumerate(ordered)}

        inners, params, ptypes = [], [], []
        for cp in plans:
            gen = self._aliases(cp)
            from_sql, extra = self._from_join(cp, gen, params, ptypes)
            where_sql = self._where(cp, gen, params, ptypes, extra)
            cols = ["{}.{} AS {}".format(gen[var], q(prop), colmap[(var, prop)])
                    for (var, prop) in ordered if var in gen] or ["1 AS c0"]
            inner = "SELECT\n    {}\n{}".format(",\n    ".join(cols), from_sql)
            if where_sql:
                inner += "\n" + where_sql
            inners.append(inner)
        if not inners:
            return header, []

        sel, group = [], []
        for (v, p, _a) in group_keys:
            c = colmap[(v, p)]
            sel.append(c)
            group.append(c)
        for (fn, v, p, _a, d) in aggs:
            if p:
                arg = colmap[(v, p)]
            elif v is not None and d:
                arg = colmap.get((v, self.node_schemas[plans[0].labels.get(v, "")].primary_key)) \
                    if plans[0].labels.get(v) in self.node_schemas else "*"
            else:
                arg = "*"
            if d and arg != "*":
                arg = "DISTINCT " + arg
            # AVG over an integer column does INTEGER division in T-SQL, and SUM over
            # int overflows rather than widening. Both are silent.
            sel.append(tsql.agg_expr(fn, arg, "int" if fn in ("avg", "sum") else None))

        union = "\nUNION ALL\n".join(inners)
        sql = "{} {}\nFROM (\n{}\n) AS u".format(
            tsql.top_clause(plan.limit, plan.skip), ", ".join(sel), union)
        if group:
            sql += "\nGROUP BY " + ", ".join(group)
        order = []
        for term, direction in (plan.order or []):
            order.append("{} {}".format(term, direction))
        order_sql = tsql.order_by(order)
        if order_sql:
            sql += "\n" + order_sql
        tail = tsql.offset_fetch(plan.limit, plan.skip)
        if tail:
            sql += "\n" + (order_sql and "" or tsql.order_by([], ["1"])) + tail
        sql += ";"
        self.last_sql = sql
        rows = self._run(sql, params, ptypes)
        width = len(header)
        out = []
        for r in rows:
            vals = list(r.values())
            out.append(vals[:width])
        return header, out
