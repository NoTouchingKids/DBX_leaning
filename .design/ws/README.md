# The job's WebSocket path, as a canvas

Four artboards documenting `job/channels.py` and the live path around it:
the session lifecycle, the two-header ingress auth, the relay that feeds the
socket, and the teardown race that drops a fast run's live stream.

These `.dc.html` files and `canvas.json` are the **source**. The seeded page
is generated from them and gitignored — it carries its own copy of the editor,
which is why it is 2.5 MB and why it never gets committed.

Published at: https://claude.ai/code/artifact/76fb2235-8117-4e92-a23a-fe4ad67726fd

To change a board, edit the `.dc.html`, re-seed, and republish to that same
URL. The `/design` skill carries the seeding command; the canvas cannot be
rebuilt from the published page alone without it.

Diagrams go stale. If one of these disagrees with the code, the code is right.
