# Security

## Reporting a problem

Please report security issues privately through
[GitHub's advisory form](https://github.com/Kineviz/ibase-kineviz-bridge/security/advisories/new)
rather than opening a public issue.

## What this software touches

It reads an investigative database. That shapes what matters here.

**It only reads.** Two independent guards, because one is not enough:

1. Connect with a SQL login granted `SELECT` and nothing else. `sql/020_readonly_login.sql`
   creates one and then proves it cannot write.
2. The bridge itself refuses any statement not beginning with `SELECT` or `WITH`, before it
   reaches the driver. That turns a future bug in the query generator into a loud crash rather
   than a write, and it fails safe if someone misconfigures the connection to use a privileged
   account.

**Never change an iBase schema with SQL.** i2 directs administrators to use iBase Designer.

**Credentials come from the environment**, never from the mapping file. `./setup` generates
passwords into `.sa-password` and `.ro-password`, both git-ignored. If you add a file that
could hold a credential, add it to `.gitignore` in the same commit.

**Logs record table names, timings and counts — never record contents.** `logs/` is
git-ignored, and it should stay that way: against a real database those logs contain the text
of investigative queries.

**Discovery output describes a customer's schema.** `mapping.proposed.yml` and
`discovery.json` are git-ignored for the same reason.

## No authentication

The bridge has none. It is built to run on `localhost`, or somewhere only the analyst can
reach. **Do not expose it to a network you do not control.** Anyone who can reach the port can
read everything the SQL login can read.

If you need it reachable from elsewhere, put it behind something that authenticates — a
reverse proxy, an SSH tunnel, a VPN — and use `--ssl-cert` / `--ssl-key` so the traffic is
encrypted.

## Not yet run against a real iBase database

It has been built and tested against a demo schema and a second one shaped the way iBase is
shaped. Real deployments differ by version and local customisation. Validate against a
sanitised copy before pointing it at anything live.
