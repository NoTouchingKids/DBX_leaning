"""Make the generated copies before anything is collected.

`job/` is a self-contained deployable unit: `job/*.py` imports `.shared`, its
own copy of the repo root's `shared/`. That copy is **generated and
gitignored** — a job is only ever deployed by `databricks bundle deploy`,
whose `preinit` hook runs `scripts/sync_shared.py` before syncing, so the copy
belongs in the workspace rather than in the history.

The cost is that a fresh checkout cannot `import job` at all until it has been
made, which would show up as a collection error in a dozen files rather than
anything resembling its cause. So it is made here, before collection.

Only what is MISSING is created. Drift between a copy and `shared/` is a
different question, and `tests/deploy/test_shared_copy.py` is what asks it —
silently repairing drift here would be exactly the wrong thing, since that
test exists to catch an edit made to the wrong file.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "scripts"))

import sync_shared  # noqa: E402

sync_shared.ensure()
