# Contributing

## Running the tests

```bash
python3 tests/test_pipeline.py     # 50 tests, no database, PyYAML is the only dependency
python3 -m pytest                  # the same tests
```

They run against the mapping files and a small fake connection, so the whole suite finishes in
about a second on a machine with nothing installed. Keep it that way: a test that needs a
database belongs in the integration job, not here.

For the real thing:

```bash
./setup
python3 scripts/probe_queries.py http://localhost:7073/ibase/demo
```

`probe_queries.py` fires ~33 queries at a running bridge and reports what each returned. It
also checks that the **unsupported** ones were refused — see below for why that matters.

## Two rules worth stating

**Fail loudly.** A query this bridge cannot translate must return an error a person can read,
never an empty graph. An empty canvas looks exactly like "nothing matched", and someone will
believe it. If you add a construct the emitter cannot handle, reject it by name.

**Do not guess on the user's behalf.** A schema cannot say which way a link points, and a
backwards link returns no rows rather than an error. Discovery *proposes*; a person decides.

## Before you change the interesting parts

Read [`docs/design-notes.md`](docs/design-notes.md). It covers the decisions that are not
obvious from the code, including three that were wrong the first time and what testing showed —
the node id scheme, how Expand branches, and the T-SQL emitter. Each has a constraint behind it
that is easy to break by accident.

## Adding a test

Name it after the behaviour, not the function: `test_parallel_links_between_the_same_pair_stay_distinct`
rather than `test_convert_rows`. Several tests in the suite exist because something broke in a
way nobody noticed for a while; the name is what stops it happening twice.

## Style

Match what is there. Comments explain **why**, especially where the reason is a constraint you
cannot see from the code — SQL Server's parameter ceiling, what Kineviz sends back on Expand,
what iBase permits.
