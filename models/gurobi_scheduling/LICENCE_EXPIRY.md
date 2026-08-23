# Gurobi bundled restricted licence

**Pinned: `gurobipy >=13,<14` in `pyproject.toml`.**

| Verified release | Bundled licence expires |
|---|---|
| `gurobipy` 13.0.2 | **2027-11-29** (verified 2026-08-22, `scripts/check_gurobi_licence.py`) |
| `gurobipy` 11.x | 2025-11-24 (expired) |
| `gurobipy` 10.x | 2024-10-28 (expired) |
| `gurobipy` 9.x | 2023-10-25 (expired) |

The bundled licence **fails hard, not gracefully**, once past its date. Whoever
maintains this after the pin moves needs the new date here without archaeology:

```
python scripts/check_gurobi_licence.py
```

## Why bundled and not WLS

WLS contacts `token.gurobi.com` over the internet on environment creation, and
Free Edition restricts outbound to trusted domains. See
`docs/free-edition-constraints.md`. The cost of that choice is a hard cap:

- **2000 variables / 2000 constraints** (200 if the model has quadratic terms)
- no concurrent-session limit

This model stays linear and is asserted under the cap in its own tests, not by
eyeballing the generator.
