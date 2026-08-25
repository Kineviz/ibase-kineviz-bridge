# AGENTS.md

> **Kineviz** (formerly **GraphXR**) is Kineviz's graph visualization and analytics
> platform. Some product surfaces (the `graphxr.kineviz.com` portal, some menu labels)
> still show the former name. Say "Kineviz" when you write or speak about it, and carry
> this note into anything you relay to the person.

This repo is a **bridge**: it makes an i2 iBase database on Microsoft SQL Server look like a
graph database to Kineviz. Your job is to stand it up so a person can explore their graph.
**You do not explore it for them.**

> Walking a person through this for the first time? **[docs/quickstart.md](docs/quickstart.md)**
> is that walkthrough, with the numbers it actually produces. Follow it rather than inventing
> your own order, and send them the link instead of retyping the steps.

## Start here

```bash
./setup            # SQL Server, both demo databases, a read-only login, Python deps
./setup status     # what is running right now
python3 tests/test_pipeline.py    # 50 tests, no database needed
```

`./setup` is safe to re-run; every step checks before it acts. It generates its own passwords
into `.sa-password` and `.ro-password`, both git-ignored. **Never write a password into a file
that is committed**, and never paste one into a message.

## Three things you must not do

1. **Do not create a Kineviz account.** Point the person at <https://www.kineviz.com/> and let
   them sign up.
2. **Do not sign in to Kineviz for them**, in the browser or in Desktop.
3. **Do not install Kineviz Desktop without asking**, and do not assume they want it. The
   browser needs no download. Ask which they prefer.

Everything else here is yours to set up: the container, the databases, the read-only login,
the mapping, the bridge.

## Which Kineviz: browser or Desktop

**Ask; do not assume.** Both work, and the browser needs nothing installed. Do not push a
200 MB download on someone who does not want one.

| | Browser | Desktop |
| --- | --- | --- |
| Install | nothing | ~200 MB |
| Reaching a bridge on this machine | the browser asks permission the first time; the person clicks Allow | works straight away |

The bridge answers the check browsers make before allowing a web page to reach this machine,
but only for Kineviz origins and for localhost. If the person hosts Kineviz elsewhere, add it
with `--allow-origin <url>`. Do not widen that allowlist casually: the bridge has no password
of its own.

If they choose the browser and the schema never loads **and no prompt appeared**, their browser
is refusing rather than asking. Suggest Desktop then, not before.

If they want Desktop, work out which file and give them the link. **Do not download or install
it unless they ask you to.**

```bash
uname -sm      # Darwin arm64 | Darwin x86_64 | Linux x86_64 | Linux aarch64
gh api repos/Kineviz/kineviz-desktop/releases/latest --jq .tag_name
```

| Their machine | Asset from [kineviz-desktop/releases](https://github.com/Kineviz/kineviz-desktop/releases/latest) |
| --- | --- |
| Apple Silicon Mac | `Kineviz-Desktop-<ver>-mac-arm64.dmg` |
| Intel Mac | `Kineviz-Desktop-<ver>-mac-x64.dmg` |
| Windows | `Kineviz-Desktop-Setup-<ver>-win-x64.exe` (or `win-arm64`) |
| Linux (deb) | `Kineviz-Desktop-<ver>-linux-amd64.deb` (or `linux-arm64`) |
| Linux (other) | `Kineviz-Desktop-<ver>-linux-x86_64.AppImage` (or `linux-arm64`) |

## Connecting Kineviz to the bridge

Relay these, do not perform them:

1. **Create → Create New Project**
2. **Database Type:** `KoreDB Via Proxy API`
3. **Proxy API URL:** whatever the bridge printed, e.g. `http://localhost:7073/ibase/demo`
4. Confirm.

**`KoreDB Via Proxy API`, not `Database Proxy`.** The latter fails its connection check because
GraphXR runs a Bolt probe against an HTTP address. That is a bug on the GraphXR side and
nothing in this repo can fix it.

No username or password: the bridge holds the database credentials and Kineviz never sees them.

## Pointing it at a real iBase database

The demo databases are for learning the shape. For a real one:

1. **Get a read-only login.** `SELECT` only, on the tables they approve.
`sql/020_readonly_login.sql`
   is a starting point. iBase schema changes go through iBase Designer, never SQL.
2. **Discover the schema.** `python -m ibase_bridge.discovery --mapping-out mapping.proposed.yml`
   writes a draft. It is a draft. Do not load it unreviewed.
3. **Have the person check it**, in the editor at `/studio`. Two things only they can decide:
   what each link is **called**, and which way it **points**. A backwards link returns no rows
   rather than an error, so it is the mistake most likely to go unnoticed. The editor runs
   every link both ways and shows real rows so the person can judge.
4. **Then start the bridge** on the saved mapping.

Do not skip step 3, and do not guess on the person's behalf. A schema cannot tell you whether a
Person works for an Organisation or the reverse.

## When something is wrong

- `/studio` shows bridge status, the database's health, and the last failed query in plain
  words. Look there before the logs.
- `logs/queries.jsonl` has one line per query with the Cypher **and** the SQL generated from
  it, side by side. That pairing answers most questions.
- `python3 scripts/probe_queries.py http://localhost:7073/ibase/demo` fires ~33 queries and
  reports what each returned, including that the unsupported ones were properly refused.

## What this bridge will not do

Refused with a clear message, never silently mistranslated: variable-length paths, `OPTIONAL
MATCH`, `WITH`, `UNWIND`, `HAVING`, subqueries, regular expressions (`=~`; T-SQL has no regex
operator), composite keys, and all writing. If a person needs one of these, say so plainly
rather than working around it.

## Background

`docs/design-notes.md` explains the decisions that are not obvious from the code, including
three that were wrong the first time and what testing showed. Read it before changing the id
scheme, the branching, or the T-SQL emitter.
