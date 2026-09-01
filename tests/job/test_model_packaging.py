"""A model is its own package, and that is what makes it workable alone.

These tests are about the developer experience the restructure was for: open a
notebook, import a model, run it. Everything else in this repo can be broken
and these should still pass — that is the property, not a side effect.
"""

from __future__ import annotations

import subprocess
import sys

from job.loader import ENTRY_POINT_GROUP, installed_models, load_model


def test_importing_a_model_loads_nothing_but_the_model():
    """The complaint that started this: importing one model dragged in the
    whole harness, because `job/models/` sat under a package whose
    `__init__` imported config, loader and cancellation.

    Run in a subprocess so an already-imported harness cannot mask it.
    """
    code = (
        "import sys; import heartbeat; "
        "print(sorted(k for k in sys.modules "
        "if k.split('.')[0] in {'job', 'shared', 'heartbeat'}))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert out == "['heartbeat', 'heartbeat.model']", (
        f"importing a model pulled in more than itself: {out}"
    )


def test_a_model_needs_no_platform_dependency_to_run():
    """A model is handed `emit` and a cancel check. It imports neither the
    envelope nor the harness — so it runs in a notebook with nothing
    installed but itself."""
    from heartbeat import Heartbeat

    seen = []
    model = Heartbeat(seconds=0.05, hz=40)
    model.attach(emit=lambda type, **fields: seen.append(type), should_cancel=lambda: False)

    assert model.run() == "SUCCEEDED"
    assert "progress" in seen


def test_models_are_discovered_by_entry_point_not_by_a_registry():
    """`[tool.dbx-leaning.models]`, `scripts/_registry.py` and a dotted path in
    DBX_MODEL are all replaced by this. A model in another repository is
    discovered identically to one in this one."""
    # The CLASS, not a factory. `modelkit.Model.__init__` takes both a config
    # dict and keywords, so the factory function a model used to need has
    # nothing left to do — `heartbeat.build_model` survives only because it is
    # the first name `job/loader.py` looks for and should keep working.
    assert installed_models().get("heartbeat") == "heartbeat:Heartbeat"
    assert ENTRY_POINT_GROUP == "dbx_leaning.models"


def test_the_harness_loads_a_model_by_plain_name():
    handle = load_model("heartbeat", {"seconds": 0.05, "hz": 40})
    assert handle.run is not None
    assert handle.attach is not None


def test_an_uninstalled_model_can_still_be_loaded_by_import_path():
    """A model being written is not installed yet, and should not have to be.
    Requiring the entry point would make this a framework you must package
    before you can run once."""
    handle = load_model("heartbeat.model:build_model", {"seconds": 0.05})
    assert handle.run is not None


def test_an_unknown_model_names_what_is_installed():
    from job.loader import ModelLoadError

    try:
        load_model("no_such_model")
    except ModelLoadError as exc:
        assert "heartbeat" in str(exc), "the error does not say what IS available"
        assert ENTRY_POINT_GROUP in str(exc)
    else:
        raise AssertionError("loading a model that does not exist should fail")
