/**
 * The landing page.
 *
 * Two jobs that pull against each other. It has to say what this platform is
 * to someone who has never seen it — hence the hero — while staying the honest
 * status page it already was: health, the reasons a deploy is running
 * degraded, the concurrency ceiling that will refuse the sixth trigger, and
 * the cross-reference between two lists that can legitimately disagree.
 * `MODEL_SPECS` is what this client knows how to build a form for, hand-derived
 * from `job/models/<name>/model.py`; `GET /api/models` is what has a Databricks
 * job configured behind it. A model in the first and not the second exists but
 * cannot be run, and that difference is rendered rather than hidden — hiding it
 * turns a configuration gap into a mysterious 404 at trigger time.
 *
 * `PageHead` is deliberately not used here. It renders a 1.55rem `<h1>` sized
 * for a document header and takes no size prop, and a hero headline at that
 * size reads as a page that forgot it had a hero. The hero carries the page's
 * one `<h1>` itself — `NotFound.tsx` writes its own for the same reason — and
 * the lead paragraph is the text `PageHead` used to carry, unchanged in what it
 * claims.
 */

import { motion } from "motion/react";
import { Link } from "react-router";

import HeroFallback from "@/components/landing/HeroFallback";
import { SceneBoundary } from "@/components/landing/SceneBoundary";
import { DURATION, EASE, staggerFor } from "@/components/models/motion";
import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import { Callout } from "@/components/ui/Callout";
import { Card } from "@/components/ui/Card";
import { DataList, DataRow } from "@/components/ui/DataList";
import { useHealthz, useModels, useWhoami } from "@/hooks/useApi";
import { initials } from "@/lib/format";
import { MODEL_SPECS } from "@/lib/models";

/**
 * The scrims that sit between the canvas and the copy.
 *
 * Inline `linear-gradient`s rather than gradient utilities, for the same reason
 * three of the model signatures build their gradients inline: these name
 * palette tokens directly, and `index.css` declares the theme with
 * `@theme inline`, so `--c-raised` is the name guaranteed to exist at runtime.
 * `color-mix` against a token is already how this app gets a partial alpha out
 * of one — see `.live-dot` in `index.css`.
 *
 * Two of them because the copy sits in a different place at different widths:
 * beside the scene on a wide screen, over the middle of it on a phone. The flat
 * one is deliberately heavy. The scene is decorative and the headline is not,
 * so where they compete for the same pixels the headline wins — and the hero
 * must not assume the canvas behind it is dark, because it does not own it.
 */
const SCRIM_DIRECTIONAL =
  "linear-gradient(100deg," +
  " var(--c-raised) 0%," +
  " color-mix(in srgb, var(--c-raised) 92%, transparent) 38%," +
  " color-mix(in srgb, var(--c-raised) 55%, transparent) 70%," +
  " color-mix(in srgb, var(--c-raised) 22%, transparent) 100%)";

const SCRIM_FLAT = "color-mix(in srgb, var(--c-raised) 74%, transparent)";

/**
 * The two hero calls to action.
 *
 * `<Link>`, not `<Button>`: these navigate, and a hero CTA is the one control
 * on this page someone will middle-click or copy the address of. `Button`
 * renders a `<button>` and takes no `as` prop, and nesting one inside a Link is
 * invalid interactive content that breaks keyboard activation. So they borrow
 * its shape instead. The cost is that a change to `Button`'s variants has to be
 * repeated here — cheap, and cosmetic. The alternative was an anchor that is
 * not an anchor.
 *
 * One deliberate divergence from it: the label is `accent-soft`, not white.
 * `Button`'s comment reasons about the light palette only — `--c-accent` is a
 * dark blue there, so white on it is legible. It is a LIGHT blue in dark mode,
 * where white on it lands near 2:1 and this label is 0.82rem semibold. Both
 * fills invert with the theme and `--c-accent-soft` inverts with them, so one
 * class clears AA on `accent` and on the `accent-ink` hover in both. Every
 * primary `Button` in the app has the same problem; that file is not this
 * one's to fix.
 */
