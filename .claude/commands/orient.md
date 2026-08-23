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
3. **Whether the two ingress probes have run yet.** Check for evidence in
   the repo (a results file, a note in `docs/`, anything committed by
   `/spike-ws` or `/spike-sse`) — don't assume. If neither has run, say so
   plainly: most of this platform's build-out is blocked until both do (see
   `CLAUDE.md`, "How to work in this repo"). The exception is the five
   `models/*` tracks and `shared/` itself, which don't depend on the probe
   results at all — if you're on one of those tracks, note that you can
   proceed regardless.
4. **Any constraint from `docs/free-edition-constraints.md` that's directly
   relevant to the track you're about to work on** — e.g. if you're building
   a model, the 2000-variable Gurobi cap or the concurrent-job-task ceiling;
   if you're building `job/`, the delta-rs/Spark fallback and flush cadence;
   if you're building `app/`, the SSE/warehouse-cost interaction.

Keep this to a few short paragraphs, not an essay — the goal is a quick,
checkable statement of understanding before any code gets written, not a
restatement of every doc. If anything in `CLAUDE.md` or `docs/` seems to
conflict with what you're being asked to do in this session, say so and ask
before proceeding, rather than silently picking one interpretation.
