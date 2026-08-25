# Quick start

**Fifteen minutes, from nothing to a graph you can click around.** You need Docker, Python, and
a Kineviz account — everything else the setup script installs. No iBase licence and no real
data — `./setup` builds a demo database that has the shapes a real one has.

Everything below was run exactly as written, against
<https://graphxr.kineviz.com/> in a browser. The numbers are the ones it returned.

---

## 1. Stand up the demo

```bash
git clone https://github.com/Kineviz/ibase-kineviz-bridge
cd ibase-kineviz-bridge
./setup
```

That starts SQL Server in Docker, builds two demo databases, creates a read-only login, and
prints the command to run the bridge. It takes a few minutes the first time, mostly downloading
SQL Server.

Run the command it printed. You should see:

```
  iBase bridge is serving 3 record types and 3 link types.

  In Kineviz: Create -> Create New Project
      Database Type:  KoreDB Via Proxy API
      Proxy API URL:  http://localhost:7073/ibase/demo
```

**What is in the demo:** 12 people, 5 organisations, 10 accounts, and the links between them —
who works for whom, who owns which account, and money moving between accounts.

---

## 2. Get Kineviz

**If you already have a Kineviz account and it is open, skip to step 3.**

**Create an account** at <https://www.kineviz.com/> if you do not have one. It is the same
account for the browser and for Desktop.

**Then pick one.** Both work, and nothing here depends on which:

| | Kineviz in the browser | Kineviz Desktop |
| --- | --- | --- |
| Where | <https://graphxr.kineviz.com/> | [download](https://github.com/Kineviz/kineviz-desktop/releases/latest) |
| To install | nothing | ~200 MB |
| Reaching the bridge on this machine | your browser asks permission once — click **Allow** | works straight away |
| Cost | account required | free for individual use, forever |

The browser is the quicker start — nothing to download. Desktop is worth it if you would rather
not deal with the permission prompt, or if your browser refuses rather than asks (see below).

If you want Desktop, `uname -sm` tells you which build:

| Your machine | File |
| --- | --- |
| Apple Silicon Mac | `Kineviz-Desktop-<ver>-mac-arm64.dmg` |
| Intel Mac | `Kineviz-Desktop-<ver>-mac-x64.dmg` |
| Windows | `Kineviz-Desktop-Setup-<ver>-win-x64.exe` |
| Linux (Debian/Ubuntu) | `Kineviz-Desktop-<ver>-linux-amd64.deb` |
| Linux (other) | `Kineviz-Desktop-<ver>-linux-x86_64.AppImage` |

---

## 3. Connect Kineviz to the bridge

**Create → Create New Project**, then two fields:

| Field | Value |
| --- | --- |
| **Database Type** | `KoreDB Via Proxy API` |
| **Proxy API URL** | `http://localhost:7073/ibase/demo` |

No username or password. The bridge holds the database credentials; Kineviz never sees them.

Click **Confirm**.

> **Two things that trip people up.**
>
> Pick **`KoreDB Via Proxy API`**, not `Database Proxy`. The second one fails its connection
> check because GraphXR probes it with Bolt, which cannot speak to an HTTP address.
>
> **In a browser, your browser will ask permission** the first time, because the bridge is
> running on your machine while Kineviz is served from the web. Click **Allow**. If you see no
> prompt *and* nothing loads, your browser is refusing rather than asking — use Desktop instead.

You know it worked when the schema panel lists **3 record types** (Person, Organization,
Account) and **3 link types** (WORKS_FOR, OWNS, TRANSFERRED_TO).

---

## 4. Pull some nodes and relationships

Open the **Query** panel — the `</>` icon in the left strip — and replace what is there with:

```cypher
MATCH (p:Person)-[r:WORKS_FOR]->(o:Organization) RETURN p, r, o LIMIT 25
```

Click **Run**. The panel reports:

```
13 nodes · 9 edges · 0.10 s
```

Eight people, five organisations, nine employment links. Close the panel with the same `</>`
icon to see the graph.

**If the dots are piled on top of each other**, that is normal — a query drops everything at
the same point. Click the **Fly Out** button in the bottom toolbar (four outward arrows) to
frame the graph, and the **layout** icon in the left strip → **Force** → **Run** to spread it
out.

**What just happened underneath.** Kineviz speaks Cypher; SQL Server does not. The bridge
translated it and logged both halves side by side:

```bash
tail -1 logs/queries.jsonl | python3 -m json.tool
```

```sql
SELECT TOP (25)
    v0.[person_id] AS __gx_v0_k0, v0.[full_name] AS __gx_v0_p0, ...
    v1.[organization_id] AS __gx_v1_k0, v1.[name] AS __gx_v1_p0, ...
    e0.[employment_id] AS __gx_e0_k0, e0.[job_title] AS __gx_e0_p0, ...
FROM [dbo].[Person] AS v0
JOIN [dbo].[Employment] AS e0 ON e0.[person_id] = v0.[person_id]
JOIN [dbo].[Organization] AS v1 ON v1.[organization_id] = e0.[organization_id]
ORDER BY v0.[person_id], v1.[organization_id], e0.[employment_id];
```

Ordinary SQL. Nothing was copied or exported — that ran against SQL Server when you clicked Run.

### A few more to try