const CTA_BASE =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border px-4 py-2.5 " +
  "text-[0.82rem] leading-none font-semibold no-underline transition-colors " +
  "duration-150 motion-reduce:transition-none";

const CTA_PRIMARY =
  `${CTA_BASE} border-accent bg-accent text-accent-soft shadow-[var(--shadow-card)] ` +
  "hover:border-accent-ink hover:bg-accent-ink";

const CTA_SECONDARY =
  `${CTA_BASE} border-edge bg-raised text-dim ` +
  "hover:border-accent hover:bg-accent-soft hover:text-accent";

/**
 * One line per model, saying what KIND of computation it is.
 *
 * Copy, not data. `MODEL_SPECS` has no description field, this page does not
 * own it, and a gallery of eleven two-word labels is a list pretending to be a
 * gallery. Keyed by model name and looked up with a fallback, so a model added
 * to the registry gets a tile with no blurb rather than an `undefined` — it
 * degrades to exactly the old behaviour. The facts come from the model list in
 * `CLAUDE.md` and each model's own config surface; that is where to re-derive
 * them when one changes.
 */
const BLURBS: Record<string, string> = {
  gurobi_scheduling: "Mixed-integer programme. Staff onto shifts against an hourly demand curve.",
  gurobi_routing: "Mixed-integer programme. One tour over a set of stops, formulated edge by edge.",
  ortools_jobshop: "CP-SAT. Operations onto machines under a wall-clock limit, with no licence cap.",
  forecasting: "Trains on lagged history, then forecasts a horizon ahead of it.",
  mcmc: "Parallel chains sampling a posterior — burn-in, then draws.",
  scenario: "A grid of demand, capacity and unit-cost multipliers. One outcome per cell.",
  streaming_results: "A rolling backtest: windows walked forward, results emitted as they land.",
  annealing: "Simulated annealing over a knapsack of trips, cooling toward an incumbent.",
  bayesian_ab: "A conjugate Bayesian comparison of two slices of the same data.",
  neural_net: "A small torch classifier, an epoch at a time, against a held-out split.",
  panel_fit: "One curve fit per group, across a panel of entities and periods.",
};

/**
 * The hero copy enters as one staggered group.
 *
 * The landing page is not a run and has no lifecycle phase, so it borrows only
 * the shared durations and curves from `motion.ts` rather than inventing a
 * timing of its own — an entrance that moves at a different speed from the rest
 * of the app is the thing that vocabulary exists to prevent. Reduced motion
 * removes the transition and nothing else: the copy is simply already there.
 */
function riseIn(index: number, reduced: boolean) {
  return {
    initial: reduced ? false : { opacity: 0, y: 12 },
    animate: { opacity: 1, y: 0 },
    transition: reduced
      ? { duration: 0 }
      : { duration: DURATION.slow, ease: EASE.decelerate, delay: index * staggerFor(4) },
  };
}

