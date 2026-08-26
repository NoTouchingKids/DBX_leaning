/**
 * The still hero: many parallel computations resolving.
 *
 * `SceneBoundary` renders this when there is no WebGL, when the scene crashes,
 * and — the case that decides everything about how it is built — when the user
 * has asked the OS for reduced motion. That user never sees the animated scene
 * at all. This is not a placeholder they are waiting out and not a degraded
 * mode they should be told about; it is what the hero IS for them, for good.
 *
 * So the rules it is built under:
 *
 * **It must never read as an error.** No warning tone, no empty-state framing,
 * no apology. It is a composition.
 *
 * **No state colours.** `index.css` spends `good`/`bad`/`warn` on run state and
 * nothing else, and anyone who has watched a run once has learned that. A green
 * glow behind the headline would say SUCCEEDED. Everything here is the accent
 * and the neutral ramp.
 *
 * **No JS decides a colour.** Both themes come out of `currentColor` and the
 * token classes; there is no palette branch to get wrong and nothing to
 * re-render when the OS scheme flips.
 *
 * **Transparent ground.** `HeroScene` runs its renderer with `alpha: true` and
 * composites over whatever the hero section paints. This does the same, so
 * swapping one for the other cannot change the background under the copy.
 *
 * ## The composition
 *
 * Strands enter loose and wandering from the left, and their wander decays as
 * they cross to a fixed vertical rule where each one lands on a dot, evenly
 * spaced and in order. Divergent search on the left, a settled answer on the
 * right — the same idea the moving scene carries, held as one frame.
 *
 * Eleven prominent strands, because there are eleven models. That is a wink
 * rather than a readout; nothing here is bound to data and it must not start
 * being, or the hero becomes a dashboard that lies when a model is added.
 *
 * Behind them a denser field of hairlines converges too but dissolves just
 * short of the rule. It is what stops eleven curves from reading as a line
 * chart: most of the work a platform like this does is never a headline number.
 */

import { useId } from "react";

const VIEW = { w: 1200, h: 600 } as const;

/** Strands start off-canvas. A composition whose elements all begin on a
 *  visible left margin reads as a chart with a y-axis. */
const X_IN = -60;

/** Where the answer lands. Left of centre-right on purpose: `slice` crops the
 *  sides on a narrow container, and the one thing that must survive the crop
 *  is the moment of resolution. */
const X_RESOLVE = 760;
const X_HAIR_END = X_RESOLVE - 46;

const MID = VIEW.h / 2;
/** Height of the settled stack. Set against the dot rings below — at much less
 *  than this the rings touch and eleven answers read as one chain. */
const BAND = 240;

const PRINCIPALS = 11;
const HAIRLINES = 22;

/** Points per strand. The curves are sums of two sines, so a polyline through
 *  them is indistinguishable from a spline at this stroke weight — and 33
 *  spline paths would put three times the path data in the DOM for a hero. */
const SAMPLES = 72;

const TAU = Math.PI * 2;

/**
 * Deterministic per-strand jitter.
 *
 * Not `Math.random`: this is a fixed composition, and a hero that reshuffles
 * itself on every mount is a hero nobody tuned. The constants are the usual
 * fract-of-a-large-sine hash — arbitrary, and only ever asked for a number
 * between 0 and 1.
 */
function hash(index: number, salt: number): number {
  const x = Math.sin(index * 12.9898 + salt * 78.233) * 43758.5453;
  return x - Math.floor(x);
}

/**
 * The drift toward the answer. Quadratic, not cubic.
 *
 * Cubic was the first try and it converges too early: everything was inside a
 * pixel of its final height by 70% of the crossing, leaving a third of the
 * width as eleven dead-flat parallel rules. That is ruled paper, not a
 * resolution. This holds the approach open until nearly the end.
 */
function easeInOut(p: number): number {
  return p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
}

/** Where strand `index` comes to rest. Shared by the path and the dot drawn on
 *  its end, so the two cannot drift apart. */
function endY(index: number, count: number, spread: number): number {
  const u = count === 1 ? 0.5 : index / (count - 1);
  return MID + (u - 0.5) * spread;
}

interface StrandSpec {
  index: number;
  count: number;
  xEnd: number;
  /** Peak wander at the left edge, in user units. */
  amplitude: number;
  startTop: number;
  startBottom: number;
  endSpread: number;
}

function strandPath(spec: StrandSpec): string {
  const { index, count, xEnd, amplitude, startTop, startBottom, endSpread } = spec;
  const u = count === 1 ? 0.5 : index / (count - 1);

  // Spread evenly, then knocked off the even spacing. A ruler-straight left
  // edge is the tell that turns a field of searches into a bar chart on its side.
  const yStart = startTop + u * (startBottom - startTop) + (hash(index, 1) - 0.5) * 46;
  const yEnd = endY(index, count, endSpread);

  const f1 = TAU * (1.1 + hash(index, 2) * 1.6);
  const f2 = TAU * (2.4 + hash(index, 3) * 1.4);
  const ph1 = hash(index, 4) * TAU;
  const ph2 = hash(index, 5) * TAU;

  let d = "";
  for (let s = 0; s <= SAMPLES; s += 1) {
    const p = s / SAMPLES;
    const x = X_IN + p * (xEnd - X_IN);
    const e = easeInOut(p);
    // Wander dies faster than the drift converges, so a strand stops searching
    // before it stops moving and the arrival reads as settling rather than as
    // being cut off. Linear decay left every strand still twitching at its dot.
    const wander = Math.pow(1 - e, 1.25) * amplitude;
    const y =
      yStart +
      (yEnd - yStart) * e +
      wander * (0.66 * Math.sin(p * f1 + ph1) + 0.34 * Math.sin(p * f2 + ph2));
    d += `${s === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)}`;
  }
  return d;
}

