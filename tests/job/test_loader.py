import pytest

from job.loader import ModelLoadError, describe_object, load_model


class Complete:
    results_table = "results_x"
    preview_axes = ("t", "v")

    def build(self): ...
    def run(self): ...
    def results(self): return []


def test_discovers_the_conventional_surface():
    h = describe_object(Complete(), "complete")
    assert h.found["run"] == "run"
    assert h.found["build"] == "build"
    assert h.found["results"] == "results"
    assert h.results_table == "results_x"
    assert h.preview_axes == ("t", "v")


def test_aliases_are_accepted():
    class Alias:
        def fit(self): ...
        def get_results(self): return []

    h = describe_object(Alias(), "alias")
    assert h.found["run"] == "fit"
    assert h.found["results"] == "get_results"


def test_a_model_needs_neither_build_nor_results():
    class Minimal:
        def run(self): ...

    h = describe_object(Minimal(), "minimal")
    assert h.build is None and h.results is None and h.run is not None


def test_missing_run_names_everything_that_was_tried():
    class Nothing:
        pass

    with pytest.raises(ModelLoadError) as exc:
        describe_object(Nothing(), "nothing")

    message = str(exc.value)
    for expected in ("run", "solve", "fit", "sample", "execute", "grb_model", "models/README.md"):
        assert expected in message


def test_a_gurobi_style_model_needs_no_run_method():
    class Grb:
        grb_model = object()

        def results(self): return []

    h = describe_object(Grb(), "grb")
    assert h.run is None and h.gurobi_model is not None


def test_load_by_import_spec():
    h = load_model("tests.job.conftest:FakeModel", {"steps": 2})
    assert h.obj.steps == 2
    assert h.results_table == "results_fake"


def test_unimportable_module_says_so_clearly():
    with pytest.raises(ModelLoadError, match="could not import model module"):
        load_model("models.not_a_real_model")


def test_missing_attribute_lists_what_the_module_does_have():
    with pytest.raises(ModelLoadError, match="has no attribute 'Nope'"):
        load_model("tests.job.conftest:Nope")


def test_module_without_a_factory_names_the_conventions():
    with pytest.raises(ModelLoadError, match="build_model, create_model, make_model, Model"):
        load_model("json")  # a real module, but not a model package


def test_config_that_would_be_silently_ignored_is_an_error():
    class NoArgs:
        def run(self): ...

    import sys
    import types

    mod = types.ModuleType("_tmp_model_pkg")
    mod.build_model = lambda: NoArgs()  # takes no config
    sys.modules["_tmp_model_pkg"] = mod
    try:
        with pytest.raises(ModelLoadError, match="silently ignored"):
            load_model("_tmp_model_pkg", {"steps": 3})
    finally:
        del sys.modules["_tmp_model_pkg"]


def test_wire_prefers_attach_when_present():
    seen = {}

    class WithAttach:
        def attach(self, emit, should_cancel):
            seen["emit"], seen["cancel"] = emit, should_cancel

        def run(self): ...

    h = describe_object(WithAttach(), "attach")
    h.wire(emit="E", should_cancel="C")
    assert seen == {"emit": "E", "cancel": "C"}


def test_wire_falls_back_to_attributes():
    class Plain:
        def run(self): ...

    obj = Plain()
    describe_object(obj, "plain").wire(emit="E", should_cancel="C")
    assert obj.emit == "E" and obj.should_cancel == "C"


def test_a_gurobi_model_is_discovered_before_build_has_created_it():
    """Discovery runs before build(), so the attribute exists but is None.
    Presence is the capability signal; the value comes later."""

    class LateGrb:
        grb_model = None

        def build(self):
            self.grb_model = object()

        def results(self): return []

    obj = LateGrb()
    handle = describe_object(obj, "late")
    assert handle.gurobi_model_attr == "grb_model"
    assert handle.gurobi_model is None
    assert handle.run is None  # accepted anyway

    obj.build()
    handle.refresh()
    assert handle.gurobi_model is not None


def test_refresh_also_picks_up_a_callback_defined_during_build():
    class LateCallback:
        grb_model = None

        def build(self):
            self.grb_model = object()
            self.gurobi_callback = lambda m, where: None

    obj = LateCallback()
    handle = describe_object(obj, "late-cb")
    assert handle.model_callback is None
    obj.build()
    handle.refresh()
    assert handle.model_callback is not None


def test_a_gurobi_model_still_none_after_build_fails_with_an_actionable_message():
    from job.drivers import select_driver

    class NeverBuilt:
        grb_model = None

        def build(self): ...

    handle = describe_object(NeverBuilt(), "never")
    with pytest.raises(RuntimeError, match="still None after build"):
        select_driver(handle, lambda *a, **k: None, lambda: False)
