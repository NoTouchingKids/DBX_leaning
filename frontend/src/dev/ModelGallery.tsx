/**
 * The review surface for the per-model views.
 *
 * There is no Databricks workspace to point at, so this page is the only
 * place any of the nine signature animations can be looked at — let alone
 * compared with each other, or checked in the lifecycle states that are hard
 * to catch in real life.
 *
 * It takes its views as a PROP rather than importing a registry. Nine views
 * are being written in parallel in nine directories; a static import list
 * here would mean this file cannot typecheck until the last of them lands,
 * which is the opposite of what a review surface is for. The route wires the
 * registry in.
 */

import { useMemo, useState } from "react";

import type { ModelView } from "@/components/models/contract";
import { Button } from "@/components/ui/Button";
import { Callout } from "@/components/ui/Callout";
import { MODEL_SPECS } from "@/lib/models";
import {
  FIXTURE_NAMES,
  FIXTURE_NOTES,
  hasScript,
  type FixtureName,
} from "./fixtures";
import { isReducedMotionForced, setReducedMotionOverride } from "./reducedMotionOverride";
import { ViewHarness } from "./ViewHarness";

const ALL = "__all__";

/** Fixtures heavy enough that rendering nine views x eight states of them at
 *  once will lock the tab up for seconds. Still reachable — being able to
 *  measure that is the point of the dense fixture — just not by accident. */
const HEAVY: readonly FixtureName[] = ["dense"];

function Toggle({
  active,
  onClick,
  children,
  title,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  title?: string;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={
        `cursor-pointer rounded-md border px-2.5 py-1 text-[0.72rem] font-semibold ` +
        (active
          ? "border-accent bg-accent-soft text-accent-ink"
          : "border-edge bg-raised text-dim hover:border-accent hover:text-accent")
      }
    >
      {children}
    </button>
  );
}

