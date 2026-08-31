"""Nothing to arrange before collection any more, and that is the change.

This file used to generate `job/shared/` — a gitignored copy of `shared/`,
made by `scripts/sync_shared.py` — because a fresh
checkout could not `import job` at all until it existed. That copy, its
generator, the drift test that policed it and the bundle's preinit hook were
all workarounds for one thing: `shared` was not an installed package.

It is now, along with `job` and every model under `models/`, as uv workspace
members. Python finds them, so there is nothing to prepare and no path to
insert. The file stays because pytest uses its location to fix the rootdir,
and because its absence would invite someone to recreate what it explains.
"""
