# Databricks notebook source
# MAGIC %md
# MAGIC # Running a model from a notebook
# MAGIC
# MAGIC No `sys.path` juggling, no repo-root search, no `%run`. The two things
# MAGIC you might want to do are both one import:
# MAGIC
# MAGIC | Question | What you need |
# MAGIC |---|---|
# MAGIC | Is my model's LOGIC right? | just the model — `from heartbeat import Heartbeat` |
# MAGIC | Is the RUN right — telemetry, cancel? | `from job.local import run_local` |
# MAGIC
# MAGIC This works because each piece is an installed package rather than a
# MAGIC folder someone has to locate. Install once per cluster session; after
# MAGIC that Python finds them the ordinary way.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Install
# MAGIC
# MAGIC The paths are RELATIVE to this notebook, which works because Databricks
# MAGIC sets the working directory to the notebook's own folder. `..` is the repo
# MAGIC root (the harness and the envelope); the second is the model.
# MAGIC
# MAGIC Installing them SEPARATELY is the point, not a convenience — the model is
# MAGIC its own distribution with its own dependency list. Working on a model that
# MAGIC needs torch? Only its line pulls torch in.
# MAGIC
# MAGIC If relative paths give you trouble (a notebook opened from somewhere
# MAGIC unexpected, an older runtime), run the cell below this one to print the
# MAGIC absolute paths and paste those instead.

# COMMAND ----------

# MAGIC %pip install .. ../models/heartbeat

# COMMAND ----------

# MAGIC %md
# MAGIC Stuck? This prints the exact `%pip install` line for wherever this
# MAGIC notebook actually is. `%pip` takes literal text, not Python variables, so
# MAGIC this prints a line to copy rather than running it for you.

# COMMAND ----------

import pathlib

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
here = pathlib.Path("/Workspace" + ctx.notebookPath().get()).parent
root = here.parent
print(f"%pip install {root} {root}/models/heartbeat")

# COMMAND ----------

# MAGIC %md
# MAGIC Required after `%pip install`, and the single most common reason a
# MAGIC notebook still reports `ModuleNotFoundError` after a successful install:
# MAGIC the interpreter has to restart before it can see the new packages.

# COMMAND ----------

dbutils.library.restartPython()  # noqa: F821

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. The model on its own
# MAGIC
# MAGIC No harness, no Databricks, no envelope. A model is handed two callables
# MAGIC and knows nothing else — so a caller can be four lines, which is what
# MAGIC makes a model debuggable here at all.
# MAGIC
# MAGIC `emit(type, **fields)` and `should_cancel()` are the ENTIRE coupling
# MAGIC surface. Print them, collect them, ignore them.

# COMMAND ----------

from heartbeat import Heartbeat

model = Heartbeat(seconds=3, hz=2)
model.attach(emit=lambda t, **f: print(t, f), should_cancel=lambda: False)
model.run()

# COMMAND ----------

# MAGIC %md
# MAGIC Cancellation is just a callable you control, so you can test it without
# MAGIC a job, a token type or a socket.

# COMMAND ----------

ticks = {"n": 0}


def stop_after_three() -> bool:
    ticks["n"] += 1
    return ticks["n"] > 3


model = Heartbeat(seconds=60, hz=10)
model.attach(emit=lambda *a, **k: None, should_cancel=stop_after_three)
print("status:", model.run())

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. The whole run
# MAGIC
# MAGIC `run_local` drives the REAL harness — the same one a deployed job runs.
# MAGIC It writes real telemetry part files and reads the messages back OFF those
# MAGIC files rather than from memory, so if the durable record is wrong you find
# MAGIC out here instead of on a workspace.
# MAGIC
# MAGIC What it does not do is talk to Databricks. There is no app, no socket, no
# MAGIC Unity Catalog. A run with nothing watching is the normal case.

# COMMAND ----------

from job.local import run_local

run = run_local("heartbeat", seconds=5, hz=2)

print("status  :", run.outcome.status)
print("messages:", len(run.messages))
print("progress:", len(run.of_type("progress")))
print("parts in:", run.telemetry_dir)

# COMMAND ----------

# MAGIC %md
# MAGIC Point `telemetry_dir` at a volume and the part files land where a real
# MAGIC run's would. This is how to develop the ingestion job — it reads exactly
# MAGIC these files, and needs no library of ours to do it.

# COMMAND ----------

run = run_local(
    "heartbeat",
    run_id="notebook-1",
    seconds=5,
    hz=2,
    telemetry_dir="/Volumes/main/dbx_leaning/telemetry",
    on_message=lambda m: print(m["seq"], m["type"]),  # the live channel, as print
)
print(run.outcome.status)

# COMMAND ----------

import json
import pathlib