export function ModelGallery({ views }: { views: readonly ModelView[] }) {
  const [fixture, setFixture] = useState<FixtureName>("typical");
  const [model, setModel] = useState<string>(ALL);
  const [showCharts, setShowCharts] = useState(true);
  const [reduced, setReduced] = useState(() => isReducedMotionForced());
  const [forceHeavy, setForceHeavy] = useState(false);

  const shown = useMemo(
    () => (model === ALL ? views : views.filter((v) => v.model === model)),
    [views, model],
  );

  /* Coverage, computed rather than remembered. Three lists that are supposed
     to agree — MODEL_SPECS, the views passed in, and the fixture scripts —
     and nothing else ties them together. */
  const specNames = useMemo(() => new Set(MODEL_SPECS.map((s) => s.name)), []);
  const viewNames = useMemo(() => views.map((v) => v.model), [views]);
  const missing = MODEL_SPECS.filter((s) => !viewNames.includes(s.name)).map((s) => s.name);
  const unknown = viewNames.filter((n) => !specNames.has(n));
  const duplicates = viewNames.filter((n, i) => viewNames.indexOf(n) !== i);
  const unscripted = viewNames.filter((n) => !hasScript(n));

  const heavy = HEAVY.includes(fixture) && shown.length > 1 && !forceHeavy;

  function toggleReducedMotion() {
    const next = !reduced;
    // Patch BEFORE the re-render: a component that already mounted holds the
    // real MediaQueryList and will not see the override. The `key` below then
    // remounts everything so the hook re-reads through the patched one.
    setReducedMotionOverride(next);
    setReduced(next);
  }

  return (
    <main className="mx-auto flex max-w-none flex-col gap-4 p-6">
      <header>
        <h1 className="text-xl font-semibold">Model view gallery</h1>
        <p className="mt-1 max-w-3xl text-[0.8rem] text-dim">
          Every registered <code className="font-mono">ModelView</code>, rendered through all eight
          lifecycle states against synthetic envelope traffic. Nothing here talks to a server; the
          snapshots come from <code className="font-mono">src/dev/fixtures.ts</code> and are
          deterministic, so two loads look identical and a difference means a real change.
        </p>
      </header>

      <section className="flex flex-col gap-3 rounded-[10px] border border-edge bg-raised p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="w-20 text-[0.68rem] font-bold tracking-wide text-faint uppercase">fixture</span>
          {FIXTURE_NAMES.map((name) => (
            <Toggle key={name} active={name === fixture} onClick={() => setFixture(name)} title={FIXTURE_NOTES[name]}>
              {name}
            </Toggle>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span className="w-20 text-[0.68rem] font-bold tracking-wide text-faint uppercase">model</span>
          <Toggle active={model === ALL} onClick={() => setModel(ALL)}>
            all ({views.length})
          </Toggle>
          {views.map((v) => (
            <Toggle key={v.model} active={model === v.model} onClick={() => setModel(v.model)}>
              {v.model}
            </Toggle>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <span className="w-20 text-[0.68rem] font-bold tracking-wide text-faint uppercase">render</span>
          <Toggle active={showCharts} onClick={() => setShowCharts(!showCharts)}>
            charts {showCharts ? "on" : "off"}
          </Toggle>
          <Toggle
            active={reduced}
            onClick={toggleReducedMotion}
            title="Forces usePrefersReducedMotion() only. CSS @media rules are not affected."
          >
            reduced motion {reduced ? "forced" : "off"}
          </Toggle>
        </div>

        <p className="text-[0.72rem] leading-relaxed text-dim">
          <span className="font-bold">{fixture}</span> — {FIXTURE_NOTES[fixture]}
        </p>

        {reduced && (
          <Callout tone="warn" title="Reduced motion is half-simulated, and this is the half it misses">
            The toggle patches <code className="font-mono">window.matchMedia</code>, so{" "}
            <code className="font-mono">usePrefersReducedMotion()</code> — the hook the signatures
            consult — returns true and its listeners fire. It cannot change how the browser
            evaluates CSS: the{" "}
            <code className="font-mono">@media (prefers-reduced-motion: reduce)</code> block in{" "}
            <code className="font-mono">index.css</code>, which stops{" "}
            <code className="font-mono">.bar-indeterminate</code> and{" "}
            <code className="font-mono">.live-dot</code>, is still reading the real OS setting. For
            that half use DevTools &gt; Rendering &gt; Emulate CSS media feature
            prefers-reduced-motion. An animation that keeps moving with this on may be reading the
            hook correctly and animating from CSS.
          </Callout>
        )}
      </section>

      {views.length === 0 && (
        <Callout tone="info" title="No views passed in">
          The gallery renders whatever <code className="font-mono">views</code> it is handed. An
          empty list means the registry has not been wired yet, not that the views are broken.
        </Callout>
      )}

      {unknown.length > 0 && (
        <Callout tone="bad" title="View model names not found in MODEL_SPECS">
          {unknown.join(", ")} — a view&apos;s <code className="font-mono">model</code> must equal the
          name of its entry in <code className="font-mono">models.ts</code>, or the run page will
          never match it to a run.
        </Callout>
      )}

      {duplicates.length > 0 && (
        <Callout tone="bad" title="Duplicate model names in the registry">
          {duplicates.join(", ")} — two views claiming one model; only one of them will ever render.
        </Callout>
      )}

      {unscripted.length > 0 && unknown.length === 0 && (
        <Callout tone="warn" title="No fixture script for these models">
          {unscripted.join(", ")} — they are being shown common-envelope-fields-only traffic with an
          empty payload. Add a script in <code className="font-mono">src/dev/fixtures.ts</code>
          before judging the view.
        </Callout>
      )}

      {missing.length > 0 && (
        <Callout tone="info" title="Models with no view yet">
          {missing.join(", ")} — these fall back to the generic view on the run page.
        </Callout>
      )}

      {heavy ? (
        <Callout
          tone="warn"
          title={`"${fixture}" across ${shown.length} views is ${shown.length * 8} mounts of a few thousand points`}
          actions={
            <>
              <Button onClick={() => setForceHeavy(true)}>Render anyway</Button>
              <Button onClick={() => setModel(views[0]?.model ?? ALL)}>Pick one model instead</Button>
            </>
          }
        >
          That is the fixture doing its job — it exists to make a view that hands every point
          straight to Recharts feel as slow as it is. Measuring one view at a time is the way to
          find out which one; measuring all nine at once mostly locks the tab.
        </Callout>
      ) : (
        // Remounting on the reduced-motion flag is what makes the toggle take
        // effect: usePrefersReducedMotion reads matchMedia at mount.
        <div key={`${reduced}`} className="flex flex-col gap-4">
          {shown.map((view) => (
            <ViewHarness
              key={view.model}
              view={view}
              fixture={fixture}
              showCharts={showCharts}
            />
          ))}
        </div>
      )}
    </main>
  );
}
