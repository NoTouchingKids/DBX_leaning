# Writing a model

A model is a plain Python object. No base class, no registration, no import
from the platform — the harness discovers a small set of conventional names
on whatever object your package hands back. See `docs/architecture.md`
("Why models are duck-typed") for why it is this way rather than a
`ModelAdapter` hierarchy.

The authoritative list of names is `CONVENTIONS` in `job/loader.py`. This
file is the same contract in prose. If the two disagree, the code is right
and this file is stale — say so.

## The whole contract

```python
# models/my_model/__init__.py
from .model import MyModel

def build_model(config: dict) -> "MyModel":       # module-level factory
    return MyModel(config)
```

```python
class MyModel:
    results_table = "results_my_model"   # optional; DBX_RESULTS_TABLE overrides
    preview_axes = ("t", "value")        # optional; enables LTTB previews

    def __init__(self, config: dict | None = None):
        self.emit = None            # set by the harness before build/run
        self.should_cancel = None   # set by the harness; call it, don't inspect it

    def build(self): ...            # optional
    def run(self): ...              # required (unless you expose a gurobipy model)
    def results(self) -> list[dict]: ...   # optional
```

| What the harness looks for | Accepted names (first match wins) | Required |
|---|---|---|
| Factory (module level) | `build_model`, `create_model`, `make_model`, `Model` | yes |
| Wiring hook | `attach(emit, should_cancel)` | no — falls back to setting the two attributes |
| Build step | `build`, `setup`, `prepare` | no |
| The blocking call | `run`, `solve`, `fit`, `sample`, `execute` | yes\* |
| Results accessor | `results`, `get_results`, `result_rows` | no |
| Gurobi model | `grb_model`, `gurobi_model` | \* instead of a blocking call |
| Your Gurobi callback | `gurobi_callback`, `callback` | no |
| Results table | `results_table` | no |
| Preview axes | `preview_axes` — `(x, y)` column names | no |

\* Expose **either** a blocking call **or** a `gurobipy.Model`. A Gurobi model
must not call `optimize()` itself: the harness owns the solve so it can attach
its own progress/log/cancellation observers to the single callback slot Gurobi
allows, composed with yours. Calling `optimize()` yourself bypasses
cancellation and progress entirely.

If nothing runnable is found, the loader fails with a message listing every
name it tried. You should never need to read `job/loader.py` to find out what
it wanted.

## `emit(type, **fields)`

Your entire coupling surface. Four types, documented in full in
`docs/message-envelope-spec.md`:

```python
self.emit("log", message="built 840 vars", source="model", phase="build")
self.emit("progress", elapsed_seconds=12.5, percent_complete=40.0,
          primary_metric=0.03, primary_metric_label="mip_gap",
          payload={"nodes_explored": 900})
self.emit("result", rows=[{"t": 0, "value": 1.2}, ...])
```

- You never supply `run_id`, `seq` or `ts` — the harness stamps all three.
- **`emit` never raises for a transport problem.** Nobody listening is a
  normal state of affairs. It *does* raise if your message is malformed,
  because that is your bug and swallowing it is how message shapes drift.
- `emit` is safe to call from whatever thread your blocking call runs on.
- Emitting `status` is the harness's job, not yours. Return a status string
  from your `run()` if you need to say something it could not infer
  (`"INFEASIBLE"`).

### Results

Pass `rows=[...]` to `emit("result", ...)` and the harness will:

1. write them to your results table (stamped with `run_id` and `chunk_index`),
2. count them into `row_count` — the field that distinguishes "succeeded,
   wrote 8,760 rows" from "succeeded, wrote nothing",
3. build a bounded `preview` (LTTB if you set `preview_axes`),
4. fill in `fetch_hint` so a client can pull the full set on demand.

Do not pass `row_count` yourself; do not put the rows on the message. Call it
**once per chunk** if your model produces results incrementally — each chunk
gets its own `chunk_index`, its own count, and `final=False` until the last.

If you never call it, the harness calls your `results()` accessor once at the
end instead. It will not do both.

## Cancellation

```python
for i, item in enumerate(work):
    if self.should_cancel():
        break          # keep what you have; do not raise
    ...
```

A cancelled run is a **clean outcome**, not an error. Results you already
produced are written and the run reports `CANCELLED`. Check the signal at
whatever granularity is natural for your model — between scenarios, between
epochs, between draws — not mid-computation.

## Non-goals for every model in this repo

- No WebSocket, HTTP, Delta or SQL code. Only `emit(...)`.
- No knowledge of `run_id`, `seq` or timestamps.
- No import from `job/`, `app/` or `shared/`. You conform to a documented
  contract; you do not call into the platform. A model must behave identically
  run standalone, which is also how its tests run.

## Testing

Mock the two things the harness hands you — that is the entire test harness:

```python
model = build_model({"n": 10})
messages = []
model.emit = lambda type, **f: messages.append((type, f))
model.should_cancel = lambda: False
model.run()
```

`job.loader.describe_object(model)` tells you what the harness would discover,
so a model's own test suite can assert its surface without importing by string.
