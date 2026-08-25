/**
 * The status line above both Gurobi signatures.
 *
 * Shared with `gurobi_routing`: the two models are the same solver reporting
 * the same fields, and a user switching between their pages should not have to
 * re-learn where the state is written.
 */

import { TONE_DOT, TONE_TEXT, type StateCopy } from "./tones";

export function SignatureHeader({
  copy,
  readout,
  animating,
  reducedMotion,
}: {
  copy: StateCopy;
  /** Real numbers off the wire — solution count, nodes, gap. Rendered
   *  monospace and right-aligned so it reads as telemetry, not as caption. */
  readout?: string;
  /** Drives the ambient dot pulse only. It says "the stream is live", which is
   *  true independently of anything the animation below is doing. */
  animating: boolean;
  reducedMotion: boolean;
}) {
  const pulsing = animating && !reducedMotion;
  return (
    <div className="mb-3.5 flex min-h-[2.6em] items-start gap-2.5">
      <span
        className={[
          "mt-1 h-2.5 w-2.5 shrink-0 rounded-full border",
          copy.hollow ? "border-2 bg-transparent" : TONE_DOT[copy.tone],
          copy.hollow ? "" : "border-transparent",
          pulsing ? "animate-pulse" : "",
          copy.hollow ? TONE_TEXT[copy.tone] : "",
        ].join(" ")}
        style={copy.hollow ? { borderColor: "currentColor" } : undefined}
        aria-hidden="true"
      />
      <div className="min-w-0">
        <div className={`text-[0.85rem] font-bold ${TONE_TEXT[copy.tone]}`}>{copy.title}</div>
        <div className="mt-0.5 text-[0.74rem] text-dim">{copy.detail}</div>
      </div>
      {readout !== undefined && (
        <span className="ml-auto self-center font-mono text-[0.7rem] whitespace-pre text-faint">
          {readout}
        </span>
      )}
    </div>
  );
}
