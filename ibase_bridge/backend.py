"""Shared types and the Backend contract.

Everything a query touches flows through these dataclasses, so the same Cypher
front-end can drive either a real SQL Server or an in-memory fixture.

No third-party imports here on purpose: the core pipeline (translator, id codec,
converter, processor) must import under plain CPython, so the whole thing can be
tested with neither FastAPI nor a database driver installed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ----- schema descriptors -----

@dataclass
class NodeSchema:
    label: str
    primary_key: str                       # property name that identifies the vertex
    properties: Dict[str, str]             # property name -> SQL type (from the mapping)
    table: str = ""                        # physical table (defaults to the label)
    keys: List[str] = field(default_factory=list)   # full key column list (composite-safe)
    id_prefix: Optional[str] = None        # iBase record-id prefix, e.g. "PER"

    def __post_init__(self):
        if not self.table:
            self.table = self.label
        if not self.keys:
            self.keys = [self.primary_key]


@dataclass
class EndpointPair:
    """One concrete (source label, target label) a link type may connect.

    A plain foreign-key edge has exactly one of these. An iBase link type is
    polymorphic — `Associate` may join Person->Person, Person->Organization and
    Organization->Vehicle — so it carries several, and the backend produces one
    concrete query per pair.
    """
    src_label: str
    dst_label: str
    src_col: str = ""                      # column holding the source record id
    dst_col: str = ""
    src_prefix: Optional[str] = None       # optional sargable pre-filter, e.g. "PER"
    dst_prefix: Optional[str] = None
    src_type_value: Optional[str] = None   # for a discriminator-column mapping
    dst_type_value: Optional[str] = None
    row_estimate: int = 0                  # observed count, used to pick a representative

    def swapped(self) -> "EndpointPair":
        return EndpointPair(
            src_label=self.dst_label, dst_label=self.src_label,
            src_col=self.dst_col, dst_col=self.src_col,
            src_prefix=self.dst_prefix, dst_prefix=self.src_prefix,
            src_type_value=self.dst_type_value, dst_type_value=self.src_type_value,
            row_estimate=self.row_estimate,
        )


@dataclass
class LinkEndSpec:
    """How to reach a link's endpoints through iBase's `_LinkEnd` system table.

    Every column name here is CONFIGURED, never assumed. Public i2 documentation
    confirms `_LinkEnd` holds link endpoints but does not publish its columns, and
    they vary between iBase versions. `discover` reads the real names off the
    server; `validate` fails loudly if they are wrong.
    """
    table: str = "_LinkEnd"
    link_table_column: str = "LinkTable"   # which link type a row belongs to
    link_id_column: str = "LinkId"
    end_column: str = "End"
    record_id_column: str = "RecordId"
    src_end_value: Any = 1
    dst_end_value: Any = 2


@dataclass
class RelSchema:
    type: str
    endpoints: List[EndpointPair]          # every (source, target) pair this type allows
    table: str = ""                        # physical link table (defaults to the type)
    primary_key: str = ""                  # the link table's OWN key — edge ids come from it
    resolution: str = "fk"                 # "fk" | "prefixed_fk" | "link_end"
    link_end: Optional[LinkEndSpec] = None
    properties: Dict[str, str] = field(default_factory=dict)
    keys: List[str] = field(default_factory=list)

    def __post_init__(self):
        if not self.table:
            self.table = self.type
        if not self.keys and self.primary_key:
            self.keys = [self.primary_key]

    # --- representative pair -------------------------------------------------
    # The KoreDB schema shape is a dict keyed by type name, so a polymorphic type
    # cannot appear twice. We report its most common pair and list the rest under
    # a non-standard `endpointPairs` key. See build_schema_response below.

    def representative(self) -> EndpointPair:
        return max(self.endpoints, key=lambda e: e.row_estimate) if self.endpoints else \
            EndpointPair(src_label="", dst_label="")

    @property
    def src_label(self) -> str:
        return self.representative().src_label

    @property
    def dst_label(self) -> str:
        return self.representative().dst_label

    def pairs_touching(self, labels) -> List[EndpointPair]:
        """Endpoint pairs with at least one end in `labels` — the pruning step that
        keeps an untyped expand from fanning out across the whole schema."""
        want = set(labels)
        return [e for e in self.endpoints
                if e.src_label in want or e.dst_label in want]


# ----- query intermediate representation (produced by cypher_translator) -----

@dataclass
class VertexPat:
    var: str
    label: Optional[str] = None
    # id(var) IN [internal_id(t,o), ...] references, decoded by the backend:
    id_refs: Optional[List[Tuple[int, int]]] = None
    id_negated: bool = False


@dataclass
class EdgePat:
    var: str
    types: Optional[List[str]]             # None = any edge type
    direction: str                         # "out" | "in" | "both"
    src_var: str
    dst_var: str


@dataclass
class MatchPlan:
    vertices: List[VertexPat]
    edges: List[EdgePat]
    where: Any = None                          # predicate expression tree (see predicate.py)
    order: List[Tuple[str, str]] = field(default_factory=list)   # [(term, 'ASC'|'DESC')]
    limit: Optional[int] = None
    skip: int = 0                              # Kineviz paginates with SKIP n LIMIT m
    return_items: Optional[List[str]] = None   # bare vars / "*"; informational

    def vertex(self, var: str) -> Optional[VertexPat]:
        for v in self.vertices:
            if v.var == var:
                return v
        return None


@dataclass
class BoundEdge:
    """One edge of a ConcretePlan, with every choice already made."""
    var: str
    rel_type: str
    pair: EndpointPair
    swapped: bool                          # True when the pattern runs against the
                                           # schema direction (a reverse arrow)
    src_var: str                           # pattern variable on the left
    dst_var: str                           # pattern variable on the right


@dataclass
class ConcretePlan:
    """A MatchPlan with no ambiguity left.

    Every variable is bound to exactly one label; every edge to exactly one type,
    one endpoint pair and one orientation. Compilation is total on one of these —
    no runtime branching, no dynamic manifest.

    Turning a MatchPlan into a list of ConcretePlans is the single place where
    untyped edges, alternations and polymorphic endpoints are resolved. In the
    PostgreSQL bridge that logic was spread across `_resolve_labels`,
    `_compatible_typed_plans` and `query_processor._expand_branches`.
    """
    plan: MatchPlan
    labels: Dict[str, str]                 # pattern variable -> node label
    edges: List[BoundEdge]

    def label_of(self, var: str) -> Optional[str]:
        return self.labels.get(var)


# ----- results -----

@dataclass
class GraphNode:
    id: str
    labels: List[str]
    properties: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "labels": self.labels, "properties": self.properties}


@dataclass
class GraphRel:
    id: str
    startNodeId: str
    endNodeId: str
    type: str
    properties: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "startNodeId": self.startNodeId,
            "endNodeId": self.endNodeId,
            "type": self.type,
            "properties": self.properties,
        }


@dataclass
class GraphResult:
    nodes: List[GraphNode] = field(default_factory=list)
    relationships: List[GraphRel] = field(default_factory=list)

    def to_payload(self) -> Dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "relationships": [r.to_dict() for r in self.relationships],
        }


# The outcome a QueryProcessor hands back to the HTTP / simulator layer.
@dataclass
class QueryOutcome:
    type: str                              # "GRAPH" | "TABLE" | "SCHEMA"
    data: Any
    summary: Dict[str, Any] = field(default_factory=dict)


class Backend(ABC):
    """A source of graph data the bridge can query.

    Implementation: MssqlBackend. A fixture backend can stand in for tests.
    """

    node_schemas: Dict[str, NodeSchema]
    rel_schemas: Dict[str, RelSchema]

    def labels(self) -> List[str]:
        return list(self.node_schemas.keys())

    def rel_types(self) -> List[str]:
        return list(self.rel_schemas.keys())

    @abstractmethod
    def schema_response(self, db_name: str) -> Dict[str, Any]:
        """Kineviz-shaped schema: {<db>: {categories, relationships}}."""

    @abstractmethod
    def node_count(self, label: Optional[str] = None) -> int:
        """Count all vertices, or vertices of `label` when given."""

    @abstractmethod
    def rel_count(self, rel_type: Optional[str]) -> int: ...

    @abstractmethod
    def sample(self, limit: int) -> GraphResult:
        """Round-robin sample across vertex labels (untyped MATCH (n) RETURN n)."""

    @abstractmethod
    def execute(self, plan: MatchPlan) -> GraphResult:
        """Run a translated MATCH plan and return graph elements."""

    @abstractmethod
    def project(self, plan: MatchPlan, projections: List[Tuple[str, str, str]], distinct: bool = False):
        """Scalar RETURN (e.g. `RETURN a.name, b.id`) → (header, rows).

        projections: list of (var, prop, alias). Returns a header list and a list
        of row value-lists — the caller wraps them into a TABLE.
        """

    @abstractmethod
    def aggregate(self, plan: MatchPlan, group_keys, aggs):
        """Grouped aggregation → (header, rows).

        group_keys: [(var, prop, alias)] — the implicit GROUP BY columns.
        aggs: [(fn, var, prop_or_None, alias)] where fn in count/sum/avg/min/max
        (prop is None for `count(*)` / `count(var)`).
        """


def build_schema_response(
    db_name: str,
    node_schemas: Dict[str, NodeSchema],
    rel_schemas: Dict[str, RelSchema],
) -> Dict[str, Any]:
    """The Kineviz schema shape, shared by every backend (see doc §3A.6)."""
    categories: Dict[str, Any] = {}
    for label, s in node_schemas.items():
        categories[label] = {
            "name": label,
            "props": list(s.properties.keys()),
            "propsTypes": dict(s.properties),
            "keys": [s.primary_key],
            "keysTypes": {s.primary_key: s.properties.get(s.primary_key, "STRING")},
        }
    relationships: Dict[str, Any] = {}
    for rtype, s in rel_schemas.items():
        rep = s.representative()
        entry = {
            "name": rtype,
            "props": list(s.properties.keys()),
            "propsTypes": dict(s.properties),
            "keys": [],
            "keysTypes": {},
            # This shape is a dict keyed by type name, so a polymorphic link type
            # physically cannot appear more than once. We report its most common
            # endpoint pair here...
            "startCategory": rep.src_label,
            "endCategory": rep.dst_label,
        }
        if len(s.endpoints) > 1:
            # ...and list every pair under a key Kineviz does not (yet) read.
            # Unknown keys are ignored by clients, so this costs nothing today and
            # is already in place if support arrives.
            entry["endpointPairs"] = [
                {"startCategory": e.src_label, "endCategory": e.dst_label}
                for e in s.endpoints
            ]
        relationships[rtype] = entry
    return {db_name: {"categories": categories, "relationships": relationships}}
