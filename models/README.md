# Models

A model is a **plain Python object in its own installable package**. It knows
nothing about WebSockets, Unity Catalog or FastAPI, and reaches the platform
only through the `emit` callback it is handed.

The harness finds a model **structurally** — by looking for methods, never for
a base class. `libs/modelkit` gives you those methods for free, and using it is
optional in the strict sense: `job/loader.py` does not import it and never
will.

```
models/
  heartbeat/
    pyproject.toml          name, dependencies, and ONE entry point
    heartbeat/
      __init__.py           exports the class
      model.py              the actual model
```

## The five minutes version

```toml
# models/yours/pyproject.toml
[project]
name = "dbx-model-yours"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["scipy>=1.13"]        # yours alone; nothing else pays for them

[project.entry-points."dbx_leaning.models"]
yours = "yours:Yours"                 # the name DBX_MODEL takes

[tool.setuptools]
packages = ["yours"]
```

```python
# models/yours/yours/model.py
from modelkit import Model


class Yours(Model):
    unit = "iterations"

    def configs(self):
        return {"iterations": 100, "tolerance": 1e-6}

    def prestep(self):
        self.total = self.iterations
        self.solver = build_something(self.tolerance)

    def step(self, i):
        gap = self.solver.advance()
        return {"metric": gap, "label": "gap"}
```

That is the whole model. **`modelkit` is deliberately absent from
`dependencies`** — it is installed once into the serverless ENVIRONMENT, not
once per model, exactly as `pyspark` is. See `libs/modelkit/pyproject.toml`.

### What the template gives you

Everything a model would otherwise write again, slightly differently:

| | |
|---|---|
| `attach` | wiring `emit` and `should_cancel` |
| the loop | with `total`-aware percentages, or none when the total is unknown |
| cancel polling | between steps, and inside `self.sleep()` within ~100ms |
| progress | one message per step, `metric`/`label` lifted out of your dict |
| logs | a start line and a finish line, so a run is legible with no UI |
| `poststep(status)` | runs on the cancelled and failed paths too, so results survive |

Return `STOP` from `step` to finish early and successfully — a solver that
converges is `SUCCEEDED`, not interrupted. Set `total = None` for work whose
length you cannot know; progress then carries no percentage, because a made-up
one is worse than an absent one.

### When the template does not fit

Override `run()` and you are still a model. Write the object by hand with an
`attach` and a `run` and you are still a model — that is what "discovered
structurally" buys, and `job/loader.py`'s error messages name every method
name it tried.

`uv sync` picks it up — `[tool.uv.workspace] members = ["models/*"]` in the
repo root's `pyproject.toml` globs the directory, so nothing central lists your
model. Then:

```python
from job.local import run_local

outcome, messages = run_local("yours", iterations=20)
```

### From a Databricks notebook

Same two imports, after installing the repo and your model as packages:

```python
%pip install .. ../libs/modelkit ../models/yours   # relative to the notebook
dbutils.library.restartPython()   # required, and the usual reason it "doesn't work"
```

Three paths: the harness, the template, and your model. A deployed job gets the
middle one from its serverless environment; a notebook is its own environment,
so you add it by hand.

`notebooks/heartbeat.py` is a worked example — model alone, then the full run,
then the live WebSocket, then reading the part files back. Its code cells are
executed by `tests/test_notebook.py`, so it stays true.

To watch a run arrive at the app while it happens, pass the app's URL:

```python
run = run_local(
    "yours",
    app_url="https://<app>.databricksapps.com",
    app_token=dbutils.secrets.get("dbx-leaning", "app-token"),
)
run.observed  # did anything ARRIVE — a green status says nothing about this
run.last_error  # and if not, why
```

That opens the same socket a deployed job opens, through the same function, so
what it proves is true of the job too. A notebook authenticates as you and a
job as its own principal, which is what makes this the fastest way to tell
"the app's ingress is broken" from "the job's principal lacks `CAN_USE`".

## What the harness looks for

Duck-typed, by name, in preference order. `job/loader.py` holds the table
(`CONVENTIONS`) and its failures name every alternative it tried, so you should
never have to read that file to find out what it wanted.

| Role | Names tried, in order | Required |
|---|---|---|
| factory | `build_model`, `create_model`, `make_model`, `Model` | yes |
| wiring | `attach` | in practice |
| setup | `build`, `setup`, `prepare` | no |
| execution | `run`, `solve`, `fit`, `sample`, `execute` | yes¹ |
| results | `results`, `get_results`, `result_rows` | no |
| solver handle | `grb_model`, `gurobi_model` | no |
| solver callback | `gurobi_callback`, `callback` | no |
| results table | `results_table` (a string) | no |
| preview axes | `preview_axes` (a 2-tuple) | no |

¹ or a gurobipy model attribute, which the harness can drive itself.

The factory takes an optional config dict and returns the object. If it takes
no arguments and `DBX_MODEL_CONFIG` supplied some, that is an error rather than
a silent drop — a run that ignores its own configuration is worse than one that
refuses to start.

## `emit` is your entire coupling surface

`attach` hands you two callables and nothing else:

- `emit(type, **fields)` — `type` is one of `log`, `progress`, `status`,
  `result`. The harness stamps `run_id`, `seq` and `ts`; you supply the rest.
  See `docs/message-envelope-spec.md` for the fields each type takes.
- `should_cancel()` — poll it wherever you can afford to. Return a terminal
  status when it goes true; the harness reconciles what you say with what it
  observed.

`emit` never raises into your run and never blocks on a network. It writes to
the durable path first and *offers* the message to a live channel second, so a
run with nobody watching behaves exactly like one with an audience. That is the
normal case: apps run about eight hours a day and jobs do not.

## Rules that are not stylistic

**Depend on nothing from this repo.** The heartbeat's `dependencies` list is
empty and that is the proof: the contract costs a model no dependencies at all.
A model that imports `shared` or `job` stops being movable, and being movable is
the point — discovery is by entry point, so a model in an entirely different
repository is found identically to one here.

**Own your own data.** Read what you need and write your results yourself, to
the table you name. The harness has no writer to lend you and no table to be
told about. A job runs on its own schedule whether or not the app is up, so
"the app will persist it" is not available.

**Declare your libraries in your own `pyproject.toml`.** That is what makes a
model needing torch and a model needing nothing cost the same to every other
environment. `job/requirements.txt` is the harness floor and stays that way —
`tests/deploy/test_heartbeat_job.py` fails if a model library reaches it.

## Deploying one

Copy `resources/model_heartbeat.job.yml`, change the name, the tags and the
last dependency line:

```yaml
dependencies:
  - -r ${workspace.file_path}/job/requirements.txt
  - ${workspace.file_path}
  - ${workspace.file_path}/models/yours
```

The `project: dbx-leaning` tag is how the app finds the job — not a bundle
variable it interpolated — so a job can move to another bundle or repo without
the app changing. `model: yours` is how it names it. A misspelling means the app
simply does not see the job; `/healthz` reports what was discovered, which is
what keeps that from being a mystery.