export function HomePage() {
  const models = useModels();
  const health = useHealthz();
  const whoami = useWhoami();
  const reduced = usePrefersReducedMotion();

  const triggerable = new Set(models.data?.models.map((m) => m.name) ?? []);
  const degraded = Object.entries(health.data?.degraded ?? {});
  const runnableCount = MODEL_SPECS.filter((spec) => triggerable.has(spec.name)).length;
  const liveJobs = health.data?.live_jobs ?? 0;

  // Prefer a model that actually has a job behind it: sending someone from the
  // hero straight to a model that cannot be triggered is the worst possible
  // first click on this page. Falls back to the first known model before
  // `/api/models` has answered, so the CTA is never missing while the page
  // loads — and is undefined only if the registry itself is empty.
  const featured = MODEL_SPECS.find((spec) => triggerable.has(spec.name)) ?? MODEL_SPECS[0];

  return (
    <>
      {/*
        Bleeds to the edges of the content column by cancelling `AppShell`'s
        own padding. The max-width lives in `AppShell`, which this page does
        not own, so this is as full-bleed as the hero can honestly get: edge
        to edge below ~1230px, a centred band above it. `px-6` puts the copy
        back in line with the cards underneath.
      */}
      <header className="relative -mx-6 -mt-8 mb-6 flex min-h-[22rem] items-center overflow-hidden border-b border-line bg-raised px-6 py-14 sm:min-h-[26rem] sm:py-20 lg:min-h-[29rem] lg:py-24">
        {/* Absolutely positioned by the scene and the fallback alike, so this
            section is their containing block and nothing here sizes them. */}
        <SceneBoundary fallback={<HeroFallback />} />

        <div aria-hidden className="absolute inset-0" style={{ background: SCRIM_DIRECTIONAL }} />
        <div
          aria-hidden
          className="absolute inset-0 lg:hidden"
          style={{ background: SCRIM_FLAT }}
        />

        {/* Real text in the DOM, never in the canvas: selectable, findable by
            the browser's own search, and read aloud in order. */}
        <div className="relative max-w-[38rem]">
          <motion.p
            {...riseIn(0, reduced)}
            className="m-0 mb-2 text-[0.68rem] font-semibold tracking-[0.1em] text-accent uppercase"
          >
            Modelling platform · Databricks Free Edition
          </motion.p>

          <motion.h1
            {...riseIn(1, reduced)}
            className="m-0 mb-3 text-[2.1rem] leading-[1.08] tracking-tight text-balance sm:text-[2.55rem]"
          >
            Trigger a model, then watch it run.
          </motion.h1>

          <motion.p
            {...riseIn(2, reduced)}
            className="m-0 max-w-[48ch] text-[0.95rem] leading-relaxed text-dim"
          >
            {MODEL_SPECS.length} analytical models, each running as its own
            Databricks Job and streaming progress back here over SSE. The job is
            autonomous; this app is an optional observer of it — closing the tab
            does not stop a run, and a run that started while the app was down is
            still fully recorded.
          </motion.p>

          <motion.div {...riseIn(3, reduced)} className="mt-6 flex flex-wrap items-center gap-3">
            <Link to="/runs" className={CTA_PRIMARY}>
              See what is running
            </Link>
            {featured !== undefined && (
              <Link to={`/models/${featured.name}`} className={CTA_SECONDARY}>
                Open {featured.label}
              </Link>
            )}
          </motion.div>

          {/* Only when it is non-zero: "0 jobs streaming" is a worse thing to
              say than nothing at all, and this is the one fact on the page that
              makes the primary CTA worth clicking right now. Accent, not
              `good` — `index.css` spends the state colours on run state, and a
              green dot here would read as SUCCEEDED. */}
          {liveJobs > 0 && (
            <p className="mt-4 flex items-center gap-2 text-[0.75rem] text-dim">
              <span
                aria-hidden
                className="live-dot inline-block h-2 w-2 flex-none rounded-full bg-accent text-accent"
              />
              {liveJobs} {liveJobs === 1 ? "job is" : "jobs are"} streaming into this
              app right now.
            </p>
          )}
        </div>
      </header>

      <div className="mb-5 grid gap-4 sm:grid-cols-2">
        <Card title="This app">
          <DataList>
            <DataRow label="Signed in as">{whoami.data?.email ?? "—"}</DataRow>
            <DataRow label="Health">{health.data?.status ?? "…"}</DataRow>
            <DataRow label="Live job sockets">{health.data?.live_jobs ?? "—"}</DataRow>
            <DataRow label="Protocol schema">
              v{health.data?.protocol_schema_version ?? "—"}
            </DataRow>
          </DataList>
          {degraded.length > 0 && (
            /* A callout, not a bulleted list in warning-coloured text. These
               are the reasons a deploy is running in a reduced mode, and a
               user needs to see them as one thing they can act on rather than
               as red prose trailing off the bottom of a panel. */
            <div className="mt-4">
              <Callout tone="warn" title={`Running degraded (${degraded.length})`}>
                {degraded.map(([service, reason]) => (
                  <div key={service} className="mt-1 first:mt-0">
                    <code className="font-semibold">{service}</code> — {reason}
                  </div>
                ))}
              </Callout>
            </div>
          )}
          <p className="mt-3 text-[0.7rem] leading-relaxed text-faint">
            Identity here is cosmetic — it comes from the platform proxy and is
            not an authorization boundary. Access is decided by Unity Catalog
            grants, not by this page.
          </p>
        </Card>

        <Card title="Concurrency">
          <p className="text-[0.8rem] leading-relaxed text-dim">
            Free Edition allows <strong>5 concurrent job tasks per account</strong>,
            across every model combined. The sixth trigger is refused with a 429
            whose body names the current count — that is the error you are most
            likely to see, and it is not a bug.
          </p>
          <Link
            to="/runs"
            className="mt-3 inline-block text-[0.78rem] font-semibold text-accent"
          >
            See what is running →
          </Link>
        </Card>
      </div>

      <Card
        title="Models"
        hint={
          models.data
            ? `${runnableCount} of ${MODEL_SPECS.length} have a job configured`
            : `${MODEL_SPECS.length} known to this client`
        }
      >
        <p className="mb-4 max-w-[70ch] text-[0.8rem] leading-relaxed text-dim">
          One job per model, each with its own serverless environment and its own
          dependency list — the MCMC job does not carry gurobipy. A model listed
          here that has no job configured exists in the repo but cannot be
          triggered; it is shown rather than hidden.
        </p>

        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {MODEL_SPECS.map((spec) => {
            const runnable = models.data ? triggerable.has(spec.name) : undefined;
            const blurb = BLURBS[spec.name];
            return (
              <li key={spec.name}>
                <Link
                  to={`/models/${spec.name}`}
                  className={
                    `group flex h-full flex-col gap-2.5 rounded-xl border border-edge bg-paper ` +
                    `p-4 no-underline transition-colors duration-150 hover:border-accent ` +
                    `hover:bg-accent-soft motion-reduce:transition-none`
                  }
                >
                  <div className="flex items-center gap-2.5">
                    {/* Initials, derived from the label, exactly as the sidebar
                        does it — a per-model glyph table would need a new entry
                        every time `MODEL_SPECS` grows and would silently not
                        get one. */}
                    <span
                      aria-hidden
                      className={
                        `flex h-7 w-7 flex-none items-center justify-center rounded-md border ` +
                        `border-line bg-raised text-[0.6rem] font-bold text-faint ` +
                        `group-hover:border-accent/40 group-hover:text-accent`
                      }
                    >
                      {initials(spec.label)}
                    </span>
                    <span className="min-w-0">
                      <span className="block truncate text-[0.86rem] font-semibold text-ink">
                        {spec.label}
                      </span>
                      <code className="block truncate text-[0.66rem] text-faint">
                        {spec.name}
                      </code>
                    </span>
                  </div>

                  {blurb !== undefined && (
                    <p className="m-0 text-[0.75rem] leading-relaxed text-dim">{blurb}</p>
                  )}

                  {/* `mt-auto` so the footers line up across a row whose tiles
                      have blurbs of different lengths. */}
                  <div className="mt-auto flex items-baseline justify-between gap-2 text-[0.66rem]">
                    <span className={runnable === false ? "font-semibold text-warn" : "text-faint"}>
                      {runnable === undefined
                        ? ""
                        : runnable
                          ? `${spec.fields.length} fields`
                          : "no job configured"}
                    </span>
                    <span
                      aria-hidden
                      className={
                        `text-faint transition-transform duration-150 ` +
                        `group-hover:translate-x-0.5 group-hover:text-accent ` +
                        `motion-reduce:transition-none`
                      }
                    >
                      →
                    </span>
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      </Card>
    </>
  );
}
