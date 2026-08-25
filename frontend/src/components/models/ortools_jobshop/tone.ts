/**
 * The tone vocabulary this view uses.
 *
 * A near-copy of `gurobi_shared/tones.ts`, and deliberately not an import of
 * it. That file says in its own header that it is "the tone vocabulary the two
 * Gurobi signatures share", and `gurobi_shared/mipSeries.ts` explains the rule
 * it lives by: a sibling reaching into a sibling's directory reads as an
 * accident, and the next person editing the Gurobi views has no way to know a
 * third model depends on them. This model exists precisely to CONTRAST with
 * those two, so coupling its styling to their shared module would be the wrong
 * dependency to create for the sake of forty lines of literal strings.
 *
 * Every class here is a literal string for the same reason theirs are:
 * Tailwind scans source text, so a composed name (`` `bg-${tone}` ``) produces
 * a class that exists in the markup and not in the stylesheet — and since the
 * dark palette re-points these tokens at runtime, that failure would only be
 * visible in one colour scheme.
 */

export type Tone = "info" | "good" | "warn" | "bad" | "idle" | "accent";

export const TONE_TEXT: Record<Tone, string> = {
  info: "text-info",
  good: "text-good",
  warn: "text-warn",
  bad: "text-bad",
  idle: "text-idle",
  accent: "text-accent",
};

export const TONE_DOT: Record<Tone, string> = {
  info: "bg-info",
  good: "bg-good",
  warn: "bg-warn",
  bad: "bg-bad",
  idle: "bg-idle",
  accent: "bg-accent",
};

export const TONE_FILL: Record<Tone, string> = {
  info: "bg-info border-info",
  good: "bg-good border-good",
  warn: "bg-warn border-warn",
  bad: "bg-bad border-bad",
  idle: "bg-idle border-idle",
  accent: "bg-accent border-accent",
};

export const TONE_SOFT: Record<Tone, string> = {
  info: "bg-info-soft border-info",
  good: "bg-good-soft border-good",
  warn: "bg-warn-soft border-warn",
  bad: "bg-bad-soft border-bad",
  idle: "bg-idle-soft border-idle",
  accent: "bg-accent-soft border-accent",
};

/** What the signature says it is doing, per lifecycle state. `null` is "no run
 *  selected" and is a real state the page can be in. */
export interface StateCopy {
  tone: Tone;
  title: string;
  detail: string;
  /** Ring the dot instead of filling it: pre-solve states have not started
   *  doing the thing a filled dot would claim. */
  hollow?: boolean;
}
