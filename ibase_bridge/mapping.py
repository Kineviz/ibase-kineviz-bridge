"""Load and check the mapping file.

SQL Server has no idea what a graph is. This file is where a human says which
tables are dots, which are lines, and how a line finds its two ends. Everything
else in the bridge follows from it, so it is worth validating hard and failing
early with a message that names the problem.

The format extends the one in the connector specification (its section 6) with an
``endpoints`` list, because a single iBase link type can join several different
kinds of record and the original one-source-one-target shape cannot say that.

Nothing here guesses at physical column names. iBase's real column names are not
publicly documented and vary between versions; ``discovery`` reads them off the
server and proposes a draft, and a person reviews it before it is used.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .backend import EndpointPair, LinkEndSpec, NodeSchema, RelSchema

SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

RESOLUTIONS = ("fk", "prefixed_fk", "link_end")

DEFAULT_TUNING = {
    "id_list_inline_max": 200,
    "id_list_chunk_size": 1000,
    "recompile_threshold": 50,
    "max_branches": 64,
    "case_insensitive_like": "auto",
}


class MappingError(Exception):
    """A problem with the mapping file, phrased for the person who wrote it."""


class Mapping:
    def __init__(self, raw: Dict[str, Any], path: Optional[str] = None):
        self.raw = raw
        self.path = path
        self.source: Dict[str, Any] = raw.get("source") or {}
        self.tuning: Dict[str, Any] = dict(DEFAULT_TUNING, **(raw.get("tuning") or {}))
        self.ibase: Dict[str, Any] = raw.get("ibase") or {}
        self.node_schemas: Dict[str, NodeSchema] = {}
        self.rel_schemas: Dict[str, RelSchema] = {}
        # "Label.property" -> physical column / SQL type, used by the WHERE compiler
        # so a parameter is bound as the type the column really is.
        self.columns: Dict[str, str] = {}
        self.column_types: Dict[str, str] = {}

    @property
    def schema(self) -> str:
        return self.source.get("schema") or "dbo"

    @property
    def query_timeout(self) -> int:
        return int(self.source.get("query_timeout_seconds") or 120)

    def connection_string(self) -> str:
        env = self.source.get("connection_env")
        if not env:
            raise MappingError("source.connection_env is required (name the environment "
                               "variable holding the connection string; never the "
                               "password itself)")
        value = os.environ.get(env)
        if not value:
            raise MappingError(
                "environment variable {!r} is not set. It should hold the SQL Server "
                "connection string for a login with SELECT permission only.".format(env))
        return value

    def label_order(self) -> List[str]:
        """Labels then link types, in file order.

        This ordering *is* the first half of every node id, so it must stay stable.
        Appending a new label is safe; reordering existing ones invalidates the ids
        in any saved Kineviz project.
        """
        return list(self.node_schemas.keys()) + list(self.rel_schemas.keys())

    def key_types(self) -> Dict[str, str]:
        out = {l: s.properties.get(s.primary_key, "STRING") for l, s in self.node_schemas.items()}
        for t, r in self.rel_schemas.items():
            out[t] = r.properties.get(r.primary_key, "STRING")
        return out


# ---------------------------------------------------------------- validation

def _ident(value: Any, where: str) -> str:
    if not isinstance(value, str) or not SAFE_IDENT.match(value):
        raise MappingError("{}: {!r} is not a valid SQL identifier "
                           "(letters, digits and underscore, not starting with a digit)"
                           .format(where, value))
    return value


def _require(d: Dict[str, Any], key: str, where: str) -> Any:
    if key not in d or d[key] in (None, "", []):
        raise MappingError("{}: missing required field {!r}".format(where, key))
    return d[key]


def _node_from(raw: Dict[str, Any], idx: int) -> NodeSchema:
    where = "nodes[{}]".format(idx)
    label = _ident(_require(raw, "label", where), where + ".label")
    table = _ident(raw.get("table") or label, where + ".table")

    if "keys" in raw and raw["keys"]:
        keys = list(raw["keys"])
        if len(keys) > 1:
            # The WHERE compiler and the id codec both assume one key column. Say so
            # now rather than silently filtering on the first one and returning
            # plausible-looking but wrong results.
            raise MappingError(
                "{}: composite keys are not supported yet (label {!r} declares {}). "
                "Use a single key column, or add a surrogate key column to the view."
                .format(where, label, keys))
        key = _ident(keys[0], where + ".keys")
    else:
        key = _ident(_require(raw, "key", where), where + ".key")

    props = [_ident(p, where + ".properties") for p in (raw.get("properties") or [])]
    # `types` is optional and normally written by `discover`. It matters more than it
    # looks: pyodbc binds every Python string as nvarchar, and an nvarchar parameter
    # compared against a varchar or bigint column makes SQL Server convert THE COLUMN,
    # which turns an index lookup into a full table scan with nothing in the output to
    # say so. Knowing the real type lets us bind the parameter correctly.
    declared = {k: str(v) for k, v in (raw.get("types") or {}).items()}
    properties = {p: declared.get(p, "STRING") for p in props}
    properties.setdefault(key, declared.get(key, "STRING"))
    return NodeSchema(label=label, primary_key=key, properties=properties,
                      table=table, keys=[key], id_prefix=raw.get("id_prefix"))


def _endpoints_from(raw: Dict[str, Any], where: str, resolution: str) -> List[EndpointPair]:
    ep_raw = raw.get("endpoints")
    if not ep_raw:
        raise MappingError("{}: missing required field 'endpoints'".format(where))
    if isinstance(ep_raw, dict):
        ep_raw = [ep_raw]

    pairs: List[EndpointPair] = []
    for j, ep in enumerate(ep_raw):
        w = "{}.endpoints[{}]".format(where, j)
        src, dst = ep.get("src") or {}, ep.get("dst") or {}
        if not src or not dst:
            raise MappingError("{}: each endpoint needs both 'src' and 'dst'".format(w))
        src_label = _ident(_require(src, "label", w + ".src"), w + ".src.label")
        dst_label = _ident(_require(dst, "label", w + ".dst"), w + ".dst.label")
        # link_end resolution reads the endpoints out of the _LinkEnd table, so a
        # column on the link table itself is not required there.
        need_col = resolution in ("fk", "prefixed_fk")
        src_col = _ident(src["column"], w + ".src.column") if src.get("column") else ""
        dst_col = _ident(dst["column"], w + ".dst.column") if dst.get("column") else ""
        if need_col and not (src_col and dst_col):
            raise MappingError("{}: resolution {!r} needs a 'column' on both src and dst"
                               .format(w, resolution))
        pairs.append(EndpointPair(
            src_label=src_label, dst_label=dst_label,
            src_col=src_col, dst_col=dst_col,
            src_prefix=src.get("prefix"), dst_prefix=dst.get("prefix"),
            src_type_value=src.get("type_value"), dst_type_value=dst.get("type_value"),
            row_estimate=int(ep.get("row_estimate") or 0),
        ))
    return pairs


def _rel_from(raw: Dict[str, Any], idx: int) -> RelSchema:
    where = "edges[{}]".format(idx)
    rtype = _ident(_require(raw, "type", where), where + ".type")
    table = _ident(raw.get("table") or rtype, where + ".table")
    resolution = raw.get("resolution") or "fk"
    if resolution not in RESOLUTIONS:
        raise MappingError("{}: resolution must be one of {}, got {!r}"
                           .format(where, ", ".join(RESOLUTIONS), resolution))

    # The edge's OWN key is mandatory. Without it we would have to mint edge ids
    # from the two endpoint keys, and iBase lets the same two records be linked
    # more than once by the same link type - those edges would collide onto one id
    # and Kineviz would silently draw one line where there are several.
    key = _ident(_require(raw, "key", where + " (an edge needs its own primary key so "
                                            "parallel links stay distinct)"), where + ".key")

    props = [_ident(p, where + ".properties") for p in (raw.get("properties") or [])]
    declared = {k: str(v) for k, v in (raw.get("types") or {}).items()}
    properties = {p: declared.get(p, "STRING") for p in props}
    properties.setdefault(key, declared.get(key, "STRING"))

    link_end = None
    if resolution == "link_end":
        le = raw.get("link_end") or {}
        link_end = LinkEndSpec(
            table=le.get("table") or "_LinkEnd",
            link_table_column=le.get("link_table_column") or "LinkTable",
            link_id_column=le.get("link_id_column") or "LinkId",
            end_column=le.get("end_column") or "End",
            record_id_column=le.get("record_id_column") or "RecordId",
            src_end_value=le.get("src_end_value", 1),
            dst_end_value=le.get("dst_end_value", 2),
        )
        for f in ("table", "link_table_column", "link_id_column", "end_column", "record_id_column"):
            _ident(getattr(link_end, f).lstrip("_") or "x", where + ".link_end." + f)

    return RelSchema(type=rtype, endpoints=_endpoints_from(raw, where, resolution),
                     table=table, primary_key=key, resolution=resolution,
                     link_end=link_end, properties=properties, keys=[key])


def _cross_check(m: Mapping) -> None:
    """Every endpoint label must be a declared node. A typo here would otherwise
    show up as an edge type that silently never matches anything."""
    for rtype, rel in m.rel_schemas.items():
        if not rel.endpoints:
            raise MappingError("edge {!r} declares no endpoints".format(rtype))
        for ep in rel.endpoints:
            for label in (ep.src_label, ep.dst_label):
                if label not in m.node_schemas:
                    raise MappingError(
                        "edge {!r} points at node label {!r}, which is not declared "
                        "under `nodes`. Declared labels: {}"
                        .format(rtype, label, ", ".join(sorted(m.node_schemas)) or "(none)"))
    clash = set(m.node_schemas) & set(m.rel_schemas)
    if clash:
        raise MappingError("these names are used for both a node and an edge: {}"
                           .format(", ".join(sorted(clash))))


def from_dict(raw: Dict[str, Any], path: Optional[str] = None) -> Mapping:
    if not isinstance(raw, dict):
        raise MappingError("the mapping file must be a YAML mapping at the top level")
    if int(raw.get("version") or 0) < 1:
        raise MappingError("missing or unsupported `version` (expected 1 or 2)")

    m = Mapping(raw, path)
    for i, n in enumerate(raw.get("nodes") or []):
        node = _node_from(n, i)
        if node.label in m.node_schemas:
            raise MappingError("duplicate node label {!r}".format(node.label))
        m.node_schemas[node.label] = node
    if not m.node_schemas:
        raise MappingError("the mapping declares no nodes")

    for i, e in enumerate(raw.get("edges") or []):
        rel = _rel_from(e, i)
        if rel.type in m.rel_schemas:
            raise MappingError("duplicate edge type {!r}".format(rel.type))
        m.rel_schemas[rel.type] = rel

    _cross_check(m)

    for label, ns in m.node_schemas.items():
        for prop, sql_type in ns.properties.items():
            m.columns["{}.{}".format(label, prop)] = prop
            if sql_type and sql_type != "STRING":
                m.column_types["{}.{}".format(label, prop)] = sql_type
    for rtype, rs in m.rel_schemas.items():
        for prop, sql_type in rs.properties.items():
            m.columns["{}.{}".format(rtype, prop)] = prop
            if sql_type and sql_type != "STRING":
                m.column_types["{}.{}".format(rtype, prop)] = sql_type
    return m


def load(path: str) -> Mapping:
    import yaml
    if not os.path.exists(path):
        raise MappingError("mapping file not found: {}".format(path))
    with open(path) as fh:
        raw = yaml.safe_load(fh)
    return from_dict(raw, path)
