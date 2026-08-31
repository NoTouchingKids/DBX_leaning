# Container tests

Twenty tests that run the platform's three deployable shapes in real
containers, each seeing only what its Databricks counterpart would see.

```bash
DBX_CONTAINER_TESTS=1 uv run pytest tests/container -v
```

Opt-in, because they build images and pull from a registry. Without the
variable they skip; with it and no Docker daemon they skip too — no Docker is
not a broken repo. The default `uv run pytest` reports `314 passed, 20 skipped`.

## Why, when 314 tests already pass

Because a subprocess inherits `sys.path`, and every isolation claim the suite
makes is checked in a process where the whole repo happens to be importable.

That is not hypothetical. Deleting `app/shared/` as apparent duplication broke
the deployed app and left **296 tests green**: pytest has the repo root on its
path, and a Databricks App does not. The bug was found by reasoning about
`source_code_path`, not by a failing test.

Containers fix that by construction rather than by discipline. **The build
context is the enforcement.** `Dockerfile.model`'s context is
`models/heartbeat/`, so it cannot copy the repo in even if someone edits it to
try. The platform is not absent by convention; it is absent from the disk.

## The three shapes

| Image | Context | Mirrors | Proves |
|---|---|---|---|
| `model` | `models/heartbeat/` | a model, alone | it needs nothing from this repo |
| `job` | repo root, minus the bundle's sync excludes | a Databricks job task | the declared dependency list is right |
| `app` | `app/` | Databricks Apps' `source_code_path` | the app carries what it imports |

Plus two negatives, which are the interesting half:

- **`job-nomodel`** — the harness with no model installed. A real deploy state
  (the job file's third dependency line missing or misspelled), and the test
  asserts the failure *names what is installed*.
- **`app-noshared`** — today's bug, on purpose. `shared/` withheld, and the app
  must fail to start. Without it, the app tests prove the app works and nothing
  proves they could notice if it stopped. A green test that cannot go red is
  not a test.

## What the job image is really checking

`Dockerfile.job`'s three `RUN pip install` lines are the three `dependencies`
entries in `resources/model_heartbeat.job.yml`, in order:

```
-r ${workspace.file_path}/job/requirements.txt
${workspace.file_path}
${workspace.file_path}/models/heartbeat
```

Then it **deletes `/src`**. That is the point rather than tidiness: what runs on
Databricks is the installed distribution, and a container that kept the source
could pass on an accidental relative import and tell us nothing. It is also
what makes `shared` load-bearing here — it lives under `app/` in the repo and
reaches the job only through `[tool.setuptools] package-dir`, so if that mapping
breaks, `test_the_source_tree_is_gone_and_everything_still_imports` is where it
surfaces.

`--network none` is the default for every run. A model that quietly fetched
something at run time would fail on Free Edition's restricted egress and
nowhere else; this is the cheap place to find out.

## Running them by hand

```bash
uv run python -m tests.container.harness build        # all five images
uv run python -m tests.container.harness shell model  # poke at one
```

## Notes on this sandbox

Two things are specific to the machine rather than to anything under test, and
both are recorded in `harness.py` beside the code that works around them:

- **The base image is `mirror.gcr.io/library/python:3.11-slim`, not
  `python:3.11-slim`.** Docker Hub's manifest fetch succeeds here and its blob
  CDN then 403s, which reads like a broken pull rather than a blocked host.
  Probed 2026-08-31: `mirror.gcr.io` and `mcr.microsoft.com` both serve
  manifests and blobs; `quay.io` and the Hub CDN do not resolve. The mirror wins
  on size (~150MB vs ~1.5GB for MCR's devcontainer image) and on being
  byte-identical to upstream.
- **The base image installs the egress proxy's CA**, because processes inside a
  container do not trust it and cannot reach it on `127.0.0.1`. On a machine
  with no `/root/.ccr/ca-bundle.crt` the harness builds from
  `base/Dockerfile.nocert` instead and nothing else changes.

The daemon itself needs `/etc/docker/daemon.json` pointing at the proxy, or it
pulls without it and fails on the first blob.
