#!/usr/bin/env python3
"""Load the .sql fixture files without needing sqlcmd.

`sqlcmd` is the usual way, but it is a separate install and it is not on a CI
runner by default. The only thing it does that a plain driver cannot is split a
file on `GO`, which is not SQL at all — it is sqlcmd's own batch separator. So we
split on it here and send each batch through pyodbc.

    python3 scripts/load_fixtures.py sql/001_demo_generic.sql sql/002_demo_generic_seed.sql

The connection string comes from IBASE_ADMIN_CONNECTION_STRING (an account that may
create databases), falling back to IBASE_CONNECTION_STRING.
"""

from __future__ import annotations

import os
import re
import sys

# `GO` on a line of its own, optionally followed by a repeat count. Not SQL.
GO = re.compile(r"^\s*GO(?:\s+\d+)?\s*(?:--.*)?$", re.IGNORECASE)
# sqlcmd-only directives; harmless to skip.
SQLCMD_DIRECTIVE = re.compile(r"^\s*:(setvar|r|connect|on error)\b", re.IGNORECASE)


def batches(text: str):
    current = []
    for line in text.splitlines():
        if SQLCMD_DIRECTIVE.match(line):
            continue
        if GO.match(line):
            body = "\n".join(current).strip()
            if body:
                yield body
            current = []
        else:
            current.append(line)
    body = "\n".join(current).strip()
    if body:
        yield body


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    dsn = os.environ.get("IBASE_ADMIN_CONNECTION_STRING") or os.environ.get("IBASE_CONNECTION_STRING")
    if not dsn:
        print("set IBASE_ADMIN_CONNECTION_STRING (or IBASE_CONNECTION_STRING) first",
              file=sys.stderr)
        return 2
    if "$(" in "".join(open(p).read() for p in argv):
        print("note: files using sqlcmd variables ($(...)) need sqlcmd; skipping those batches",
              file=sys.stderr)

    import pyodbc
    conn = pyodbc.connect(dsn, autocommit=True)
    failures = 0
    for path in argv:
        text = open(path, encoding="utf-8").read()
        ran = 0
        for i, batch in enumerate(batches(text), 1):
            if "$(" in batch:            # unresolved sqlcmd variable
                continue
            try:
                cur = conn.cursor()
                cur.execute(batch)
                while True:              # drain any result sets the batch produced
                    if cur.description:
                        cur.fetchall()
                    if not cur.nextset():
                        break
                cur.close()
                ran += 1
            except Exception as exc:
                failures += 1
                print("  batch {} of {} failed: {}".format(i, path, exc), file=sys.stderr)
                print("    {}".format(" ".join(batch.split())[:120]), file=sys.stderr)
        print("{}: {} batch(es) ran".format(path, ran))
    conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
