"""Model packages. One per model, each a plain Python object.

A model imports nothing from ``app/``, ``job/`` or ``shared/`` — it conforms to
the envelope's documented shape via the ``emit()`` callback the harness hands
it, and behaves identically run standalone. See ``models/README.md``.
"""
