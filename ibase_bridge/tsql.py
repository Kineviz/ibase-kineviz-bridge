"""T-SQL spelling rules, kept in one place.

The Cypher parser and the WHERE expression tree are shared with the PostgreSQL
bridge unchanged. Only the *spelling* of the generated SQL differs, and every
difference lives here so `predicate.py` can stay engine-neutral.

Several of these are not cosmetic. Getting them wrong produces a wrong answer with
no error at all — see `like_pattern` (wildcards in user text), `AVG` (integer
division) and `param_type_for` (a text parameter that silently disables an index).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

# Identifiers come from the mapping file, never from a query, but validate anyway.
SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# SQL Server refuses more than 2100 parameters in one statement. This is a hard
# engine limit, not a setting. Kineviz routinely sends expand queries carrying far
# more node ids than that, so anything above `INLINE_ID_MAX` goes through OPENJSON
# as a single parameter instead.
MAX_PARAMS = 2100
INLINE_ID_MAX = 200
CHUNK_SIZE = 1000


class UnsupportedByTSql(Exception):
    """Raised for a construct T-SQL genuinely cannot express.

    The caller turns this into a one-row error table so the user sees a clear
    message in Kineviz rather than an empty result they might mistake for data.
    """


def quote(name: str) -> str:
    """Wrap an identifier in brackets: Person -> [Person]."""
    return "[" + str(name).replace("]", "]]") + "]"


def qualify(schema: str, table: str) -> str:
    return "{}.{}".format(quote(schema), quote(table))


def validate_ident(name: str, what: str = "identifier") -> str:
    if not SAFE_IDENT.match(str(name or "")):
        raise ValueError("unsafe {}: {!r}".format(what, name))
    return name


# ----- paging ---------------------------------------------------------------
# T-SQL has two ways to limit rows and they are not interchangeable:
#   TOP (n)                      - no sort required
#   OFFSET m ROWS FETCH NEXT n   - ONLY legal after an ORDER BY
# Kineviz pages with a bare `SKIP 1000 LIMIT 1` and never sends a sort, so when
# we skip we have to supply the order ourselves.

def top_clause(limit: Optional[int], skip: int, distinct: bool = False) -> str:
    """The `SELECT [DISTINCT] [TOP (n)]` prefix. Empty TOP when paging with OFFSET."""
    parts = ["SELECT"]
    if distinct:
        parts.append("DISTINCT")
    if limit is not None and not skip:
        parts.append("TOP ({:d})".format(int(limit)))
    return " ".join(parts)


def offset_fetch(limit: Optional[int], skip: int) -> str:
    if not skip:
        return ""
    out = "OFFSET {:d} ROWS".format(int(skip))
    if limit is not None:
        out += " FETCH NEXT {:d} ROWS ONLY".format(int(limit))
    return out


def order_by(terms: Sequence[str], tiebreakers: Sequence[str] = ()) -> str:
    """Build ORDER BY, appending key columns so the total order is deterministic.

    Paging without a stable order is not merely untidy: the database may return a
    row on page two that it already returned on page one, so Kineviz's "is there
    more?" probe can answer incorrectly. Never emit `ORDER BY (SELECT NULL)` —
    it satisfies the parser and destroys paging.
    """
    seen, out = set(), []
    for t in list(terms) + list(tiebreakers):
        base = t.rsplit(" ", 1)[0] if t.upper().endswith((" ASC", " DESC")) else t
        if base in seen:
            continue
        seen.add(base)
        out.append(t)
    return "ORDER BY " + ", ".join(out) if out else ""


# ----- LIKE -----------------------------------------------------------------

_LIKE_META = re.compile(r"([%_\[\]])")


def like_pattern(value: Any, mode: str) -> str:
    """Build a LIKE pattern, escaping wildcards the user did not intend.

    CONTAINS '50%' must not become LIKE '%50%%', which matches anything holding
    "50". The PostgreSQL bridge has this bug; do not carry it over.
    """
    escaped = _LIKE_META.sub(r"\\\1", str(value))
    return {"contains": "%{}%", "startswith": "{}%", "endswith": "%{}"}[mode].format(escaped)


LIKE_ESCAPE = " ESCAPE '\\'"


# ----- aggregates -----------------------------------------------------------
# AVG over an int column does INTEGER division in T-SQL: avg(1,2,2) returns 1,
# not 1.67, and nothing warns you. SUM over int overflows rather than widening.

INTEGER_TYPES = {"tinyint", "smallint", "int", "bigint"}
LOB_TYPES = {"text", "ntext", "image", "xml"}


def agg_expr(fn: str, col: str, sql_type: Optional[str] = None) -> str:
    t = (sql_type or "").lower()
    if fn == "avg":
        return "AVG(CAST({} AS decimal(38,10)))".format(col)
    if fn == "sum" and t in INTEGER_TYPES:
        return "SUM(CAST({} AS decimal(38,0)))".format(col)
    return "{}({})".format(fn.upper(), col)


def group_key_expr(col: str, sql_type: Optional[str] = None, max_len: Optional[int] = None) -> str:
    """GROUP BY is illegal on nvarchar(max)/text/image — cast down to an index-sized key."""
    t = (sql_type or "").lower()
    if t in LOB_TYPES or (max_len is not None and max_len < 0):
        return "CAST({} AS nvarchar(450))".format(col)
    return col


def cast_text(col: str, sql_type: Optional[str] = None) -> str:
    """Only cast when the column is not already character data — wrapping a column
    in CAST stops SQL Server using an index on it."""
    t = (sql_type or "").lower()
    if t in ("char", "varchar", "nchar", "nvarchar", "text", "ntext") or not t:
        return col
    return "CAST({} AS nvarchar(4000))".format(col)


# ----- big id lists ---------------------------------------------------------

OPENJSON_MIN_VERSION = 13  # SQL Server 2016
OPENJSON_MIN_COMPAT = 130  # ...and the DATABASE must be at this compatibility level

# The oldest engine this bridge works on. SQL Server 2012 introduced
# `OFFSET ... FETCH NEXT`, and Kineviz pages every result set, so 2008 R2 and
# earlier cannot be supported without rewriting paging around a windowed subquery.
MIN_SUPPORTED_MAJOR = 11  # SQL Server 2012

RELEASE_NAMES = {
    9: "2005", 10: "2008 / 2008 R2", 11: "2012", 12: "2014", 13: "2016",
    14: "2017", 15: "2019", 16: "2022", 17: "2025",
}


def describe_server(major, compat=None, edition=""):
    """Plain words about what this server can and cannot do here."""
    name = RELEASE_NAMES.get(major, "major version {}".format(major))
    if "azure" in (edition or "").lower():
        name = "Azure SQL"
    if major < MIN_SUPPORTED_MAJOR:
        return {"supported": False, "name": name, "level": "error", "note":
                "SQL Server {} is too old for this bridge. Paging uses OFFSET/FETCH, "
                "which arrived in SQL Server 2012.".format(name)}
    if major >= OPENJSON_MIN_VERSION and (compat or 0) >= OPENJSON_MIN_COMPAT:
        return {"supported": True, "name": name, "level": "ok", "note":
                "Fully supported."}
    if major >= OPENJSON_MIN_VERSION:
        return {"supported": True, "name": name, "level": "warn", "note":
                "Supported. This database is at compatibility level {}, below 130, so "
                "OPENJSON is unavailable and large selections are sent as XML instead - "
                "which works, just less directly.".format(compat)}
    return {"supported": True, "name": name, "level": "warn", "note":
            "Supported. OPENJSON arrived in SQL Server 2016, so large selections are "
            "sent as XML instead - which works, just less directly."}


def id_list_sql(column: str, values: Sequence[Any], params: List[Any],
                param_types: List[Any], sql_type: Optional[str] = None,
                openjson: bool = True) -> str:
    """`column IN (...)` for any number of values, on any supported server.

    Small lists become a plain IN: best plans, and readable in a log. Large ones
    must not, because 2100 parameters is a hard ceiling and Kineviz sends expand
    queries carrying tens of thousands of ids.

    Two ways to send a long list as a *single* parameter:

    * **OPENJSON**, on SQL Server 2016 and later. Its WITH clause declares the
      column type, so there is no hidden text-to-number conversion to stop the join
      using an index, and JSON has no delimiter a record id could contain.
    * **XML shredding**, on anything older. Same one-parameter property, same
      explicit cast, and it has worked since SQL Server 2005 — so a 2012 or 2014
      server handles a selection of any size rather than failing at id 2,101.
    """
    if not values:
        return "1 = 0"
    if len(values) <= INLINE_ID_MAX:
        holes = ", ".join("?" for _ in values)
        params.extend(values)
        param_types.extend(sql_type for _ in values)
        return "{} IN ({})".format(column, holes)

    if openjson:
        params.append(json.dumps([_jsonable(v) for v in values], separators=(",", ":")))
        param_types.append("nvarchar_max")
        return "{} IN (SELECT [value] FROM OPENJSON(?) WITH ([value] {} '$'))".format(
            column, openjson_type(sql_type))

    params.append("".join("<i>{}</i>".format(_xml_escape(v)) for v in values))
    param_types.append("nvarchar_max")
    return ("{} IN (SELECT T.c.value('.', '{}') FROM (SELECT CAST(? AS xml)) AS X(x) "
            "CROSS APPLY x.nodes('/i') AS T(c))".format(column, openjson_type(sql_type)))


def _xml_escape(v: Any) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def openjson_type(sql_type: Optional[str]) -> str:
    t = (sql_type or "").lower()
    if t in INTEGER_TYPES:
        return "bigint"
    return "nvarchar(450)"


def _jsonable(v: Any) -> Any:
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def chunks(values: Sequence[Any], size: int = CHUNK_SIZE):
    for i in range(0, len(values), size):
        yield values[i:i + size]


# ----- parameter typing -----------------------------------------------------
# pyodbc sends every Python str as nvarchar. Comparing an nvarchar parameter to a
# varchar column makes SQL Server convert THE COLUMN, turning an index lookup into
# a full scan — on an iBase expand, the difference between 20ms and 20s, with
# nothing in the output to say so. Bind each parameter as the column's real type.

def param_type_for(sql_type: Optional[str], size: Optional[int] = None):
    """A `setinputsizes` entry, or None to let pyodbc decide."""
    try:
        import pyodbc
    except ImportError:
        return None
    t = (sql_type or "").lower()
    if t == "nvarchar_max":
        return (pyodbc.SQL_WVARCHAR, 0, 0)
    if t in ("varchar", "char"):
        return (pyodbc.SQL_VARCHAR, size or 255, 0)
    if t in ("nvarchar", "nchar"):
        return (pyodbc.SQL_WVARCHAR, size or 255, 0)
    return None


def input_sizes(param_types: Sequence[Any]) -> List[Any]:
    return [param_type_for(t if isinstance(t, str) else None) for t in param_types]


class Dialect:
    """The knobs `predicate.to_sql` turns. One instance per backend."""

    name = "sqlserver"
    placeholder = "?"

    def __init__(self, server_major: int = 13, column_types: Optional[Dict[str, str]] = None,
                 compat_level: int = 130):
        self.server_major = server_major
        self.column_types = column_types or {}
        # OPENJSON needs a 2016+ engine AND a database left at compatibility level
        # 130 or higher. A database restored from an older server keeps its old
        # level, so a modern engine is not on its own enough.
        self.compat_level = compat_level
        self.supports_openjson = (server_major >= OPENJSON_MIN_VERSION
                                  and compat_level >= OPENJSON_MIN_COMPAT)

    def quote(self, name: str) -> str:
        return quote(name)

    def bool_true(self, col: str) -> str:
        # There is no boolean type; a `bit` compares against 1. Written this way
        # a NULL bit is correctly excluded, matching Cypher.
        return "{} = 1".format(col)

    def empty_in(self, negated: bool) -> str:
        return "1 = 1" if negated else "1 = 0"

    def like(self, col: str, value: Any, mode: str, sql_type: Optional[str] = None):
        return "{} LIKE ?{}".format(cast_text(col, sql_type), LIKE_ESCAPE), like_pattern(value, mode)

    def regex(self, col: str, value: Any) -> str:
        raise UnsupportedByTSql(
            "regular-expression matching (=~) is not supported by the SQL Server "
            "backend - T-SQL has no regex operator. Use CONTAINS, STARTS WITH or "
            "ENDS WITH instead."
        )
