#!/usr/bin/env python3
"""Print the installed gurobipy's bundled restricted-licence expiry.

The licence date only appears in Gurobi's own banner, so this captures the
banner rather than asking an API that does not exist. Record what it prints in
job/models/gurobi_scheduling/LICENCE_EXPIRY.md next to the pin.
"""

from __future__ import annotations

import contextlib
import io
import re
import sys


def main() -> int:
    try:
        import gurobipy as gp
    except ImportError:
        print("gurobipy is not installed (pip install 'dbx-leaning[gurobi]')")
        return 2

    banner = io.StringIO()
    with contextlib.redirect_stdout(banner):
        env = gp.Env(params={"OutputFlag": 1})
        gp.Model("licence-probe", env=env).dispose()
        env.dispose()

    text = banner.getvalue()
    version = ".".join(str(p) for p in gp.gurobi.version())
    match = re.search(r"expires (\d{4}-\d{2}-\d{2})", text)

    print(f"gurobipy {version}")
    if match:
        print(f"bundled restricted licence expires {match.group(1)}")
        return 0
    print("no expiry line in the banner — this may be a full licence:")
    print(text.strip() or "(no output)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
