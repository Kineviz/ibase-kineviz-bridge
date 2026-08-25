"""Synthesising a unique node id, and getting it back again.

## The problem

SQL Server has no node ids. A row is identified by its table plus its primary
key, and in iBase that key is a string like ``PER0000123``. Kineviz needs one id
per dot on the canvas, and when you select dots and press Expand it hands those
ids straight back to us. So we have to invent an id that we can reverse.

## The constraint that decides the design

Kineviz's KoreDB connector does not treat an id as an opaque string. It splits it
on ``:`` and re-emits it as **two integers**::

    we return   id = "0:123"
    Kineviz sends back   WHERE id(n) IN [internal_id(0, 123)]

Verified against real captured traffic. So a base64 id such as
``WyJQZXJzb24iLFsiMTAwMSJdXQ`` cannot survive the round trip on this connector,
however tidy it looks. (It is fine on the Database Proxy connector, which does
round-trip opaque strings — see ``element_id``. That connector currently fails
its connection check inside GraphXR, so it is not the one we ship on.)

## The scheme

An id is ``"<table>:<offset>"``, two integers.

``table`` is the label's position in the mapping file. It is fixed by the mapping,
so it is the same in every process and after every restart.

``offset`` has to carry the primary key, and how it does that depends on the key:

===============  ==========================  ==================================
key looks like   strategy                    example
===============  ==========================  ==================================
1001 (a number)  ``direct``                  Person 1001      -> ``"0:1001"``
'PER0000123'     ``numeric_suffix``          Person PER0000123 -> ``"0:123"``
anything else    ``registry``                assigned in order, optionally saved
===============  ==========================  ==================================

The first two are pure arithmetic: no state, no memory, identical across restarts
and across machines. Between them they cover both the generic demo schema (integer
keys) and iBase's own record-id convention (a type prefix followed by digits),
which is why they are worth the trouble.

``registry`` is the fallback for keys that are neither. It hands out offsets in
the order rows are first seen, which means ids change if the bridge restarts —
the known weakness of the PostgreSQL bridge's scheme. Point ``state_path`` at a
file to keep them across restarts.

Two limits worth knowing. Kineviz is JavaScript, so an offset above 2**53 loses
precision; a key that large falls back to the registry. And a ``numeric_suffix``
label needs its keys to share one prefix and one width, or ``PER123`` and
``PER0000123`` would both encode to 123 — we check that when the strategy is
chosen and fall back to the registry if they disagree.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

KeyTuple = Tuple[Any, ...]

# Above this, a JavaScript number can no longer hold the value exactly.
JS_SAFE_INT = 2 ** 53 - 1

_NUMERIC_SUFFIX = re.compile(r"^([A-Za-z_]*)(\d+)$")

DIRECT = "direct"
NUMERIC_SUFFIX = "numeric_suffix"
REGISTRY = "registry"

INTEGER_SQL_TYPES = {"tinyint", "smallint", "int", "bigint", "INT64"}


class LabelCodec:
    """How one label's primary key turns into an integer offset, and back."""

    def __init__(self, label: str, table_id: int, strategy: str = REGISTRY,
                 prefix: str = "", width: int = 0):
        self.label = label
        self.table_id = table_id
        self.strategy = strategy
        self.prefix = prefix
        self.width = width
        # registry state
        self._to_offset: Dict[KeyTuple, int] = {}
        self._to_key: Dict[int, KeyTuple] = {}
        self._next = 0

    # -- choosing a strategy from a sample of real keys -----------------------

    @classmethod
    def infer(cls, label: str, table_id: int, sql_type: Optional[str] = None,
              samples: Optional[List[Any]] = None) -> "LabelCodec":
        """Pick the cheapest strategy that is provably reversible for these keys."""
        if (sql_type or "") in INTEGER_SQL_TYPES:
            return cls(label, table_id, DIRECT)

        samples = [s for s in (samples or []) if s is not None]
        if samples:
            prefixes, widths, ok = set(), set(), True
            for s in samples:
                m = _NUMERIC_SUFFIX.match(str(s))
                if not m or int(m.group(2)) > JS_SAFE_INT:
                    ok = False
                    break
                prefixes.add(m.group(1))
                widths.add(len(m.group(2)))
            # One prefix and one width, or we cannot rebuild the original string.
            if ok and len(prefixes) == 1 and len(widths) == 1:
                return cls(label, table_id, NUMERIC_SUFFIX,
                           prefix=next(iter(prefixes)), width=next(iter(widths)))
        return cls(label, table_id, REGISTRY)

    # -- encode / decode ------------------------------------------------------

    def offset_for(self, key: KeyTuple) -> Optional[int]:
        if len(key) != 1 or key[0] is None:
            return self._registry_offset(key)          # composite -> registry
        raw = key[0]
        if self.strategy == DIRECT:
            try:
                n = int(raw)
            except (TypeError, ValueError):
                return self._registry_offset(key)
            return n if 0 <= n <= JS_SAFE_INT else self._registry_offset(key)
        if self.strategy == NUMERIC_SUFFIX:
            m = _NUMERIC_SUFFIX.match(str(raw))
            if m and m.group(1) == self.prefix and len(m.group(2)) == self.width:
                n = int(m.group(2))
                if 0 <= n <= JS_SAFE_INT:
                    return n
            return self._registry_offset(key)
        return self._registry_offset(key)

    def key_for(self, offset: int) -> Optional[KeyTuple]:
        if self.strategy == DIRECT and offset not in self._to_key:
            return (offset,)
        if self.strategy == NUMERIC_SUFFIX and offset not in self._to_key:
            return (self.prefix + str(offset).zfill(self.width),)
        return self._to_key.get(offset)

    # -- registry fallback ----------------------------------------------------

    def _registry_offset(self, key: KeyTuple) -> int:
        off = self._to_offset.get(key)
        if off is None:
            # Start well above any arithmetic offset so the two never collide.
            off = max(self._next, JS_SAFE_INT // 2)
            self._next = off + 1
            self._to_offset[key] = off
            self._to_key[off] = key
        return off

    def state(self) -> Dict[str, Any]:
        return {"strategy": self.strategy, "prefix": self.prefix, "width": self.width,
                "next": self._next,
                "pairs": [[list(k), o] for k, o in self._to_offset.items()]}

    def load_state(self, st: Dict[str, Any]) -> None:
        self.strategy = st.get("strategy", self.strategy)
        self.prefix = st.get("prefix", self.prefix)
        self.width = st.get("width", self.width)
        self._next = st.get("next", 0)
        for k, o in st.get("pairs", []):
            self._to_offset[tuple(k)] = o
            self._to_key[o] = tuple(k)


class NodeIdCodec:
    """Mints and reverses `"<table>:<offset>"` ids for a whole schema.

    Thread-safe: FastAPI serves requests from a thread pool and the registry
    fallback mutates shared dictionaries.
    """

    def __init__(self, state_path: Optional[str] = None):
        self._lock = threading.RLock()
        self._by_label: Dict[str, LabelCodec] = {}
        self._by_table: Dict[int, LabelCodec] = {}
        self.state_path = state_path

    # -- setup ----------------------------------------------------------------

    def register(self, names: List[str], key_types: Optional[Dict[str, str]] = None,
                 samples: Optional[Dict[str, List[Any]]] = None) -> None:
        """Assign each label (and relationship type) its table number.

        Order comes from the mapping file and must stay stable — it is what makes
        an id mean the same thing after a restart. Adding a new label at the end is
        safe; reordering the existing ones invalidates saved Kineviz projects.
        """
        key_types = key_types or {}
        samples = samples or {}
        with self._lock:
            for name in names:
                if name in self._by_label:
                    continue
                tid = len(self._by_label)
                codec = LabelCodec.infer(name, tid, key_types.get(name), samples.get(name))
                self._by_label[name] = codec
                self._by_table[tid] = codec
        if self.state_path:
            self.load()

    # -- the round trip -------------------------------------------------------

    def encode(self, label: str, key: KeyTuple) -> str:
        with self._lock:
            codec = self._by_label.get(label)
            if codec is None:
                self.register([label])
                codec = self._by_label[label]
            return "{}:{}".format(codec.table_id, codec.offset_for(tuple(key)))

    def decode(self, table_id: int, offset: int) -> Optional[Tuple[str, KeyTuple]]:
        """Reverse `internal_id(table_id, offset)` back to (label, key)."""
        with self._lock:
            codec = self._by_table.get(int(table_id))
            if codec is None:
                return None
            key = codec.key_for(int(offset))
            return (codec.label, key) if key is not None else None

    def strategy_of(self, label: str) -> Optional[str]:
        c = self._by_label.get(label)
        return c.strategy if c else None

    def is_stateless(self) -> bool:
        """True when every label uses arithmetic, so ids survive a restart on their
        own and the state file is unnecessary."""
        return all(c.strategy in (DIRECT, NUMERIC_SUFFIX) for c in self._by_label.values())

    # -- persistence (only needed when some label fell back to the registry) ---

    def save(self) -> None:
        if not self.state_path or self.is_stateless():
            return
        with self._lock:
            blob = {"labels": [c.label for c in self._by_table.values()],
                    "state": {c.label: c.state() for c in self._by_label.values()}}
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(blob, fh)
        os.replace(tmp, self.state_path)

    def load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as fh:
                blob = json.load(fh)
        except Exception:
            return
        with self._lock:
            for label, st in (blob.get("state") or {}).items():
                c = self._by_label.get(label)
                if c is not None:
                    c.load_state(st)
