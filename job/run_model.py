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
    sys.exit(main())
