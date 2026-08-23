# Databricks Free Edition — verified constraints

Reference doc so every agent/session works from the same facts instead of
re-deriving or guessing. Verified against current docs as of Aug 2026;
sources at the bottom. If something here looks stale, re-check the source —
Free Edition's feature set changes.

## Platform limits

| Constraint | Value | Why it matters here |
|---|---|---|
| Databricks Apps per account | 3 | Not a constraint at this scale |
| App lifetime | Runs up to 24h after start/update/redeploy | There is no "always-on" WS relay. In practice this app runs ~8 business hours/day and stops; jobs must be fully independent of app uptime |
| Concurrent job tasks | **5 per account** | Hard ceiling on run concurrency across *all* models combined |
| SQL warehouse | One, 2X-Small cluster size only | Confirms: warehouse is for reads/backfill, not the write path |
| Compute | Serverless only | No custom cluster configs |
| Outbound internet | Restricted to trusted domains (expandable via LinkedIn verification) | Blocks anything needing arbitrary egress — see Gurobi below |
| Account-level APIs | No access to account console / account-level APIs | Blocks anything needing account-admin approval or beta enrolment at the account level |
| Lakebase (managed Postgres) | **Available** (added June 2026) | Real fallback for OLTP-shaped state (`run_status`) and for multi-worker fan-out via `LISTEN`/`NOTIFY`, if ever needed |

## SQL warehouse cost model

**Cost is driven by uptime, not statement count or data volume.**
Auto-stop minimum is 5 minutes via the UI, 1 minute via the API; default 10.

Consequence: anything that touches the warehouse on a short interval (a
status poll every few seconds, a reconnect-triggered backfill query) keeps it
continuously awake, which is where real cost comes from — not the number or
size of individual writes. This is why the write path bypasses the warehouse
via Delta, and why client reads should be backfill-on-demand, not polling.

## Gurobi

- **Bundled restricted licence** (comes with `pip install gurobipy`, no
  network call): **2000 variables / 2000 constraints** (200 for models with
  quadratic terms). No concurrent-session limit.
- **The bundled licence has a fixed expiry date per gurobipy release** — e.g.
  v11's expired 2025-11-24, v10's 2024-10-28, v9's 2023-10-25. Whichever
  version is pinned, record its expiry; it fails hard, not gracefully, once
  past it.
- **WLS (Web License Service)** contacts `token.gurobi.com` over the internet
  on environment creation. Free Edition's restricted egress makes this risky
  unless the trusted-domain list is confirmed to include it. **This build
  uses the bundled restricted licence only — no WLS.**

## Delta / Unity Catalog external writes

- Officially supported external Delta clients: **Spark** (GA). **Flink,
  DuckDB, StreamNative, Starburst** are Beta, and Beta access requires
  Databricks account-team approval — likely unreachable from Free Edition
  given no account-level API access.
- **Python `delta-rs` / `deltalake` is not on the documented supported-client
  list.** It may still work for external (non-managed) tables; this is
  unverified and worth a quick spike, but is not a blocker since Spark is a
  legitimate fallback (see below).
- **Managed-table writes from external clients are Public Preview**, gated
  behind `catalogManaged` ("catalog commits") and external data access being
  enabled at the metastore level.
- **VARIANT type support in the Python `deltalake` bindings lags the Rust
  kernel** (delta-rs issue #3637) — treat VARIANT as unavailable for delta-rs
  writes; fall back to a JSON string column where needed.
- **Spark is not a disaster fallback.** On Databricks serverless jobs a Spark
  session already exists, so its cost is paid once per run, not per flush.
  At the flush granularities this project uses (~1MB/30s), Delta commit
  overhead is per-flush, not per-row. Build `write_batch(table, rows)` with
  delta-rs preferred and Spark as the real second implementation, selected
  once at startup — not as an emergency path.
- **Delta's own conflict rule:** concurrent blind `INSERT`/append operations
  cannot conflict with each other (optimistic concurrency control treats
  disjoint appends as non-overlapping). The one documented exception:
  **delta-rs writing to AWS S3** needs a locking provider
  (`AWS_S3_LOCKING_PROVIDER=dynamodb`) or conditional-put support for safe
  concurrent writers — this is an S3-specific limitation, not a Delta
  protocol one. Check which cloud the workspace runs on before relying on
  concurrent same-table writes from delta-rs.

## Statement Execution API (for the read/backfill path)

- `INLINE` disposition supports **`JSON_ARRAY` only**, and aborts if the
  result exceeds **25 MiB**.
- `ARROW_STREAM` and `CSV` are supported **only with `EXTERNAL_LINKS`**
  disposition, which returns presigned URLs to cloud storage (a second hop —
  interacts with the egress restriction) and supports up to 100 GiB.
- Practical split: `JSON_ARRAY` + `INLINE` for backfill and small reads;
  `ARROW_STREAM` + `EXTERNAL_LINKS` only for genuinely large result pulls.
- Use the API's own `wait_timeout` (5–50s) so a fast statement is one round
  trip, rather than polling for completion.

## Databricks Apps ingress — unresolved, treat as a real risk

No official documentation confirms or denies WebSocket support, SSE
streaming duration limits, or idle-connection handling on the Databricks
Apps ingress. Community reports (unofficial, none resolved) consistently
point at:

- Idle connections dropped around **~30s**
- Long-lived streams cut around **~120s**
- A separate report of a **~60s** upstream timeout on long requests

Three independent, consistent reports across two different apps in this
project's own history is enough to design around, not enough to trust
blindly. This is why `/spike-ws` and `/spike-sse` exist and gate everything
else — they turn "probably" into "confirmed, with these exact numbers."

---

## Sources

- [Databricks Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations)
- [What's coming next to Free Edition (Lakebase)](https://www.databricks.com/blog/whats-coming-next-free-edition)
- [Access Databricks tables from Delta clients](https://docs.databricks.com/aws/en/external-access/unity-rest)
- [Unity Catalog credential vending](https://learn.microsoft.com/en-us/azure/databricks/external-access/credential-vending)
- [Introducing the UC Delta API](https://delta.io/blog/2026-07-03-unity-catalog-delta-api/)
- [delta-rs issue #3637 — VARIANT support](https://github.com/delta-io/delta-rs/issues/3637)
- [Statement Execution API reference](https://docs.databricks.com/api/statement-execution/v1)
- [Gurobi restricted license limits](https://support.gurobi.com/hc/en-us/articles/29682074018833-What-does-Restricted-license-for-non-production-use-only-mean)
- [Gurobi WLS setup](https://support.gurobi.com/hc/en-us/articles/13232844297489-How-do-I-set-up-a-Web-License-Service-WLS-license)
- [Databricks Apps documentation](https://learn.microsoft.com/en-us/azure/databricks/dev-tools/databricks-apps/)
