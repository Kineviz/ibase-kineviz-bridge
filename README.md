# iBase Bridge

**Query an i2 iBase database as a graph, from Kineviz, live.**

iBase stores its data in Microsoft SQL Server as ordinary tables. This bridge sits between
the two: Kineviz sends it a graph query, the bridge turns that into ordinary SQL `SELECT`s
with `JOIN`s, runs them, and hands back dots and lines. Nothing is copied, nothing is
exported, and nothing in iBase is changed. The bridge only ever reads.

```
Kineviz  ──── graph query ────>  iBase Bridge  ──── SQL ────>  SQL Server
         <─── dots and lines ──                <─── rows ───
```

To Kineviz it looks like an ordinary graph database, so nothing inside Kineviz has to change.

> **Early version.** It has been built and tested against a demo database and a second one
> shaped the way iBase databases are shaped. It has **not** yet been run against a real iBase
> installation. Real deployments vary by version and by local customisation, so treat the
> mapping file as something to review, not something to trust. Use at your own risk.

---

## A few words, in plain terms

| Word | What it means here |
| --- | --- |
| **Cypher** | The graph query language Kineviz speaks. `MATCH (p:Person)-[:WORKS_FOR]->(o:Organization) RETURN p, o` means "find people and the organisations they work for." |
| **Node / edge** | A dot and a line. In iBase's own words, an *entity* record and a *link* record. |
| **Label / type** | What kind of dot (`Person`) or line (`WORKS_FOR`). |
| **Mapping file** | A short file saying which tables are dots, which are lines, and how a line finds its two ends. SQL Server has no idea what a graph is, so somebody has to say. |
| **Expand** | Selecting dots in Kineviz and asking "what do these connect to?" It turns out to be the query most likely to break. |

---

## Which SQL Server versions work

| Version | | Notes |
| --- | --- | --- |
| **2016, 2017, 2019, 2022, 2025** | ✅ full | Long selections travel as one JSON parameter (`OPENJSON`). |
| **Azure SQL Database / Managed Instance** | ✅ full | Same engine generation. |
| **SQL Server 2012, 2014** | ✅ works | No `OPENJSON`, so long selections travel as XML instead. One parameter either way. |
| **2008 R2 and earlier** | ❌ no | Paging needs `OFFSET … FETCH NEXT`, added in 2012. Kineviz pages every result, so there is no way around it. |

Also **SQL Server on Linux** (2017+) and **in containers**; the demo runs in one.

**One thing to watch on an older database.** `OPENJSON` needs the engine to be 2016+ *and* the
database to be at **compatibility level 130 or higher**. A database restored from an older
server keeps its old level, so a 2022 engine hosting a level-110 database still falls back to
XML. The bridge checks both and picks the right path. `/studio` says which one it is on.

## What you need

1. **Docker.** Engine 24.0+ or Docker Desktop. SQL Server runs as a container.
   On an Apple Silicon Mac there is no native SQL Server build, so it runs emulated; turn on
   *Docker Desktop → Settings → General → "Use Rosetta for x86_64/amd64 emulation"* or it will
   be slow.
2. **Python 3.9+**.
3. **The Microsoft ODBC driver.** `./setup` tells you how if it is missing.
   On macOS: `brew tap microsoft/mssql-release https://github.com/Microsoft/homebrew-mssql-release`
   then `brew trust microsoft/mssql-release && brew install msodbcsql18`.