```cypher
-- everything, to see the whole demo at once
MATCH (n) RETURN n LIMIT 50

-- who owns which account
MATCH (p:Person)-[r:OWNS]->(a:Account) RETURN p, r, a LIMIT 25

-- money moving between accounts, in both directions
MATCH (a:Account)-[r:TRANSFERRED_TO]-(b:Account) RETURN a, r, b LIMIT 50

-- a table rather than a graph
MATCH (p:Person) RETURN p.full_name, p.country_code, p.risk_score
```

The last one comes back as a table, not dots — the bridge decides which shape to return from
what you asked for.

---

## 5. Select a few nodes and expand

**Expand** is the question "what else do these connect to?", and it is the thing you will do
most.

1. Click a **Person** dot. Shift-drag to add more, or hold Ctrl and drag a box around several.
2. Right-click one of them → **Expand**.

Starting from the 13-node graph above and expanding **three people**:

| | before | after |
| --- | --- | --- |
| nodes | 13 | **19** |
| edges | 9 | **17** |

Six **Account** nodes appeared, joined by eight new **OWNS** links. Those people own bank
accounts, and now you can see which.

Expand again from the new Accounts and the transfers between them appear. That is the loop:
pull a little, expand, follow what looks interesting.

> **Why this is the interesting one.** Kineviz writes *every selected node's id* into the expand
> query. Select a thousand dots and the query carries a thousand ids — and SQL Server refuses
> more than 2,100 parameters in a single statement. In real captured traffic, 22% of expands
> exceeded that, and one carried 42,214 ids. The bridge sends large selections as a single JSON
> parameter instead, so this keeps working when you select the whole canvas. Try it.

---

## 6. Try the iBase-shaped database

The second demo database looks the way a real iBase database looks: one table per record type,
string record ids like `PER0000123`, endpoints in a `_LinkEnd` system table, and a link type
that joins several different kinds of record at once.

It needs its own bridge, because one bridge serves one database:

```bash
. .venv/bin/activate
export IBASE_CONNECTION_STRING='DRIVER={ODBC Driver 18 for SQL Server};SERVER=127.0.0.1,11433;DATABASE=IBaseShapedDemo;UID=ibase_ro;PWD='$(cat .ro-password)';Encrypt=yes;TrustServerCertificate=yes'
python ibase_server.py --mapping config/mapping.ibase.yml --name ibase --port 7074 --studio
```

Add a second Kineviz project pointing at `http://localhost:7074/ibase/ibase`, then:

```cypher
MATCH (a)-[r:Associate]->(z) RETURN a, r, z LIMIT 50
```

One link type, three different kinds of endpoint — Person→Person, Person→Organization,
Organization→Vehicle. The bridge runs a separate indexed query for each and merges them.

---

## 7. Point it at your own database

All of this happens in the editor at **<http://localhost:7073/studio>**.

**1. Get a read-only login.** `SELECT` only, on the tables you are allowed to read.
`sql/020_readonly_login.sql` creates one. iBase schema changes go through iBase Designer, never
SQL.

**2. Point the bridge at your database.** In the editor, **Database → Connect a different
database**. Fill in server, port, database and login, then **Test**:

```
✓ 2022 · Developer Edition (64-bit)
  Fully supported.

  version          16.0.4265.3
  compatibility    160
  tables           412
  can it write?    ● no — writes are refused, which is what we want
```

Read that last line. It asks the server what your login may actually do, and warns you if the
account can change data. **Test and use** switches the running bridge over without a restart.
Your password is used to connect and then dropped — it is never written to a file. The panel
shows the `export IBASE_CONNECTION_STRING=…` line for making it permanent.

**3. Press Read the database.** It works out which tables are records and which are links, and
for iBase link tables it asks the data which record types they actually join.

**4. Name the links, and check their direction.** This is the step not to skip. Two things no
database can tell you: what a link is **called**, and which way it **points**. A backwards link
returns no rows rather than an error, so it is the mistake most likely to go unnoticed. The
editor runs every link **both ways** against your data and shows you real rows:

```
WORKS_FOR    from Employment                        [flip ⇄] [check direction]
Person ──▶ Organization

  ✎ Both directions join, so the database cannot decide this one for you —
    only you know which sentence is true. Read a row:
    "Avery Chen → Northwind Logistics".
```

**5. Save mapping**, then **Reload bridge**. Your Kineviz project is now querying your own
database.

Prefer the command line? `python -m ibase_bridge.discovery --mapping-out mapping.proposed.yml`
writes the same draft as a file for you to edit by hand.

---

## When something looks wrong

**<http://localhost:7073/studio>** first. It shows whether the bridge is running, whether the
database is answering, and the last query that failed — in plain words rather than a stack
trace.

Then `logs/queries.jsonl`, which pairs every Cypher query with the SQL it became.

A few things behave in ways worth knowing:

- **A query returns nothing, with no error.** Usually a link pointing the wrong way. Check it
  in the editor.
- **An error mentioning something "is not supported".** That is deliberate. The bridge refuses
  what it cannot translate — variable-length paths, `OPTIONAL MATCH`, `WITH`, `UNWIND`,
  `HAVING`, regular expressions — rather than quietly returning the wrong answer. An empty
  canvas is too easily mistaken for "nothing matched".
- **Everything is read-only.** `CREATE`, `MERGE`, `SET` and `DELETE` are refused twice over: by
  the bridge, and by the SQL login itself.