part_dir = pathlib.Path(run.telemetry_dir) / "runs" / "notebook-1"
for part in sorted(part_dir.glob("part-*.jsonl")):
    print("--", part.name)
    for line in part.read_text().splitlines()[:3]:
        print("  ", json.loads(line)["type"], json.loads(line)["seq"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Watching it live — the WebSocket
# MAGIC
# MAGIC Same call, one more argument. `app_url` opens the SAME WebSocket a
# MAGIC deployed job opens, with the same credentials, through the same function
# MAGIC (`job.ws.app_client`) — so what you prove here is true of the job, not
# MAGIC just of this notebook.
# MAGIC
# MAGIC **This is worth doing before blaming the job.** A notebook authenticates
# MAGIC as YOU; a job authenticates as its own principal. So this separates two
# MAGIC questions that a `302` cannot tell apart on its own:
# MAGIC
# MAGIC | Here | In the job | Means |
# MAGIC |---|---|---|
# MAGIC | delivered > 0 | 302 | the app is fine; the JOB's principal lacks `CAN_USE` |
# MAGIC | 302 | 302 | the app's ingress rejects everyone — check the app is running |
# MAGIC
# MAGIC **A live channel cannot fail a run.** Unreachable app, missing grant,
# MAGIC expired token — all of them leave the run `SUCCEEDED` with nothing
# MAGIC delivered, because a run nobody watched is the normal case. So a green
# MAGIC status says nothing about the socket: check `observed`.

# COMMAND ----------

# Paste your app's URL to enable this section. Empty means skip, so the
# notebook stays runnable end to end without one.
APP_URL = ""  # e.g. "https://dbx-leaning-1234567890.aws.databricksapps.com"
APP_TOKEN = None

# COMMAND ----------

# MAGIC %md
# MAGIC The app's own shared secret, from the workspace secret scope. It is NOT
# MAGIC your Databricks identity and does not replace it — the two travel on
# MAGIC different headers and the ingress needs both. `job/auth.py` has the why.
# MAGIC
# MAGIC The scope is a WORKSPACE scope, which is a flat namespace: `dbx-leaning`
# MAGIC the scope, not `dbx_leaning` the schema. One character apart, and a
# MAGIC secret created in Unity Catalog cannot be read from here.

# COMMAND ----------

APP_TOKEN = dbutils.secrets.get(scope="dbx-leaning", key="app-token")  # noqa: F821

# COMMAND ----------

if not APP_URL:
    print("APP_URL is empty — skipping the live section")
else:
    run = run_local(
        "heartbeat",
        run_id="notebook-live-1",
        seconds=30,
        hz=1,
        app_url=APP_URL,
        app_token=APP_TOKEN,
        on_message=lambda m: print(m["seq"], m["type"]),
    )
    print()
    print("status   :", run.outcome.status)
    print("observed :", run.observed)
    print("delivered:", run.delivered, "of", run.outcome.live_offered, "offered")
    print("connects :", run.connects)
    print("error    :", run.last_error)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Reading the result
# MAGIC
# MAGIC Three numbers, and together they say WHICH way it failed:
# MAGIC
# MAGIC | | Meaning |
# MAGIC |---|---|
# MAGIC | `connects: 0` | never got a socket open — see `error` |
# MAGIC | `connects: 1`, `delivered: 0` | connected, sent nothing — suspect the app's token check |
# MAGIC | `delivered > 0` | it works. Open the app in a browser and watch the run |
# MAGIC
# MAGIC `error` containing **`HTTP 302`** is the one worth recognising, because it
# MAGIC is not what it looks like. The Apps proxy answers an unauthenticated
# MAGIC upgrade with a redirect to the OAuth login page rather than a `401`, so a
# MAGIC credentials problem arrives dressed as a routing one. It means either no
# MAGIC Databricks identity was presented, or the principal that was presented
# MAGIC lacks `CAN_USE` on the app. `job/ws.py::diagnose` says this in the log.
# MAGIC
# MAGIC While it runs, the same messages are landing on the volume. That is not a
# MAGIC fallback tier — the durable path runs regardless of whether anyone is
# MAGIC watching, and section 3 above reads it back.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Working on a model that is not installed yet
# MAGIC
# MAGIC `run_local` takes a NAME if the model is installed, or an import path if
# MAGIC it is not — so a model you are still writing works before it has a
# MAGIC `pyproject.toml`:
# MAGIC
# MAGIC ```python
# MAGIC run_local("mypkg.experiment", iterations=50)
# MAGIC ```
# MAGIC
# MAGIC What the harness looks for on your object, and in what order, is in
# MAGIC `models/README.md`. Its failures name every alternative they tried, so you
# MAGIC should not have to read `job/loader.py` to find out what it wanted:

# COMMAND ----------

from job.loader import installed_models

print(installed_models())
