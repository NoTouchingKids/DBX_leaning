/**
 * Non-component pieces of the harness.
 *
 * Split out of `ViewHarness.tsx` only so that file exports components and
 * nothing else — Fast Refresh gives up on a module that mixes the two, and a
 * gallery you have to hard-reload to see a change in is a gallery nobody
 * uses.
 */

import type { UiRunState } from "@/lib/envelope";

/** Null first: "nothing is known yet" is a state a view has to render, and
 *  putting it at the left makes forgetting it obvious. */
export const HARNESS_STATES: readonly (UiRunState | null)[] = [
  null,
  "STARTING",
  "QUEUED",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "INFEASIBLE",
];

export interface HonestyVerdict {
  level: "ok" | "thin" | "missing";
  note: string;
}

/**
 * A crude read of whether a view's honesty note does its job.
 *
 * Crude on purpose — this cannot judge prose, and it is not a gate. It
 * catches the two failure modes that actually occur: the field left empty,
 * and a one-liner that praises the animation without saying which parts of it
 * are made up. A "thin" verdict is a prompt to read the note, not a verdict
 * on it.
 *
 * The keyword lists are deliberately generous in both directions; a false
 * "thin" costs a reader ten seconds, while a false "ok" is exactly the
 * decorative-visual-read-as-data failure the honesty field exists to prevent.
 */
export function auditHonesty(honesty: string | undefined): HonestyVerdict {
  const text = (honesty ?? "").trim();
  if (text.length === 0) {
    return {
      level: "missing",
      note: "No honesty note. The contract calls a view without one incomplete.",
    };
  }
  const lower = text.toLowerCase();
  const namesTheDecoration =
    /decorat|not real|invented|arbitrar|illustrat|cosmetic|synthetic|does not|no per-|placeholder|nothing|only gestur/.test(
      lower,
    );
  const namesTheReal =
    /real|actual|driven by|comes from|tracks|derived from|payload|primary_metric|percent_complete|paced by/.test(
      lower,
    );
  if (text.length < 60 || !namesTheDecoration || !namesTheReal) {
    return {
      level: "thin",
      note: "Does not clearly separate what is real from what is decorative — read it before trusting it.",
    };
  }
  return { level: "ok", note: "Names both the real and the decorative parts." };
}

export const VERDICT_STYLE: Record<HonestyVerdict["level"], string> = {
  ok: "border-good bg-good-soft text-good",
  thin: "border-warn bg-warn-soft text-warn",
  missing: "border-bad bg-bad-soft text-bad",
};
