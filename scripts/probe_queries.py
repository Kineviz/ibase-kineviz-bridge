#!/usr/bin/env python3
"""Fire a spread of queries at a running bridge and print what came back.

Nothing is asserted. It is a sweep for a person to read: the point is to see, at a
glance, that the ordinary things still work and that the unsupported things still
fail loudly rather than returning something plausible and wrong.

    python3 scripts/probe_queries.py [http://localhost:7073/ibase/demo]
"""

import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:7073/ibase/demo"

PROBES = [
    # (category, cypher)
    ("connect",   'return "api" as a'),
    ("connect",   "CALL schema()"),
    ("connect",   "SHOW TABLES"),
    ("counts",    "MATCH (n) RETURN count(n)"),
    ("counts",    "MATCH (c:Person) RETURN count(c)"),
    ("counts",    "MATCH ()-[r:TRANSFERRED_TO]->() RETURN count(r)"),
    ("basic",     "MATCH (n:Person) RETURN n LIMIT 25"),
    ("basic",     "MATCH (n) RETURN n LIMIT 25"),
    ("one-hop",   "MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p,r,o LIMIT 50"),
    ("reverse",   "MATCH (o:Organization)<-[r:WORKS_FOR]-(p:Person) RETURN p,r,o LIMIT 50"),
    ("undirected","MATCH (a:Account)-[r:TRANSFERRED_TO]-(b:Account) RETURN a,r,b LIMIT 50"),
    ("two-hop",   "MATCH (p:Person)-[:OWNS]->(a:Account)-[:TRANSFERRED_TO]->(b:Account) RETURN p,a,b LIMIT 50"),
    ("where",     "MATCH (p:Person) WHERE p.country_code = 'ES' RETURN p"),
    ("where",     "MATCH (p:Person) WHERE p.risk_score > 50 AND p.country_code <> 'GB' RETURN p"),
    ("where",     "MATCH (p:Person) WHERE p.date_of_birth IS NULL RETURN p"),
    ("where",     "MATCH (p:Person) WHERE p.full_name CONTAINS 'Chen' RETURN p"),
    ("where",     "MATCH (p:Person) WHERE p.full_name STARTS WITH 'A' RETURN p"),
    ("where",     "MATCH (p:Person) WHERE p.full_name CONTAINS '50%' RETURN p"),
    ("scalar",    "MATCH (p:Person) RETURN p.full_name, p.country_code LIMIT 20"),
    ("scalar",    "MATCH (p:Person) RETURN DISTINCT p.country_code"),
    ("agg",       "MATCH (p:Person)-[:WORKS_FOR]->(o:Organization) RETURN o.name, count(p)"),
    ("agg",       "MATCH (a:Account)-[t:TRANSFERRED_TO]->(b:Account) RETURN count(t)"),
    ("order",     "MATCH (p:Person) RETURN p.full_name, p.risk_score ORDER BY p.risk_score DESC LIMIT 5"),
    ("paging",    "MATCH (n:Person) RETURN n SKIP 0 LIMIT 5"),
    ("paging",    "MATCH (n:Person) RETURN n SKIP 5 LIMIT 5"),
    ("paging",    "MATCH (n:Person) RETURN n SKIP 999 LIMIT 1"),   # must come back empty
    # These must all fail LOUDLY. A silent empty result would be worse than an error.
    ("must-fail", "MATCH (a)-[r*1..3]->(b) RETURN a"),
    ("must-fail", "OPTIONAL MATCH (a:Person) RETURN a"),
    ("must-fail", "MATCH (a:Person) WITH a RETURN a"),
    ("must-fail", "UNWIND [1,2] AS x RETURN x"),
    ("must-fail", "MATCH (p:Person) WHERE p.full_name =~ '^Av' RETURN p"),
    ("must-fail", "CREATE (n:Person {full_name:'nope'}) RETURN n"),
    ("must-fail", "MATCH (p:Person) RETURN p HAVING count(p) > 1"),
]


def post(url, query):
    body = json.dumps({"query": query}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http_error__": "%s %s" % (e.code, e.reason)}
    except Exception as e:
        return {"__error__": str(e)}


def classify(resp):
    if "__http_error__" in resp:
        return "HTTPERR", resp["__http_error__"]
    if "__error__" in resp:
        return "NOREPLY", resp["__error__"]
    if resp.get("status") == 1:
        return "ERROR", (resp.get("message") or "")[:70]
    data = resp.get("data")
    if isinstance(data, dict) and "categories" not in str(list(data.keys())[:1]):
        inner = data.get("data")
        kind = data.get("type")
        if kind == "GRAPH":
            return "GRAPH", "%d nodes, %d edges" % (len(inner.get("nodes", [])),
                                                    len(inner.get("relationships", [])))
        if kind == "TABLE":
            first = inner[1] if isinstance(inner, list) and len(inner) > 1 else None
            # A one-row table whose only column is "error" is how the bridge reports
            # a query it will not translate. That is a refusal, not a result.
            if isinstance(inner, list) and inner and inner[0] == ["error"]:
                return "REFUSED", str(first[0])[:70] if first else ""
            return "TABLE", "%d row(s) %s" % (max(0, len(inner) - 1), str(first)[:40] if first else "")
    return "SCHEMA", "%d categories" % len(list((data or {}).values())[0].get("categories", {})) \
        if data else "?"


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    print("probing %s\n" % url)
    counts = {}
    bad = []
    for cat, q in PROBES:
        kind, detail = classify(post(url, q))
        counts[kind] = counts.get(kind, 0) + 1
        flat = " ".join(q.split())
        print("%-10s %-8s %-58s %s" % (cat, kind, flat[:58], detail))
        if cat == "must-fail" and kind not in ("REFUSED", "ERROR"):
            bad.append((q, kind, "should have been refused"))
        if cat != "must-fail" and kind in ("ERROR", "REFUSED", "HTTPERR", "NOREPLY"):
            bad.append((q, kind, detail))
    print("\n" + ", ".join("%s=%d" % kv for kv in sorted(counts.items())))
    if bad:
        print("\n%d probe(s) did not behave as expected:" % len(bad))
        for q, k, d in bad:
            print("   [%s] %s\n       %s" % (k, " ".join(q.split())[:70], d))
        return 1
    print("\nall probes behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
