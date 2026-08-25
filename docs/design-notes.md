# Why it works the way it does

Notes on the decisions that are not obvious from the code, and on the ones that were
wrong the first time. Written for whoever changes this next.

---

## The bridge reads a file; it does not discover anything at runtime

At connect time the bridge runs exactly one query — `SELECT
SERVERPROPERTY('ProductMajorVersion')`, to find out whether `OPENJSON` exists — and no
catalog queries at all. The schema comes from the mapping file.

That is deliberate, for four reasons, the last being the real one:

1. **A schema cannot tell you direction.** Nothing in SQL Server says whether a Person works
   for an Organisation or the reverse. Guessing wrong does not error — it returns *no rows*.
2. **It cannot name things.** Discovery gives you `EMPLOYMENT`; an analyst wants `WORKS_FOR`.
3. **It cannot see an iBase link at all.** `Associate` has no foreign keys, just two
   `nvarchar` columns holding record ids. Only the data reveals what it joins, and that is a
   `GROUP BY` over the whole table — not something to run on every startup.
4. **A mapping that was guessed and never reviewed, pointed at live investigative data, is
   the wrong default.**

Discovery proposes, a person disposes, the bridge obeys the file. The file is
version-controlled and diffable, so you can see when someone changes what a link means.

The consequence: **if the iBase schema changes, the bridge will not notice.** Re-run
`discover`, compare, restart.

---

## Node ids are computed, not remembered

The first attempt reused the PostgreSQL bridge's `element_id` — `base64url(json([label,
key]))`, stateless and tidy. It cannot work here, and testing is what showed it.

Kineviz's KoreDB connector does not treat an id as an opaque string. It splits on `:` and
sends the halves back as two integers:

```
we return          id = "0:123"
you press Expand
Kineviz sends      ... WHERE id(n) IN [internal_id(0, 123)] ...
```

Confirmed twice: in 91 captured Expand queries every single one uses `internal_id(t, o)` with
small integers, and the PostgreSQL bridge's own mock backend mints `"0:0"`, `"0:1"`.

So an id is `"<table>:<offset>"`, and **both halves are arithmetic**:

| Key | Offset | Example |
| --- | --- | --- |
| a number | itself | Person 1001 → `0:1001` |
| `PER0000123` | the digits, string rebuilt on the way back | → `0:123` |
| anything else | handed out in order, saved to a file if asked | — |

The first two need no memory, so a fresh process decodes yesterday's ids. That matters: a
scheme that hands out numbers in first-seen order silently invalidates every id in a saved
Kineviz project on restart — the documented weakness of the PostgreSQL bridge.

Two guards, both tested. Keys that disagree on format (`PER123` beside `PER0000123`) would
both encode to 123, so the codec notices at load time and falls back. Keys above 2⁵³ fall back
too, because Kineviz is JavaScript and would lose precision.

`element_id` is still in the tree: the **Database Proxy** connector *does* round-trip opaque
strings and is a better home for polymorphic schemas. It is not what we ship on only because
that connector currently fails its connection check inside GraphXR.

---

## Edge ids come from the link table's own key

The inherited code minted a relationship id from the type plus both endpoint keys. That
assumes two records can be linked at most once by a given type. **iBase lets them be linked
many times** — two `Associate` records between the same people with different dates.

Kineviz de-duplicates by id, so five links would quietly become one line. No error.

Hence `key:` is required on every edge and the mapping refuses to load without it. It also
makes the id direction-stable, so an undirected match cannot draw the same edge twice.

---

## SQL Server's 2,100-parameter wall

Kineviz writes every selected node's id into an Expand query. Counted across 91 real captured
Expands:

| ids in one query | how often |
| --- | --- |
| 1–10 | 20 |
| 11–2,000 | 51 |
| 2,001–10,000 | 19 |
| over 10,000 | 1 (**42,214 ids, 907 KB**) |

**22% exceed 2,100**, which is a hard engine limit. So above 200 ids the whole list crosses as
one JSON parameter:

```sql
FROM OPENJSON(?) WITH ([k] bigint '$') AS __ids
JOIN [dbo].[Person] AS v0 ON v0.[person_id] = __ids.[k]
```

`OPENJSON` rather than `STRING_SPLIT` for two reasons that matter: its `WITH` clause declares
the output type, so no hidden text-to-number conversion stops the join using an index; and JSON
has no delimiter a record id could contain.

Rejected: a **table-valued parameter** is faster but needs `CREATE TYPE` — DDL against the
customer's database, which the spec forbids and no iBase administrator will grant.

Note two independent limits. `MAX_QUERY_LENGTH` stays at 2,000,000 characters because the ids
arrive in the **Cypher text**; the 2,100 cap is about **SQL parameters**.

---

## Expand runs separate statements, not one UNION ALL

The first plan was to fuse the branches. Wrong, for three reasons:

- **The columns do not line up.** Different labels have different properties, so fusing means
  padding every branch with `NULL AS __gx_v0_p7` and tagging rows.
- **The plan cache.** Small parameterised statements get reused; one wide union recompiles
  whenever the branch set changes.
- **The 2,100 cap is per statement.** Fusing pushes every branch into one budget.

**Prune before branching** — this is free and the biggest win. Our ids are self-describing, so
the selection decodes to its labels and link types that cannot touch them are dropped before
any SQL is written. Typically 240 possible branches down to about 15.

With nothing to prune against (`MATCH (n)-[r]-(m)`, no labels, no selection) the branch count
is capped and the query **refused with a message**, rather than melting the server.

---

## Five ways T-SQL gives a wrong answer with no error

1. **Multi-hop paths reuse the same edge.** Cypher forbids one relationship appearing twice in
   a path; a plain JOIN does not. Without a guard,
   `(a)-[:TRANSFERRED_TO]-(b)-[:TRANSFERRED_TO]-(c)` returns every `a → b → a` bounce-back. So
   two edges on the same link table get `AND e1.[pk] <> e0.[pk]`.
2. **`AVG` over an integer column does integer division.** `AVG(1,2,2)` returns `1`. Cast to
   `decimal` first. Same for `SUM` overflowing on `int`.
3. **A text parameter silently disables an index.** pyodbc sends Python strings as `nvarchar`;
   compared against a `varchar` column, SQL Server converts **the column**, turning a lookup
   into a scan. On an iBase expand that is 20 ms versus 20 s. Discovery records real column
   types and the connection layer binds with `setinputsizes`.
4. **`CONTAINS` treats `%` and `_` as wildcards.** Searching for `"50%"` builds `LIKE '%50%%'`.
   Escaped, with `ESCAPE '\'`. (This is a live bug in the PostgreSQL bridge.)
