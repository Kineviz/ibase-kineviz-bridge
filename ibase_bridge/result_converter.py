"""Turn result rows plus a projection manifest into nodes and edges.

The outbound half: read the opaque `__gx_*` columns back into nodes and
relationships using the manifest the query compiler emitted, minting an id for
each. No server-side de-duplication — Kineviz builds the unique set itself.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from . import element_id
from .backend import GraphNode, GraphRel, GraphResult

IdFn = Callable[[str, Tuple[Any, ...]], str]


def convert_rows(rows: List[Dict[str, Any]], manifest: Dict[str, Any],
                 id_fn: Optional[IdFn] = None) -> GraphResult:
    """Build nodes/relationships from result rows. `id_fn(label, key)` mints
    each element id; defaults to the stable, stateless base64 `element_id`."""
    if id_fn is None:
        id_fn = element_id.encode
    out = GraphResult()
    for row in rows:
        keys_by_var: Dict[str, tuple] = {}

        for v in manifest.get("vertices", []):
            key_vals = tuple(row.get(c) for c in v["key_cols"])
            if all(k is None for k in key_vals):
                continue
            keys_by_var[v["var"]] = key_vals
            props = {prop: row.get(col) for col, prop in v["prop_cols"].items()}
            props = {k: val for k, val in props.items() if val is not None}
            # Kineviz expects the real primary key present as a node
            # property, not only encoded in the internal node id. key_cols[0] carries it.
            pk = v.get("pk_prop")
            if pk and pk not in props and key_vals and key_vals[0] is not None:
                props[pk] = key_vals[0]
            out.nodes.append(
                GraphNode(id=id_fn(v["alias"], key_vals), labels=[v["alias"]], properties=props)
            )

        for e in manifest.get("edges", []):
            # start_var/end_var are bound to the schema source/dest labels, so the
            # rel's endpoint ids match the corresponding vertex node ids.
            src_key = keys_by_var.get(e["start_var"])
            dst_key = keys_by_var.get(e["end_var"])
            if src_key is None or dst_key is None:
                continue
            props = {prop: row.get(col) for col, prop in e["prop_cols"].items()}
            props = {k: val for k, val in props.items() if val is not None}

            # The edge's id comes from the link table's OWN key, not from its two
            # endpoints. Endpoint keys are not unique: iBase lets the same two
            # records be linked several times by one link type (two Associate
            # records with different dates, say), and Kineviz de-duplicates by id,
            # so those edges would collapse to a single line with no error. Using
            # the link row's own key also makes the id direction-stable, so an
            # undirected match cannot draw the same edge twice.
            key_cols = e.get("key_cols")
            if key_cols:
                edge_key = tuple(row.get(c) for c in key_cols)
                if all(k is None for k in edge_key):
                    edge_key = src_key + dst_key      # no key projected; fall back
            else:
                edge_key = src_key + dst_key

            out.relationships.append(
                GraphRel(
                    id=id_fn(e["alias"], edge_key),
                    startNodeId=id_fn(e["src_alias"], src_key),
                    endNodeId=id_fn(e["dst_alias"], dst_key),
                    type=e["alias"],
                    properties=props,
                )
            )
    return out