// Built once for the process, not once per mount: the geometry is fixed, and
// the component may be mounted and unmounted as the boundary probes WebGL.
const PRINCIPAL_PATHS: readonly string[] = Array.from({ length: PRINCIPALS }, (_, index) =>
  strandPath({
    index,
    count: PRINCIPALS,
    xEnd: X_RESOLVE,
    amplitude: 34,
    startTop: 30,
    startBottom: 570,
    endSpread: BAND,
  }),
);

const HAIRLINE_PATHS: readonly string[] = Array.from({ length: HAIRLINES }, (_, index) =>
  strandPath({
    index,
    count: HAIRLINES,
    xEnd: X_HAIR_END,
    amplitude: 52,
    // Past the top and bottom edges, so the field is cropped by the frame
    // rather than politely fitting inside it.
    startTop: -40,
    startBottom: 640,
    endSpread: BAND * 1.25,
  }),
);

const DOT_Y: readonly number[] = Array.from({ length: PRINCIPALS }, (_, index) =>
  endY(index, PRINCIPALS, BAND),
);

export default function HeroFallback() {
  // Gradient ids have to be unique in the document, and two instances briefly
  // coexisting — the boundary swapping its Suspense fallback for its error one —
  // would leave the survivor pointing at a def that just unmounted, which paints
  // black. `useId` is stripped to alphanumerics because React's own format
  // (`«r0»`) is legal in an HTML id but not somewhere anyone wants to debug a
  // `url(#…)` reference.
  const uid = useId().replace(/[^a-zA-Z0-9]/g, "");
  const wash = `hero-wash-${uid}`;
  const trace = `hero-trace-${uid}`;
  const hair = `hero-hair-${uid}`;

  return (
    <svg
      // `h-full w-full` as well as the insets: an SVG is a replaced element with
      // an intrinsic ratio, and inset-0 alone lets it size itself by that ratio
      // instead of filling.
      className="absolute inset-0 h-full w-full text-accent"
      viewBox={`0 0 ${VIEW.w} ${VIEW.h}`}
      // `slice`, not `none`: stretching to fit would scale the strokes
      // anisotropically, and a hairline that thickens only where it runs
      // horizontally looks like a rendering fault.
      preserveAspectRatio="xMidYMid slice"
      aria-hidden="true"
    >
      <defs>
        <radialGradient
          id={wash}
          cx={X_RESOLVE / VIEW.w}
          cy={MID / VIEW.h}
          r="0.62"
        >
          <stop offset="0" stopColor="currentColor" stopOpacity="0.16" />
          <stop offset="0.55" stopColor="currentColor" stopOpacity="0.06" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </radialGradient>

        {/* `userSpaceOnUse`, deliberately. The default bounding-box units would
            align each gradient to its own strand's box — a strand that happens
            to wander less would then brighten earlier than its neighbours, and
            the shared sense of crossing toward one answer is exactly what would
            be lost. Anchored to the composition instead. */}
        <linearGradient
          id={trace}
          gradientUnits="userSpaceOnUse"
          x1={X_IN}
          y1="0"
          x2={X_RESOLVE}
          y2="0"
        >
          <stop offset="0" stopColor="currentColor" stopOpacity="0.05" />
          <stop offset="0.42" stopColor="currentColor" stopOpacity="0.2" />
          <stop offset="0.8" stopColor="currentColor" stopOpacity="0.55" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0.92" />
        </linearGradient>

        <linearGradient
          id={hair}
          gradientUnits="userSpaceOnUse"
          x1={X_IN}
          y1="0"
          x2={X_HAIR_END}
          y2="0"
        >
          <stop offset="0" stopColor="currentColor" stopOpacity="0.04" />
          <stop offset="0.6" stopColor="currentColor" stopOpacity="0.15" />
          {/* Out to nothing before the rule: these dissolve, they do not land.
              Ending them at full strength would put 22 bright stub-ends beside
              the eleven dots and destroy the stack. */}
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>

      <rect width={VIEW.w} height={VIEW.h} fill={`url(#${wash})`} />

      <g fill="none" strokeLinecap="round">
        {HAIRLINE_PATHS.map((d, index) => (
          <path key={index} d={d} stroke={`url(#${hair})`} strokeWidth={0.9} />
        ))}
        {PRINCIPAL_PATHS.map((d, index) => (
          <path key={index} d={d} stroke={`url(#${trace})`} strokeWidth={1.7} />
        ))}
      </g>

      {/* The rule the answers are written against — neutral, not accent, so it
          sits under the dots rather than competing with them. */}
      <line
        className="text-edge"
        x1={X_RESOLVE}
        y1={MID - BAND / 2 - 34}
        x2={X_RESOLVE}
        y2={MID + BAND / 2 + 34}
        stroke="currentColor"
        strokeWidth={1}
        opacity={0.75}
      />

      {DOT_Y.map((y, index) => (
        <g key={index}>
          <circle
            cx={X_RESOLVE}
            cy={y}
            r={6.5}
            fill="none"
            stroke="currentColor"
            strokeWidth={1}
            opacity={0.16}
          />
          <circle cx={X_RESOLVE} cy={y} r={3.2} fill="currentColor" opacity={0.95} />
        </g>
      ))}
    </svg>
  );
}
