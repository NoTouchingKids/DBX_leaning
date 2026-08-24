/**
 * Design tokens for the two Recharts cards.
 *
 * Recharts wants colour strings for `stroke` and `fill`, not class names, so
 * these are the CSS custom properties from `src/index.css` referenced by name
 * rather than by value. That matters: the dark palette re-points `--c-*` at
 * runtime from one place, and a literal hex here would survive the switch and
 * be the one thing on the page still wearing the light theme.
 */

export const CHART_COLORS = {
  /** `primary_metric` / `best_fare`. The kept solution — green reads as the
   *  one that is banked. */
  best: "var(--c-good)",
  /** The current walk. Cool and neutral: it is allowed to go down. */
  current: "var(--c-info)",
  /** Over-capacity markers. Deliberately the plain border colour — no `bad`,
   *  no `warn`. Leaving the feasible region is the algorithm working. */
  overShift: "var(--c-edge)",
  overShiftFill: "var(--c-paper)",
  /** Temperature: the warm one, matching the signature lattice's hot end. */
  temperature: "var(--c-accent)",
  acceptance: "var(--c-dim)",
  grid: "var(--c-line)",
  axis: "var(--c-faint)",
  surface: "var(--c-raised)",
  border: "var(--c-edge)",
  ink: "var(--c-ink)",
} as const;

export const AXIS_TICK = { fill: "var(--c-faint)", fontSize: 10 } as const;

export const TOOLTIP_STYLE = {
  background: CHART_COLORS.surface,
  border: `1px solid ${CHART_COLORS.border}`,
  borderRadius: "8px",
  fontSize: "0.72rem",
  color: CHART_COLORS.ink,
} as const;

export const LEGEND_STYLE = { fontSize: "0.68rem", paddingTop: "4px" } as const;

export const CHART_HEIGHT = 224;

const FARE_FMT = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const COMPACT_FMT = new Intl.NumberFormat(undefined, {
  notation: "compact",
  maximumFractionDigits: 1,
});
const SIG_FMT = new Intl.NumberFormat(undefined, { maximumSignificantDigits: 3 });

export function formatFare(value: number): string {
  return FARE_FMT.format(value);
}

/** Iteration counts run to tens of thousands; full digits on every tick eats
 *  the plot area on a half-width card. */
export function formatIteration(value: number): string {
  return COMPACT_FMT.format(value);
}

export function formatTemperatureTick(value: number): string {
  return SIG_FMT.format(value);
}

export function formatPercentTick(value: number): string {
  return `${Math.round(value * 100)}%`;
}
