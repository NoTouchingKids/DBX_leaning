"""Almost nothing to arrange before collection, and that is the change.

This file used to generate `job/shared/` — a gitignored copy of `shared/`,
made by `scripts/sync_shared.py` — because a fresh checkout could not
`import job` at all until it existed. That copy, its generator, the drift test
that policed it and the bundle's preinit hook were all workarounds for one
thing: `shared` was not an installed package.

It is now, along with `job`, `modelkit` and every model under `models/`, as uv
workspace members. Python finds them, so there is no path to insert.

What remains is one line for Windows, below.
"""

from __future__ import annotations

import asyncio
import sys

# Windows defaults to the Proactor event loop, which psycopg's async mode
# refuses outright:
#
#   psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
#   in async mode.
#
# That takes out every test in tests/app/test_run_store.py — 21 errors and 3
# failures — on a Windows dev machine, and none of them on Linux or in the
# containers, so it reads as a broken repo rather than a platform default.
#
# It belongs here rather than in tests/app/ because pytest-asyncio builds its
# loop from the policy in force at collection, before any per-package fixture
# runs. The deployed app is Linux and never sees this.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
