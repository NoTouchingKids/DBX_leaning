Scaffold a new model package under `models/`, for adding a sixth (or later)
model after the initial five. Ask the user for:

1. The model's name (used for the directory name and any registration the
   harness needs — check how `job/` discovers/registers models before
   assuming a mechanism).
2. A one-line description of what it does and what makes its telemetry
   shape distinct from the existing five (if it's not distinct from any of
   them, say so — there may be no reason for a new model rather than
   extending an existing one).

Then:

1. Read `docs/message-envelope-spec.md` and `docs/architecture.md` ("Why
   models are duck-typed") again — don't work from memory, the contract
   matters more here than speed.
2. Look at the existing `.claude/agents/model-*.md` files as the pattern to
   follow for structuring a new model brief: what the model is and why it's
   in the lineup, its duck-typed surface, how it maps its own progress
   concept onto `percent_complete`/`primary_metric`/`payload`, its
   cancellation behaviour, what it writes as results, explicit non-goals,
   and tests to write. Write a new `.claude/agents/model-<name>.md` in that
   shape before writing any actual model code — the brief is what makes
   this addable by a parallel session later, not just by you right now.
3. Scaffold `models/<name>/` with the duck-typed surface the harness
   expects (check `job/`'s actual discovery code for the current
   conventional names — don't assume they match what's written in
   `docs/architecture.md` in the abstract; that doc describes the pattern,
   `job/`'s code is the actual contract once it exists).
4. If this new model needs a **new shared dependency** (not already in
   whatever shared requirements/lockfile the repo has), treat that as its
   own small sequential step — do not add it silently inside what would
   otherwise be a parallelisable, isolated model track. Flag it and, if
   working across multiple worktrees, make sure every other in-progress
   track picks up the change before their own dependency installs run
   stale.
5. Write the standalone tests described in the new agent brief — the model
   must run and be tested with no harness/transport code involved, exactly
   like the original five.
