/**
 * Just enough Beta-distribution arithmetic to draw two posteriors.
 *
 * Client-side rather than another round trip, because the payload already
 * carries everything needed: `posterior_alpha` and `posterior_beta` per arm.
 * The model's own docstring makes the point — with those two numbers a view
 * can redraw both densities with nothing else.
 *
 * Everything is in log space. A run with 2,000 trials produces a posterior
 * like Beta(1200, 800), and `x**1199` underflows to 0 in double precision
 * while `Γ(1200)` overflows to Infinity; the ratio they form is perfectly
 * finite. Computing the log density and exponentiating once at the end is the
 * difference between a curve and a chart full of NaN.
 */

/** Lanczos g=7, n=9 — good to ~15 significant figures for x > 0, which is far
 *  more than a 160-point curve needs, and short enough to read. */
const LANCZOS = [
  0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313,
  -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6,
  1.5056327351493116e-7,
];

export function logGamma(x: number): number {
  if (x < 0.5) {
    // Reflection: Γ(x)Γ(1-x) = π / sin(πx). Keeps the series in its accurate
    // range for the small alphas a Jeffreys' prior can produce.
    return Math.log(Math.PI / Math.sin(Math.PI * x)) - logGamma(1 - x);
  }
  const z = x - 1;
  let a = LANCZOS[0] ?? 0;
  const g = 7;
  for (let i = 1; i < LANCZOS.length; i += 1) {
    a += (LANCZOS[i] ?? 0) / (z + i);
  }
  const t = z + g + 0.5;
  return 0.5 * Math.log(2 * Math.PI) + (z + 0.5) * Math.log(t) - t + Math.log(a);
}

export function logBeta(alpha: number, beta: number): number {
  return logGamma(alpha) + logGamma(beta) - logGamma(alpha + beta);
}

/**
 * Beta density at `x`.
 *
 * Zero outside the open unit interval. That is a display convention, not
 * mathematics: for alpha or beta below 1 the density genuinely diverges at
 * the boundary, and plotting Infinity turns the whole chart into one spike.
 */
export function betaPdf(x: number, alpha: number, beta: number): number {
  if (!(x > 0 && x < 1)) return 0;
  if (!(alpha > 0 && beta > 0)) return 0;
  const logPdf =
    (alpha - 1) * Math.log(x) + (beta - 1) * Math.log1p(-x) - logBeta(alpha, beta);
  const value = Math.exp(logPdf);
  return Number.isFinite(value) ? value : 0;
}

export function betaMean(alpha: number, beta: number): number {
  return alpha / (alpha + beta);
}

export function betaSd(alpha: number, beta: number): number {
  const n = alpha + beta;
  return Math.sqrt((alpha * beta) / (n * n * (n + 1)));
}

/**
 * An x-window that contains both posteriors and not much else.
 *
 * Four standard deviations either side of each mean, unioned, clamped to
 * [0,1]. With thousands of trials the two posteriors are needles: plotted on
 * the full unit interval they are two vertical lines at the same apparent
 * place, and the entire point of the chart — whether they overlap — becomes
 * invisible. The floor on the width stops a single near-degenerate arm
 * collapsing the axis to nothing.
 */
export function densityWindow(
  params: ReadonlyArray<{ alpha: number; beta: number }>,
  sigmas = 4,
  minWidth = 0.004,
): [number, number] {
  let low = 1;
  let high = 0;
  for (const { alpha, beta } of params) {
    if (!(alpha > 0 && beta > 0)) continue;
    const mean = betaMean(alpha, beta);
    const sd = betaSd(alpha, beta);
    low = Math.min(low, mean - sigmas * sd);
    high = Math.max(high, mean + sigmas * sd);
  }
  if (!(low < high)) return [0, 1];
  if (high - low < minWidth) {
    const mid = (low + high) / 2;
    low = mid - minWidth / 2;
    high = mid + minWidth / 2;
  }
  return [Math.max(0, low), Math.min(1, high)];
}

export interface DensityRow {
  x: number;
  [series: string]: number;
}

/** Evaluate each named posterior on a shared grid, ready for Recharts. */
export function densityRows(
  series: ReadonlyArray<{ key: string; alpha: number; beta: number }>,
  window: [number, number],
  points = 160,
): DensityRow[] {
  const [low, high] = window;
  const step = (high - low) / Math.max(1, points - 1);
  const rows: DensityRow[] = [];
  for (let i = 0; i < points; i += 1) {
    const x = low + i * step;
    const row: DensityRow = { x };
    for (const s of series) row[s.key] = betaPdf(x, s.alpha, s.beta);
    rows.push(row);
  }
  return rows;
}