4. **A Kineviz account.** [Sign up](https://www.kineviz.com/). Only needed at the last step.
5. **Kineviz.** In the browser at <https://graphxr.kineviz.com/>, or as Desktop. Either
   works; nothing to install for the browser. See below.

## Quick start

**New here? [docs/quickstart.md](docs/quickstart.md) walks the whole thing**: set up the
demo, connect Kineviz, pull a graph, and expand it, in about fifteen minutes, with no iBase
licence and no real data.

The short version:

```bash
./setup
```

That is the whole thing. It starts SQL Server, builds both demo databases, creates a
`SELECT`-only login, sets up Python, and prints the command to run the bridge. It generates its
own passwords into `.sa-password` and `.ro-password`, neither of which is ever committed. Safe
to re-run; every step checks before it acts.

```bash
./setup status     # what is running
./setup down       # stop the container, keep the data
./setup destroy    # stop it and delete the data
```

Then start the bridge with the command `./setup` printed, and open
**<http://localhost:7073/studio>**, which shows whether things are working and the exact steps
to connect Kineviz.

The demo builds **two** databases. `GraphXRConnectorDemo` is the plain schema from the
specification. `IBaseShapedDemo` is shaped the way a real iBase database is shaped: one table
per record type, string record ids like `PER0000123`, a `_LinkEnd` endpoint table, and a link
type that joins several different kinds of record.

## Kineviz: browser or Desktop

Both work. Pick whichever suits you.

| | Kineviz in the browser | Kineviz Desktop |
| --- | --- | --- |
| Where | <https://graphxr.kineviz.com/> | [download](https://github.com/Kineviz/kineviz-desktop/releases/latest) |
| To install | nothing | ~200 MB |
| Reaching a bridge on this machine | your browser asks permission the first time; click **Allow** | works straight away |
| Cost | account required | free for individual use, forever; sign-in required |

Both need a Kineviz account. [Sign up](https://www.kineviz.com/).

**If you use the browser**, note that the bridge runs on your own machine while Kineviz is
served from the web, and browsers guard that boundary. Chrome 138 and later show a permission
prompt the first time. Allow it, and the connection works from then on. The bridge answers
the check browsers make beforehand, but only for Kineviz and for this machine; if you host
Kineviz somewhere else, add it:

```bash
python ibase_server.py --mapping config/mapping.demo.yml --allow-origin https://kineviz.example.com
```

That allowlist is deliberate. The bridge has no password of its own, so anything a browser can
be persuaded to open could otherwise read whatever the SQL login can read.

**If the schema never loads in the browser** and you saw no prompt, your browser is refusing
the connection rather than asking about it. Desktop avoids the question, which is the only
reason to prefer it.

**Desktop builds**, by machine:

| Your machine | File |
| --- | --- |
| Apple Silicon Mac | `Kineviz-Desktop-<ver>-mac-arm64.dmg` |
| Intel Mac | `Kineviz-Desktop-<ver>-mac-x64.dmg` |
| Windows | `Kineviz-Desktop-Setup-<ver>-win-x64.exe` |
| Linux (Debian/Ubuntu) | `Kineviz-Desktop-<ver>-linux-amd64.deb` |
| Linux (other) | `Kineviz-Desktop-<ver>-linux-x86_64.AppImage` |

Not sure which? `uname -sm` tells you.

## Connecting to your own database

The schema editor has a **Database** section: press **Connect a different database**, fill in
server, port, database and login, then **Test** or **Test and use**.

Test reports what it found, in words:

```
✓ 2022 · Developer Edition (64-bit)
  Fully supported.

  version          16.0.4265.3
  compatibility    160
  tables           6
  can it write?    ● no, writes are refused
```

It asks the server what the login is permitted to do rather than attempting a write, and warns
you if the account can change data. Use a
`SELECT`-only login. `sql/020_readonly_login.sql` creates one.

**Test and use** points the running bridge at that database immediately, no restart. It is held
in memory only: **the bridge never writes a password to disk**. To make it permanent, the panel
shows the `export IBASE_CONNECTION_STRING=…` line to put in your environment.

Common failures are explained rather than passed through raw: a self-signed certificate
rejected by driver 18, a wrong password, an unreachable host, a missing ODBC driver.

## Connect Kineviz to the bridge

**Create → Create New Project**, then two fields:

- **Database Type:** `KoreDB Via Proxy API`
- **Proxy API URL:** `http://localhost:7073/ibase/demo`

No username or password. The bridge holds the database credentials and Kineviz never sees
them. Confirm, and the schema panel should list the record and link types.

> **Pick `KoreDB Via Proxy API`, not `Database Proxy`.** The latter fails its connection check
> because GraphXR runs a Bolt probe against an HTTP address. That is a bug on the GraphXR side
> and nothing here can fix it.

Then try:

```cypher
MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p, r, o LIMIT 25
```

## Using a coding agent

Point Claude Code, Codex or Cursor at this repo and it can stand the whole thing up.
[`AGENTS.md`](AGENTS.md) tells it how. It will start SQL Server, build the demo databases,
create the read-only login, run the bridge, and hand you the connection details. If you want
Desktop it will work out which build your machine needs.

Three things it will not do, by design: **create your Kineviz account**, **sign in for you**, or
**install Desktop without asking**.

---

## The mapping file

SQL Server has no idea what a graph is, so this file says. It is the bridge's real contract,
and everything else follows from it.

```yaml
nodes:
  - label: Person
    table: Person
    key: person_id
    properties: [full_name, date_of_birth, country_code, risk_score]

edges:
  - type: WORKS_FOR
    table: Employment
    key: employment_id            # the line's OWN key - see "parallel links" below
    resolution: fk
    endpoints:
      - src: {label: Person,       column: person_id}
        dst: {label: Organization, column: organization_id}
    properties: [job_title, start_date, end_date]
```

Three ways a line can find its two ends:

| `resolution` | When to use it |
| --- | --- |
| `fk` | The link table holds two foreign keys. The ordinary case. |
| `prefixed_fk` | The link table holds two record ids, and which table each belongs to is told by its prefix (`PER…`, `ORG…`). Common in iBase. |
| `link_end` | The ends live in the `_LinkEnd` system table. Also common in iBase. |

The bridge **reads this file, not your database**. At connect time it runs exactly one query
(`SELECT SERVERPROPERTY('ProductMajorVersion')`, to find out whether `OPENJSON` is available)
and no catalog queries at all. Nothing about your schema is discovered at runtime.

That is on purpose. A schema cannot tell you which way round a link goes: nothing in SQL Server
says whether a Person works for an Organisation or the reverse, and getting it backwards
returns *no rows* rather than an error. It also cannot name things usefully (`EMPLOYMENT` vs
`WORKS_FOR`), and it cannot see an iBase link table at all, because those hold record ids
rather than foreign keys. So discovery proposes, a person decides, and the bridge obeys the
file, which is reviewable and diffable, so you can see when someone changes what a link means.

The consequence: **if the iBase schema changes, the bridge will not notice.** Re-run
`discover`, compare it with your mapping, restart.

### The schema editor

The two things a database cannot tell you are what to **call** a link and which way it
**points**. So there is a small page for exactly those two jobs:

```bash
python ibase_server.py --mapping config/mapping.demo.yml --studio
# then open http://localhost:7073/studio
```

One bridge serves **one database** with **one mapping file**. To serve a second database, run a
second bridge on another port (`--port 7074`) and give Kineviz a second project.

The page opens with three things: whether the bridge is **working**, how to **connect Kineviz**
to it (with the URL and a copy button), and anything that has gone **wrong**.

```
Bridge status                                      12 queries answered
  state          ● running
  database       answering · SQL Server major version 16
  serving        3 record types, 3 link types
  mapping file   config/mapping.demo.yml
  last query     MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p,r,o
```

It refreshes every few seconds, so a failed query shows up without you going to find a log:

```
  ✗ Last query failed: regular-expression matching (=~) is not supported by the
    SQL Server backend. T-SQL has no regex operator. Use CONTAINS, STARTS WITH
    or ENDS WITH instead.
```

Press **Read the database**, and it lists what it found. **Table data** on any record or link
shows real rows with the original column names alongside the names Kineviz will use:

```
Real rows from dbo.Employment, with the names Kineviz will use. Every column is mapped.

employment_id   person_id        organization_id      job_title
bigint · key    bigint ·         bigint ·             nvarchar ·
                source end →     target end →         property
                Person           Organization
─────────────────────────────────────────────────────────────────
9001            1001             2001                 Director
9002            1005             2001                 Logistics Manager
```

Columns you have not mapped are shown greyed out and counted, because an unmapped column is
invisible in Kineviz and the usual way to discover that is to go looking for it later.

**Check direction** on any link shows real rows with names rather than numbers:

```
WORKS_FOR    from Employment                          [flip] [show rows]
Person ──▶ Organization

  ✎ Both directions join, so the database cannot decide this one for you.
    Only you know which sentence is true. Read a row: "Avery Chen → Northwind Logistics".

  Person             Organization
  Avery Chen      →  Northwind Logistics    job_title=Director
  Ana Sofía Ríos  →  Banco Ríos, S.A.       job_title=Compliance Lead
```

**Every link is run both ways round**, and the page says which of four situations you are in:

| | What it means |
| --- | --- |
| ✓ **Settled** | Only this direction returns rows. Nothing to decide. |
| ⚠ **Backwards** | This direction matches nothing; flipping returns rows. Flip it. |
| ✎ **You decide** | Both directions join. No query can settle it, so read the row and pick the sentence that is true. |
| ✗ **Neither** | The table has rows but no direction matches. The key columns or prefixes are wrong. |

That last distinction matters. For an ordinary two-foreign-key link, **both directions join
perfectly well**. The rows are identical; only the sentence changes. A tool that said "returns
data, looks right" there would be lying to you. For an iBase `_LinkEnd` link, the end markers
make direction real, and then it genuinely is settled.

Rename anything, tick records in or out, then **Save mapping** (the previous file is kept as
`.bak`) and **Reload bridge** to apply it without restarting. If the set of record types
changed, the page warns you that node ids moved and the graph needs reloading in Kineviz.

### Or from the command line

`discover` does the reading half without the page, and writes a draft:

```bash
python -m ibase_bridge.discovery --mapping-out mapping.proposed.yml
```

It uses one simple rule:

> A table that points at two others, and that nothing points back at, is almost certainly a
> **line**. Everything else is almost certainly a **record**.

The draft is a starting point, never an answer. It arrives with a REVIEW banner, the reasoning
left in as comments, and a warning on every line about direction, because **a backwards edge
returns no rows rather than an error**, which is much harder to notice than a crash.

---

## One link type, several kinds of record

In iBase, a link type like `Associate` can join Person→Person *and* Person→Organisation *and*
Organisation→Vehicle. List every pair you want, with the counts `discover` measured:

```yaml
  - type: Associate
    table: Associate
    key: Associate_ID
    resolution: prefixed_fk
    endpoints:
      - src: {label: Person,       column: Link1, prefix: PER}
        dst: {label: Person,       column: Link2, prefix: PER}
        row_estimate: 412908
      - src: {label: Person,       column: Link1, prefix: PER}
        dst: {label: Organization, column: Link2, prefix: ORG}
        row_estimate: 88140
```

One Cypher pattern becomes one clean, indexed query per pair. If your pattern already names a
label, the pairs that cannot match are dropped before any SQL is written.

**One honest limitation.** Kineviz's schema panel has room for exactly one pair per link type,
because it is a list keyed by name, so `Associate` cannot appear three times. We report the **most
common** pair and list the rest under an extra key. This affects only the *picture* of the
schema; queries stay fully polymorphic. If you build a query from the schema panel and get
nothing back, that is why. Write the pattern out and it will work.

---

## Node ids, and why they look the way they do

Every dot needs an id, and when you press Expand, Kineviz hands that id straight back to us.
The catch is that it does **not** treat the id as an opaque string. It splits it on `:` and
sends back two integers:

```
we return          id = "0:123"
you press Expand
Kineviz sends      ... WHERE id(n) IN [internal_id(0, 123)] ...
```

So the id has to be two numbers that we can reverse. Ours are **computed, not remembered**:

| The key looks like | What we do | Example |
| --- | --- | --- |
| `1001`, a number | use it as-is | Person 1001 → `0:1001` |
| `PER0000123`, iBase's prefix-and-digits | use the digits, rebuild the string coming back | → `0:123` |
| anything else | hand out numbers in order, saved to a file if you ask | — |

The first two rows need no memory at all, which means **restarting the bridge does not break a
saved Kineviz project**. That is the one thing to know: a scheme that hands out numbers in
first-seen order silently invalidates every id when it restarts.

Two guards, both tested: if a record type's ids disagree on format (`PER123` alongside
`PER0000123`) we cannot tell which string to rebuild, so it falls back; and keys above 2⁵³ fall
back too, because Kineviz is JavaScript and would lose precision.

### Parallel links stay separate

A line's id comes from the **link table's own key**, not from its two ends. That matters
because iBase lets the same two records be linked more than once: two `Associate` records
between the same people, with different dates. Ids built from the endpoints would collide, and
Kineviz de-duplicates by id, so five links would quietly become one line. This is why `key:` is
required on every edge and why the mapping file refuses to load without it.

---

## What works, and what does not

**Works:** node and link patterns, multi-hop, forward, backward and undirected, untyped and
alternation edges (`[r]`, `[r:A|B]`), full `WHERE` (`AND`/`OR`/`NOT`, comparisons, `CONTAINS`,
`STARTS WITH`, `ENDS WITH`, `IN`, `IS NULL`), `count`/`sum`/`avg`/`min`/`max` with grouping,
`DISTINCT`, `ORDER BY`, `SKIP`/`LIMIT`, the schema and count panels, and Expand.

**Not supported**, and each one is **refused with a message** rather than quietly mistranslated:
variable-length paths (`[:KNOWS*1..3]`), `OPTIONAL MATCH`, `WITH`, `UNWIND`, `HAVING`,
subqueries, regular-expression matching (`=~`; T-SQL has no regex operator), composite keys,
and all writing.

A query that would give a wrong answer fails loudly instead. An empty canvas is easy to
mistake for "nothing matched".

---

## Safety

The bridge only reads. Two independent guards, because one is not enough:

1. **The database.** Connect with a login granted `SELECT` and nothing else.
   `sql/020_readonly_login.sql` creates one and then proves it cannot write.
2. **The bridge.** It refuses any statement that does not begin with `SELECT` or `WITH`,
   before it reaches the driver. That turns a future bug in the query generator into a loud
   crash rather than a write, and it lets you see *"this bridge only reads"* instead of a SQL
   Server permission error.

Never change an iBase schema with SQL. i2 directs administrators to use iBase Designer.
Connection details come from an environment variable, never from the mapping file. Logs record
table names, timings and counts, never record contents.

Reads use `READ UNCOMMITTED` so that a long query never blocks, and is never blocked by,
someone editing records in iBase. The cost is that a half-written record can be read, which for
drawing a picture is acceptable; freezing an investigator's session is not. Change it with
`isolation_level` in the mapping if you prefer.

---

## Testing

```bash
python3 tests/test_pipeline.py      # 54 tests, no database, no dependencies but PyYAML
python3 -m pytest                   # same tests
python3 scripts/probe_queries.py http://localhost:7073/ibase/demo
```

Both run on every push (`.github/workflows/tests.yml`): the fast tests across Python 3.9, 3.11
and 3.13, and a full integration job that stands up a real SQL Server, builds both fixtures,
checks discovery proposes a mapping that loads, and runs the probe sweep.

If you would rather not install `sqlcmd`, `scripts/load_fixtures.py` builds the fixtures through
the ordinary driver instead.

The probe script fires ~33 queries at a running bridge and prints what each returned. It also
checks that the unsupported ones were **refused**, not silently emptied.

---

## Things that will bite you

**Two GraphXR bugs, neither fixable from here.** Use `KoreDB Via Proxy API`, not
`Database Proxy`. The latter fails its connection check because GraphXR runs a Bolt probe
against an HTTP address. And `gxr.query(..., {saveToGraph: false})` fails on any proxy
connection for the same reason.

**Selecting thousands of dots and pressing Expand.** Kineviz writes every selected id into the
query. In real captured traffic that was over 2,100 ids in 22% of Expands, and once 42,214.
SQL Server refuses more than 2,100 parameters in a single statement. The bridge sends large
lists as one JSON parameter instead, so selecting the whole canvas works.

**A backwards edge returns nothing, not an error.** The single easiest mistake to make in a
mapping file, and the hardest to spot.

---

MIT. Built alongside the [PostgreSQL 19 bridge](https://github.com/Kineviz/postgresql-kineviz-bridge),
which does the same job for PostgreSQL.
