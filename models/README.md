# Models

A model is a **plain Python object in its own installable package**. It
implements no base class, imports nothing from this platform, and does not know
that WebSockets, Unity Catalog or FastAPI exist.

That is the whole contract. Everything below is detail.

```
models/
  heartbeat/
    pyproject.toml          name, dependencies, and ONE entry point
    heartbeat/
      __init__.py           exports the factory
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
yours = "yours:build_model"           # the name DBX_MODEL takes

[tool.setuptools]
packages = ["yours"]
```

```python
# models/yours/yours/model.py
class Yours:
    def __init__(self, iterations: int = 100):
        self.iterations = iterations

    def attach(self, emit, should_cancel):
        self._emit, self._should_cancel = emit, should_cancel

    def run(self):
        for i in range(self.iterations):
            if self._should_cancel():
                return "CANCELLED"
            self._emit("progress", current=i, total=self.iterations, phase="solve")
        return "SUCCEEDED"


def build_model(config=None):
    return Yours(**(config or {}))
```

`uv sync` picks it up — `[tool.uv.workspace] members = ["models/*"]` in the
repo root's `pyproject.toml` globs the directory, so nothing central lists your
model. Then:

```python
from job.local import run_local

outcome, messages = run_local("yours", iterations=20)
```

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
