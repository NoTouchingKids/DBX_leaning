"""The model registry, read from pyproject.toml.

`[tool.dbx-leaning.models]` maps each package under `job/models/` to the
`[project.optional-dependencies]` extra carrying its libraries. Both the
requirements exporter and the wheel builder read it here rather than each
keeping a copy — a model registered in one and not the other would deploy
with the wrong dependencies rather than failing.
"""

from __future__ import annotations

import pathlib
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

__all__ = ["REPO_ROOT", "model_extras", "extra_for", "model_names", "UnregisteredModel"]


class UnregisteredModel(KeyError):
    """A job/models/ package with no entry in the registry."""


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def model_extras() -> dict[str, str]:
    registry = _pyproject().get("tool", {}).get("dbx-leaning", {}).get("models", {})
    if not registry:
        raise UnregisteredModel(
            "pyproject.toml has no [tool.dbx-leaning.models] section; "
            "every model needs an entry mapping its job/models/ directory to its extra"
        )
    return dict(registry)


def model_names() -> list[str]:
    return sorted(model_extras())


def extra_for(model: str) -> str:
    try:
        return model_extras()[model]
    except KeyError:
        raise UnregisteredModel(
            f"{model!r} has no entry in [tool.dbx-leaning.models] in pyproject.toml — "
            f"add one (job/models/{model} -> its optional-dependencies extra) before it "
            f"can be packaged or deployed. Registered: {', '.join(model_names())}"
        ) from None


def discovered_packages() -> list[str]:
    """What is actually on disk under job/models/, registry or not."""
    # `job/models`, not `models`: each deployable unit carries what it
    # needs, and the models are the job's payload, not the app's.
    root = REPO_ROOT / "job" / "models"
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and (p / "__init__.py").exists() and not p.name.startswith("_")
    )
