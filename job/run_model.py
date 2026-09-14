"""What a Databricks task runs.

**Four lines, and that is the point.** This file used to be 144 lines that
searched for the repo root — `__file__`, then the call stack, then `sys.argv`,
then a marker file — because a serverless `spark_python_task` does not set
`__file__`, and because the code arrived as loose synced files that were on
nobody's `sys.path`.

None of that is needed once the code is installed rather than synced. Python
finds an installed package; there is no root to find. The same change retired
`job/shared/`, `scripts/sync_shared.py` and the drift test that policed the
copy — all of which existed only because `import shared` could not work.

Job parameters still arrive as command-line arguments (serverless tasks have no
`spark_env_vars`), so that translation is the one thing left to do:

    run_model.py DBX_RUN_ID=abc DBX_MODEL=heartbeat

An argument with an empty value is dropped rather than exported as an empty
string, so an unset `DBX_APP_URL` means "no app" rather than "an app at ''".

## Why a successful run does not call `sys.exit`

A serverless `spark_python_task` executes inside an **ipykernel**, and that
kernel treats a `SystemExit` as an exception rather than as an exit. So
`sys.exit(0)` — the ordinary, correct thing for a CLI — fails the task:

    SystemExit: 0
    An exception has occurred, use %tb to see the full traceback.
    ... Workload failed, see run output for details

with the Databricks run marked RUN_EXECUTION_ERROR. Observed on a real
deployed run, 2026-09-03: the heartbeat completed, wrote every part file to the
volume, and the task was reported as failed anyway, with `SystemExit: 0` as the
only clue. Nothing offline catches this — it needs the real kernel.

So: return on success, and raise on failure — and `sys.exit` is not used for
either, because it is the wrong tool in this environment twice over.

On success it is actively harmful, as above. On failure it is merely useless:
inside the kernel `SystemExit(1)` and a raised exception are both just
exceptions, and the platform fails the task either way. The difference is what
the run output says. `SystemExit: 1` names nothing; an exception can say which
run and where to look. A failed run SHOULD fail the task — otherwise a
scheduled job reports success having produced nothing, and neither Databricks'
retries nor its alerting ever fire.

This is the same environment that made v3's `asyncio.run` unusable (see
`job/main.py`).
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str] | None = None) -> int:
    for arg in argv if argv is not None else sys.argv[1:]:
        key, sep, value = arg.partition("=")
        if sep and value.strip():
            os.environ[key.strip()] = value.strip()

    from job.main import main as run

    return run()


if __name__ == "__main__":
    # No `sys.exit` on either path — see the note above. Returning is how a
    # success is reported; raising is how a failure is, and the message is the
    # whole reason to prefer it over `SystemExit(1)`.
    if main():
        raise RuntimeError(
            "the model run did not succeed. Its terminal `status` message on the "
            "telemetry volume carries the reason; the task log above carries the "
            "traceback if the model raised."
        )
