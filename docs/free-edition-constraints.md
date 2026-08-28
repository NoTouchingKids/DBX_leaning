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
  past it. This build pins `gurobipy >=13,<14`, whose bundled licence expires
  **2027-11-29** (verified 2026-08-22 against 13.0.2). The pin and the date
  live together in `pyproject.toml`, the table of them is in
  `job/models/gurobi_scheduling/LICENCE_EXPIRY.md`, and
  `scripts/check_gurobi_licence.py` re-reads the date from the installed
  package so the next person does not have to do archaeology.
- **Lazy constraints do not count against the 2000-constraint cap.** Measured
  2026-08-23 against gurobipy 13.0.2: a capacitated routing model separating
  ~412 rounded-capacity cuts kept `NumConstrs` at 25 — Gurobi holds separated
  cuts in the lazy pool rather than adding them to the model. This is what
  makes cut-based formulations (routing, subtour elimination) viable at all
  under the restricted licence, where the same constraints stated up front
  would blow the cap immediately. `job/models/gurobi_routing/` relies on it and
  pins the behaviour in a test, because it is a licence-relevant assumption
  and not documented by Gurobi as a guarantee.
- **WLS (Web License Service)** contacts `token.gurobi.com` over the internet
  on environment creation. Free Edition's restricted egress makes this risky
  unless the trusted-domain list is confirmed to include it. **This build
  uses the bundled restricted licence only — no WLS.**
- **There is an escape hatch, and it is a different solver.**
  `job/models/ortools_jobshop` runs on OR-Tools CP-SAT: Apache-2.0, CPU-only, no
  licence file, no expiry date, nothing to contact, and **no variable or
  constraint cap**. Every constraint in this section is a property of the
  bundled Gurobi licence rather than of Free Edition, so a model that cannot
  be sized to fit 2000/2000 — or that would be stranded by the expiry date —
  has somewhere to go without asking the platform for anything. Its extra is
  `ortools`, and only its own job environment installs it.

## Getting data in from outside

The project's own restriction to the `samples` catalog was **lifted on
2026-08-24**: external data is allowed where it genuinely suits a model. The
*platform* restriction that shapes how it can arrive has not lifted, and the
two are easy to confuse.

**Outbound traffic is restricted to trusted domains, including from job
compute.** So a model cannot fetch a dataset at run time. This is the same
constraint that rules out Gurobi's WLS licence (`token.gurobi.com`), and it
fails in the least helpful way: the code works on a laptop and hangs or
errors on the job, after the run has already started and claimed one of five
account-wide task slots.

External data therefore means **land it in Unity Catalog first**, once, out
of band. Routes, roughly in order of how little they can go wrong:

| Route | What it is | Notes |
|---|---|---|
| Upload to a UC **volume** | Drop a file in, read it with Spark | No egress at run time. The obvious route for a one-off CSV/Parquet |
| **Databricks Marketplace** | Free data products attach as a catalog | Governed, no egress, no file to keep in sync. Best fit when a product matches |
| **Delta Sharing** | A shared catalog from a provider | Same shape as `samples` itself |
| A **notebook** run once | Fetch and write a table by hand | Only if the source is on the trusted-domain list; a notebook is not exempt from egress rules |
| **`/Volumes/samples/databricks/datasets/`** | A Unity Catalog volume of file-based sample data | Governed, egress-free, and the file source to build on. Invisible to `information_schema.tables` because a volume is not a table |
| ~~`/databricks-datasets`~~ | The legacy DBFS mount | **Do not build on it.** Databricks "recommends against using DBFS ... in Unity Catalog-enabled workspaces", and says its contents "are subject to change without notice". `scripts/probe_sample_data.py` still reports whether it is reachable |
| **Data bundled in a PyPI package** | Installed with the environment | Genuinely egress-free at run time, because pip already ran. Small datasets only, and it inflates every model environment that lists the extra |

