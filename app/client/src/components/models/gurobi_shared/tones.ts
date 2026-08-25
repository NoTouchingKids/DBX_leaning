/**
 * The tone vocabulary the two Gurobi signatures share.
 *
 * Every class here is a literal string. Tailwind scans source text, so a
 * composed name (`` `bg-${tone}` ``) produces a class that exists in the
 * markup and not in the stylesheet — and because these tokens are re-pointed
 * at runtime by the dark palette, the missing-class failure would only show up
 * in one colour scheme.
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

/** SVG needs a colour value, not a class. Referencing the custom property
 *  rather than the hex is what keeps a stroke following the dark palette. */
export const TONE_VAR: Record<Tone, string> = {
  info: "var(--c-info)",
  good: "var(--c-good)",
  warn: "var(--c-warn)",
  bad: "var(--c-bad)",
  idle: "var(--c-idle)",
  accent: "var(--c-accent)",
};

/** What the signature says it is doing, per lifecycle state. `null` is "no
 *  run selected" and is a real state the page can be in. */
export interface StateCopy {
  tone: Tone;
  title: string;
  detail: string;
  /** Ring the dot instead of filling it: pre-solve states have not started
   *  doing the thing the fill would claim. */
  hollow?: boolean;
}
