/**
 * Chart colours, as the design tokens rather than as literals.
 *
 * Recharts wants a colour *value* for `stroke`/`fill`, and it reuses that same
 * value for the legend swatch, the active dot and the tooltip marker — so a
 * Tailwind class on the series alone would leave those three the library's
 * default blue. `var(--c-*)` is the token itself (declared once in
 * `src/index.css`), so this still re-points when the dark palette swaps the
 * palette at runtime. It is a token reference, not a hardcoded colour.
 *
 * Deliberately duplicated in `../forecasting/theme.ts`: each model view owns
 * its own directory and nothing else, and a shared file between the two is
 * how the metric-direction bug gets reintroduced later. Hoist it only when a
 * third model wants it, and only for the parts that carry no polarity.
 */

export const CHART = {
  accent: "var(--c-accent)",
  info: "var(--c-info)",
  good: "var(--c-good)",
  bad: "var(--c-bad)",
  warn: "var(--c-warn)",
  idle: "var(--c-idle)",
  dim: "var(--c-dim)",
  faint: "var(--c-faint)",
  line: "var(--c-line)",
  edge: "var(--c-edge)",
  raised: "var(--c-raised)",
  ink: "var(--c-ink)",
} as const;

export const AXIS_PROPS = {
  stroke: CHART.edge,
  tick: { fill: CHART.dim, fontSize: 10 },
  tickLine: false,
} as const;

export const GRID_PROPS = {
  stroke: CHART.line,
  strokeDasharray: "2 4",
  vertical: false,
} as const;

export const TOOLTIP_PROPS = {
  contentStyle: {
    background: CHART.raised,
    border: `1px solid ${CHART.edge}`,
    borderRadius: 8,
    fontSize: "0.72rem",
    color: CHART.ink,
  },
  labelStyle: { color: CHART.dim, fontSize: "0.68rem" },
  itemStyle: { padding: 0 },
} as const;
