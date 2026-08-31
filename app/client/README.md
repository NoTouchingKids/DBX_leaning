# app/client/ — the tiny one

There is no build step, no framework, and no `node_modules`. `../dist/index.html`
is hand-written, committed, and served as-is.

That is Slice 1's decision, not a placeholder. v3's SPA was 36,700 lines, of
which 20,117 were per-model views — 2.8x the models they visualised — against
an envelope spec whose stated thesis was *zero model-specific frontend code*.
Stripping that down to a tick renderer is more work than writing one, and a
build step is one more thing to be wrong while the transport underneath is
still moving.

So: one file, one job. Show that a tick left a deployed job and arrived in a
browser. **If something breaks during Slice 1 it is the transport**, because
there is nothing else here.

## What it does

- Opens `EventSource` on `/api/runs/<run_id>/stream`.
- Renders `log`, `progress` and `status` messages as they arrive.
- Watches `seq` for gaps and offers a button that calls `GET /replay` — the
  job resends from its own telemetry. Never automatic: a routine reconnect
  produces a gap of milliseconds, and some gaps are permanent by design
  (`client_visible=false` records never travel live), so a client that loops
  "backfill until contiguous" spins forever.
- Counts *consecutive* connection failures and resets on every success. A
  naive "give up after N" would kill a healthy stream within minutes if the
  ingress cuts long-lived connections, which community reports say it does.

## What it does not do

Anything per-model. When the first real model lands, the generic view is what
it gets, and it earns a bespoke one only by demonstrably failing on it.

## Rebuilding

There is nothing to rebuild. Edit `app/dist/index.html` and commit it — the
same reason v3 committed its bundle, minus the bundler: a deploy driven from
inside Databricks has no Node runtime and sees only tracked files.