5. **The same row getting two ids.** JSON-encoding a key means `1` and `"1"` differ. Keys are
   normalised to strings on both encode and decode.

---

## Paging needs a sort Kineviz never sends

T-SQL only allows `OFFSET` after an `ORDER BY`, and Kineviz pages with a bare `SKIP 1000 LIMIT
1`. Worse, an unsorted page can return a row it already returned, so the "is there more?" probe
answers incorrectly. The emitter supplies the sort itself, using the key columns it already
projects. A user's own `ORDER BY` wins, with the keys appended as tiebreakers.

Never `ORDER BY (SELECT NULL)` — it satisfies the parser and destroys paging.

---

## One link type, several kinds of record

`RelSchema` carries a list of `(source, target)` pairs rather than one of each. Each pair
becomes its own concrete plan, so inside a plan both ends are single labels again and the
column layout, the result rebuilder and the id scheme all work unchanged.

**What we lose, honestly:** the KoreDB schema shape is a dictionary keyed by type name, so
`Associate` cannot appear three times. We report the most common pair and list the rest under a
non-standard `endpointPairs` key. This affects only the schema *picture* — queries stay fully
polymorphic. If a user builds a pattern from the schema panel and gets nothing, that is why.

---

## The editor runs every link both ways

The first version ran only the current direction and said *"returns data, looks right."* That is
misleading, and the misleading case is the common one: for an ordinary two-foreign-key link
**both directions join perfectly well**. Same rows; only the sentence changes.

So it runs both and reports which of four situations you are in — settled, backwards, you
decide, or neither — and shows a row with names rather than ids so the sentence can be read:
*"Avery Chen → Northwind Logistics"*. For an iBase `_LinkEnd` link the end markers make
direction real, and there it genuinely is settled.

A tool that lies about this is worse than no tool.

---

## Reload, not restart

The editor is served *by* the bridge, so a restart button would kill the page pressing it.
`BridgeState` holds the mapping, id codec, backend and query processor so all four can be
swapped together while the database connection stays. Real process supervision belongs to
launchd, systemd or Docker.

Reload warns when the set of record types changed, because the first half of every node id is a
record type's **position** — so adding or removing one shifts every id and the graph needs
reloading in Kineviz. Renaming alone is safe.

---

## Read-only, twice

A `SELECT`-only login, **and** the bridge refusing any statement not beginning with `SELECT` or
`WITH` before it reaches the driver.

The second is not redundant. It turns a future bug in the query generator into a loud crash
rather than a write, and it lets the user see *"this bridge only reads"* instead of a SQL Server
permission error. It also fails safe when someone misconfigures the connection to use `sa`.
