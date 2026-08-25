Read `CLAUDE.md` in full, then every file in `docs/`. Do this before touching
any other file or running any other command.

Then, before writing or editing anything, state back in your own words:

1. **What this platform is** — one sentence, plus the three-hop transport
   shape (job→app, app→client, job→UC) and which of those is a fallback vs.
   which is always-on.
2. **What track you're about to work on.** If the current working directory
   is a git worktree whose branch name matches one of the tracks in
   `docs/parallelization-plan.md` (e.g. `feat/model-mcmc`), say which track
   that is and confirm you've read the matching file in `.claude/agents/`.
   If it's ambiguous — main repo, unclear branch, or a track not listed —
   **ask which track you're meant to be working on** rather than guessing.
   Note that a missing brief is not a missing track: there are eleven models
   and only five have their own `.claude/agents/model-*.md`, deliberately —
   `models/README.md` and `/new-model` replaced the per-model brief once the
   contract stopped changing. Read those instead, and do not write a brief to
   fill the gap.
3. **What has actually run against a real workspace, and what has not.**
   Check for evidence in the repo rather than assuming in either direction —
   this is the question sessions get wrong most often, in both directions.
   Both ingress probes have passed (`docs/spike-results.md`, 2026-08-23), so
   nothing is blocked on them; their *timings* are still unmeasured. What has
   never happened is a `databricks bundle deploy`. So "built and tested"
   here means tested offline: no envelope traffic from a deployed run has
   ever been observed, and no DDL file in `uc_ddl/` or `lakebase_ddl/` has
   ever been executed. Say plainly which side of that line the thing you are
   about to touch sits on.
4. **Any constraint from `docs/free-edition-constraints.md` that's directly
   relevant to the track you're about to work on** — e.g. if you're building
   a model, the 2000-variable Gurobi cap or the concurrent-job-task ceiling;
   if you're building `job/`, the flush cadence and the fact that **Spark is
   the only durable write path** (delta-rs is unimplemented and raises rather
   than silently writing a three-part UC name to a local directory — it is
   not a fallback tier to design around); if you're building `app/`, the
   SSE/warehouse-cost interaction and which run store is live.

Keep this to a few short paragraphs, not an essay — the goal is a quick,
checkable statement of understanding before any code gets written, not a
restatement of every doc. If anything in `CLAUDE.md` or `docs/` seems to
conflict with what you're being asked to do in this session, say so and ask
before proceeding, rather than silently picking one interpretation.
