"""Duck-typed model discovery.

A model is a plain Python object. No base class, no registration, no
inheritance — the harness looks for a small set of conventional names on
whatever object the model package hands back. See ``docs/architecture.md``
("Why models are duck-typed") for why this is the shape, and
``models/README.md`` for the contract as a model author sees it.

When something required is missing, the failure names every alternative that
was tried. A model author should never have to read this file to find out
what the harness wanted.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["ModelHandle", "ModelLoadError", "load_model", "CONVENTIONS"]

#: The conventional names, in preference order. Adding an alias here is
#: cheap; renaming one is a breaking change for every model package.
CONVENTIONS: dict[str, tuple[str, ...]] = {
    "factory": ("build_model", "create_model", "make_model", "Model"),
    "attach": ("attach",),
    "build": ("build", "setup", "prepare"),
    "run": ("run", "solve", "fit", "sample", "execute"),
    "results": ("results", "get_results", "result_rows"),
    "gurobi_model": ("grb_model", "gurobi_model"),
    "model_callback": ("gurobi_callback", "callback"),
    "results_table": ("results_table",),
    "preview_axes": ("preview_axes",),
}


class ModelLoadError(RuntimeError):
    """Raised with a readable account of what was looked for and not found."""


def _find(obj: Any, key: str) -> tuple[str, Any] | None:
    for name in CONVENTIONS[key]:
        if hasattr(obj, name):
            return name, getattr(obj, name)
    return None


@dataclass
class ModelHandle:
    """Everything the harness discovered about one model object."""

    spec: str
    obj: Any
    build: Callable[[], Any] | None = None
    run: Callable[[], Any] | None = None
    results: Callable[[], Any] | None = None
    attach: Callable[..., Any] | None = None
    gurobi_model: Any = None
    #: The attribute name the Gurobi model *will* live under. Recorded even
    #: when the value is still None, because a model builds its solver object
    #: in build() — discovery runs before that.
    gurobi_model_attr: str | None = None
    model_callback: Callable[..., Any] | None = None
    results_table: str | None = None
    #: ``(x, y)`` column names for LTTB preview downsampling, if the model's
    #: results are time-series shaped. Absent = even sampling.
    preview_axes: tuple[str, str] | None = None
    found: dict[str, str] = None  # type: ignore[assignment]

    def refresh(self) -> None:
        """Re-read the attributes a model only populates during build().

        A Gurobi model has no ``grb_model`` until ``build()`` has run, so the
        harness looks again once it has.
        """
        if self.gurobi_model_attr is not None:
            self.gurobi_model = getattr(self.obj, self.gurobi_model_attr, None)
        hit = _find(self.obj, "model_callback")
        if hit is not None and callable(hit[1]):
            self.found["model_callback"] = hit[0]
            self.model_callback = hit[1]

    def describe(self) -> str:
        bits = ", ".join(f"{k}={v}" for k, v in sorted((self.found or {}).items()))
        return f"{self.spec} ({bits or 'nothing discovered'})"

    def wire(self, emit: Callable[..., None], should_cancel: Callable[[], bool]) -> None:
        """Hand the model its two capabilities: emit, and a cancel check.

        Prefers an ``attach(emit, should_cancel)`` method if the model has
        one; otherwise sets the attributes directly. Either way the model
        never learns what is on the other end of ``emit``.
        """
        if self.attach is not None:
            self.attach(emit=emit, should_cancel=should_cancel)
            return
        self.obj.emit = emit
        self.obj.should_cancel = should_cancel


def load_model(spec: str, config: dict[str, Any] | None = None) -> ModelHandle:
    """Import ``spec`` and discover the model object it produces.

    ``spec`` is ``"models.scenario"`` (a factory is looked for by convention)
    or ``"models.scenario:build_model"`` (an explicit attribute).
    """
    config = dict(config or {})
    module_name, _, attr = spec.partition(":")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ModelLoadError(
            f"could not import model module {module_name!r}: {exc}\n"
            f"  spec given: {spec!r}\n"
            f"  expected an importable package under models/, e.g. 'models.scenario'"
        ) from exc

    factory = _resolve_factory(module, module_name, attr)
    obj = _call_factory(factory, config, module_name)
    return describe_object(obj, spec)


def _resolve_factory(module: Any, module_name: str, attr: str) -> Any:
    if attr:
        if not hasattr(module, attr):
            raise ModelLoadError(
                f"{module_name} has no attribute {attr!r}\n"
                f"  available: {', '.join(sorted(n for n in dir(module) if not n.startswith('_')))}"
            )
        return getattr(module, attr)

    for name in CONVENTIONS["factory"]:
        if hasattr(module, name):
            return getattr(module, name)

    raise ModelLoadError(
        f"{module_name} exposes no model factory.\n"
        f"  tried, in order: {', '.join(CONVENTIONS['factory'])}\n"
        f"  a model package needs one module-level callable taking an optional\n"
        f"  config dict and returning the model object (see models/README.md)"
    )


def _call_factory(factory: Any, config: dict[str, Any], module_name: str) -> Any:
    if not callable(factory):
        # Already an object rather than a factory — legitimate, if unusual.
        return factory
    try:
        sig = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory()
    if len(sig.parameters) == 0:
        if config:
            raise ModelLoadError(
                f"{module_name}'s factory takes no arguments but DBX_MODEL_CONFIG "
                f"supplied {sorted(config)} — the config would be silently ignored"
            )
        return factory()
    return factory(config)


def describe_object(obj: Any, spec: str = "<object>") -> ModelHandle:
    """Discover the duck-typed surface of an already-constructed object.

    Split out from ``load_model`` so a model's own tests can check "does the
    harness see what I think it sees" without importing by string.
    """
    found: dict[str, str] = {}
    handle = ModelHandle(spec=spec, obj=obj, found=found)

    for key in ("attach", "build", "run", "results", "model_callback"):
        hit = _find(obj, key)
        if hit is not None:
            name, value = hit
            if callable(value):
                found[key] = name
                setattr(handle, key, value)

    # Presence of the attribute is the capability signal, not its current
    # value: a model constructs its solver object in build(), which has not
    # run yet at discovery time.
    for name in CONVENTIONS["gurobi_model"]:
        if hasattr(obj, name):
            found["gurobi_model"] = name
            handle.gurobi_model_attr = name
            handle.gurobi_model = getattr(obj, name)
            break

    table = _find(obj, "results_table")
    if table is not None and isinstance(table[1], str) and table[1]:
        found["results_table"] = table[0]
        handle.results_table = table[1]

    axes = _find(obj, "preview_axes")
    if axes is not None and axes[1]:
        value = axes[1]
        if isinstance(value, (tuple, list)) and len(value) == 2:
            found["preview_axes"] = axes[0]
            handle.preview_axes = (str(value[0]), str(value[1]))

    if handle.run is None and handle.gurobi_model_attr is None:
        raise ModelLoadError(
            f"{spec} exposes nothing the harness can run.\n"
            f"  looked for a callable named one of: {', '.join(CONVENTIONS['run'])}\n"
            f"  ...or a gurobipy model attribute named one of: "
            f"{', '.join(CONVENTIONS['gurobi_model'])}\n"
            f"  object was {type(obj).__name__} with public attributes: "
            f"{', '.join(sorted(n for n in dir(obj) if not n.startswith('_'))) or '(none)'}\n"
            f"  see models/README.md for the contract"
        )

    return handle
