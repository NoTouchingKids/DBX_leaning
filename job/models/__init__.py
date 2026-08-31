"""Model packages.

One package per model, each a plain Python object the harness discovers by
duck typing — see `job/loader.py` and `docs/architecture.md`.

There is exactly one model here right now, and that is the point of Slice 1
rather than a gap: eleven models is what made v3's platform impossible to
experiment on, because every transport change had twenty-two downstream
consumers. The other eleven are on `dev`, deployed and working, and come back
one at a time once the transport underneath them has stopped moving.
"""
