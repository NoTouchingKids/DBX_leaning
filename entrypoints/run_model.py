"""Databricks Job entrypoint. One of these runs every model.

Why this file exists at all: with workspace-files sync the job runs a *file*,
not an installed package, so something has to put the synced repo root on
``sys.path`` before ``job.main`` can be imported. And serverless tasks have no
``spark_env_vars``, so job parameters arrive as command-line arguments while
``job/config.py`` reads the environment — this bridges the two.

Finding that root is the fiddly part. A serverless ``spark_python_task`` does
not run this file as a script: it reads it and ``exec``s it inside an
ipykernel, so ``__file__`` is undefined and the obvious one-liner raises
``NameError`` before anything else happens. See :func:`repo_root`.

Deliberately thin. Anything with logic in it belongs in ``job/``, where it can
be tested without a workspace.

    run_model.py DBX_RUN_ID=abc DBX_MODEL=job.models.scenario DBX_APP_URL=

An argument with an empty value is dropped rather than exported as an empty
string, so an unset ``DBX_APP_URL`` means "no app" rather than "an app at ''".
"""

from __future__ import annotations

import os
import pathlib
import sys

#: Set this to skip the search below and name the synced root outright.
ROOT_OVERRIDE = "DBX_REPO_ROOT"

#: What proves a candidate is the repo root rather than some other directory.
#: `job/main.py` is what the next line of `main()` imports, so checking for it
#: is checking the thing we are actually about to do.
_MARKER = ("job", "main.py")


def _candidates() -> list[tuple[str, pathlib.Path]]:
    """Every way we know of to find the file this module was loaded from.

    Ordered best-first, and each labelled so a failure can say what it tried.
    """
    found: list[tuple[str, pathlib.Path]] = []

    override = (os.environ.get(ROOT_OVERRIDE) or "").strip()
    if override:
        found.append((ROOT_OVERRIDE, pathlib.Path(override)))

    # The normal case: run as a script or imported as a module.
    module_file = globals().get("__file__")
    if module_file:
        found.append(("__file__", pathlib.Path(module_file).parent.parent))

    # A serverless `spark_python_task` does NOT set `__file__`. It reads the
    # file and runs `exec(compile(source, filename, "exec"))` inside an
    # ipykernel, which leaves `__file__` undefined but DOES put the real path
    # in the code object — which is why the traceback could name the file and
    # the line while the module could not name itself:
    #
    #   File /Workspace/Users/.../entrypoints/run_model.py:25
    #   NameError: name '__file__' is not defined
    frame = sys._getframe()
    while frame is not None:
        name = frame.f_code.co_filename
        # `<string>`, `<stdin>`, `<ipython-input-...>` are not paths.
        if name and not name.startswith("<") and name.endswith("run_model.py"):
            found.append(("code object", pathlib.Path(name).parent.parent))
            break
        frame = frame.f_back

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith("run_model.py"):
        found.append(("sys.argv[0]", pathlib.Path(argv0).parent.parent))

    return found


def repo_root() -> pathlib.Path:
    """The synced repo root: ``<root>/entrypoints/run_model.py`` -> ``<root>``.

    Resolved by search rather than from ``__file__``, because ``__file__`` is
    not always there. Each candidate is checked for ``job/main.py`` before it is
    accepted, so a wrong guess fails here — with every path it tried — instead
    of two frames later as an ImportError that says nothing about why.
    """
    tried: list[str] = []
    for source, path in _candidates():
        try:
            resolved = path.resolve()
        except OSError:  # pragma: no cover - a path the filesystem rejects
            tried.append(f"{source}: {path} (unresolvable)")
            continue
        if resolved.joinpath(*_MARKER).exists():
            return resolved
        tried.append(f"{source}: {resolved}")

    raise SystemExit(
        "cannot find the synced repo root — no candidate contained "
        + "/".join(_MARKER)
        + ".\n  tried: "
        + ("; ".join(tried) if tried else "nothing; __file__ is undefined and argv is empty")
        + f"\n  set {ROOT_OVERRIDE} to the directory holding job/ to resolve this."
    )


def parse_settings(args: list[str]) -> dict[str, str]:
    """``KEY=VALUE`` arguments to an environment mapping.

    Rejects anything malformed rather than ignoring it: a typo'd parameter
    that silently does nothing would show up as a run that ignored its own
    configuration, which is a miserable thing to debug from a job log.
    """
    settings: dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            raise SystemExit(
                f"bad argument {arg!r}: expected KEY=VALUE\n"
                f"  usage: run_model.py DBX_RUN_ID=... DBX_MODEL=job.models.scenario ..."
            )
        key, _, value = arg.partition("=")
        key = key.strip()
        if not key:
            raise SystemExit(f"bad argument {arg!r}: empty key")
        if value != "":
            settings[key] = value
    return settings


def main(argv: list[str] | None = None) -> int:
    settings = parse_settings(list(sys.argv[1:] if argv is None else argv))
    os.environ.update(settings)

    # After the update, so a DBX_REPO_ROOT passed as a job parameter counts.
    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    from job.main import main as run  # imported after sys.path is ready

    return run()


if __name__ == "__main__":
    sys.exit(main())