Whatever the route, the model still reads a table, and
`job/models/_data.load()` still takes arbitrary SQL — it was never
samples-specific. The synthetic fallback stays mandatory either way: it is
what keeps a model runnable in tests and on a laptop, and it is the reason a
missing table degrades instead of failing a run.

`job/models/panel_fit` is what that looks like in practice, and it is worth
reading before landing anything. It names
`main.dbx_leaning.owid_country_year` as its default table — a table nobody
has created — so *every* run at the default configuration takes the fallback
and reports it: logged at the `input` phase, and stamped on every result row
as `data_source` / `data_synthetic` / `data_rows` / `data_fallback_reason`.
Nothing about that is a failure state. Pointing a model at the table it
actually wants, and having the run say plainly that the table was not there,
is a better position than pointing it at whatever happens to exist — and it
means landing the CSV is the only step between here and real data, with no
code change at all.

## Delta / Unity Catalog external writes

- Officially supported external Delta clients: **Spark** (GA). **Flink,
  DuckDB, StreamNative, Starburst** are Beta, and Beta access requires
  Databricks account-team approval — likely unreachable from Free Edition
  given no account-level API access.
- **Python `delta-rs` / `deltalake` is not on the documented supported-client
  list.** It may still work for external (non-managed) tables; that is
  unverified, and it is not a blocker, because Spark turned out to be the
  write path outright rather than the fallback it was designed as, and
  delta-rs has since been removed from `job/delta.py` entirely (see below).
- **Managed-table writes from external clients are Public Preview**, gated
  behind `catalogManaged` ("catalog commits") and external data access being
  enabled at the metastore level.
- **VARIANT type support in the Python `deltalake` bindings lags the Rust
  kernel** (delta-rs issue #3637) — treat VARIANT as unavailable for delta-rs
  writes; fall back to a JSON string column where needed.
- **Spark is the write path, not a fallback.** On Databricks serverless jobs a
  Spark session already exists, so its cost is paid once per run, not per
  flush. At the flush granularities this project uses (~1MB/30s), Delta commit
  overhead is per-flush, not per-row.
- **delta-rs cannot address a Unity Catalog table by name, and fails silently
  when you try.** `write_deltalake()` takes a path or URI. Handed
  `"main.dbx_leaning.run_logs"` it does not raise — it creates a *local
  directory* with that literal name and writes there. Verified 2026-08-23. Any
  job doing this would report SUCCEEDED with an accurate `row_count` while its
  telemetry sat in an ephemeral container filesystem. This is why there is no
  delta-rs writer in the repo at all: `DeltaRsWriter` and
  `WriterKind.DELTA_RS` are deleted rather than stubbed, so the durable-write
  selector cannot be handed a name for that failure. Making delta-rs usable
  means resolving the table to a storage location and obtaining credentials
  via UC credential vending — new work, not a switch.
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

## Databricks Apps ingress — WebSocket and SSE both work; the numbers are not measured

No official documentation confirms or denies WebSocket support, SSE streaming
duration limits, or idle-connection handling on the Databricks Apps ingress.
That is still true of the documentation. It is no longer true of this
project: **both `/spike-ws` and `/spike-sse` were run against a real
workspace on 2026-08-23 and both passed** — see `docs/spike-results.md`.
WebSocket `Upgrade` survives the ingress, and SSE streams through it. The
question that stayed open across all three builds of this platform is closed,
and the transport in `docs/architecture.md` is the one being built rather
than a hedge.

What remains unmeasured is the timing, and the community reports (unofficial,
none resolved) are still the only figures anyone has:

- Idle connections dropped around **~30s**
- Long-lived streams cut around **~120s**
- A separate report of a **~60s** upstream timeout on long requests

Three independent, consistent reports across two different apps in this
project's own history is enough to design against, which is what
`DBX_WS_PING_S` (20s, `job/config.py`) and `DBX_SSE_KEEPALIVE_S` (10s,
`app/server/config.py`) are set from. They are conservative guesses that work, not
tuned values. `docs/spike-results.md` lists the three measurements that would
change specific code, and none of them is recorded yet.

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
