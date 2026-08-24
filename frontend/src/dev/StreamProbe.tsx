/**
 * The M1 acceptance page, and the only UI in this milestone.
 *
 * BUILD-PLAN's "done when" for the transport spine is a page that subscribes
 * to a real run id, survives an ingress cut without the reconnect counter
 * tripping, and leaves a contiguous seq range in IndexedDB. That cannot be
 * asserted in jsdom — a fake EventSource cannot reproduce what the Databricks
 * Apps ingress does to a long-lived connection — so it is a page you point at
 * a real run and watch.
 *
 * M2 replaces this as the app's entry. It stays afterwards as a diagnostic:
 * transport tier, connection state, consecutive-failure count and observed
 * gaps, none of which the product UI should be showing a user.
 */

import { useState } from "react";

import { isTerminal } from "@/lib/envelope";
import { getTransport } from "@/transport/client";
import { useRunStream } from "@/transport/useRunStream";

function Field({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex justify-between gap-4 border-b border-neutral-200 py-1 text-sm">
      <span className="text-neutral-500">{label}</span>
      <span className="font-mono">{value}</span>
    </div>
  );
}

export default function StreamProbe() {
  const [input, setInput] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const snap = useRunStream(runId);

  // Contiguity is the acceptance criterion, so it is computed rather than
  // eyeballed. Note it can legitimately never reach "contiguous": the live
  // path drops client_visible=False logs and backfill filters them too.
  const seqs = [
    ...snap.logs, ...snap.progress, ...snap.statuses, ...snap.results,
  ].map((m) => m.seq).sort((a, b) => a - b);
  const holes = seqs.reduce((count, seq, i) => {
    const prev = i === 0 ? undefined : seqs[i - 1];
    return prev !== undefined && seq > prev + 1 ? count + 1 : count;
  }, 0);

  return (
    <main className="mx-auto max-w-2xl p-8 font-sans">
      <h1 className="text-xl font-semibold">Transport probe</h1>
      <p className="mt-1 text-sm text-neutral-500">
        M1 acceptance. Point it at a live run, cut the network, watch the
        counter reset rather than climb.
      </p>

      <form
        className="mt-6 flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          setRunId(input.trim() || null);
        }}
      >
        <input
          className="flex-1 rounded border border-neutral-300 px-3 py-2 font-mono text-sm"
          placeholder="run-0123456789ab"
          value={input}
          onChange={(event) => setInput(event.target.value)}
        />
        <button className="rounded bg-neutral-900 px-4 py-2 text-sm text-white" type="submit">
          Watch
        </button>
      </form>

      <section className="mt-6">
        <Field label="transport tier" value={getTransport().tier ?? "—"} />
        <Field label="connection" value={snap.connection} />
        <Field label="consecutive failures" value={snap.consecutiveFailures} />
        <Field label="hydrated from cache" value={String(snap.hydrated)} />
        <Field label="status" value={snap.status ?? "—"} />
        <Field
          label="terminal"
          value={String(snap.terminal || (snap.status ? isTerminal(snap.status) : false))}
        />
        <Field label="last seq" value={snap.lastSeq ?? "—"} />
        <Field label="messages held" value={seqs.length} />
        <Field label="seq holes" value={holes} />
        <Field label="gaps reported" value={snap.gaps.length} />
        <Field label="logs / progress dropped" value={`${snap.droppedLogs} / ${snap.droppedProgress}`} />
      </section>

      {snap.gaps.length > 0 && (
        <p className="mt-4 rounded bg-amber-50 p-3 text-sm text-amber-900">
          Gaps at {snap.gaps.map((g) => `${g.from}–${g.to}`).join(", ")}. Expected
          when a run emits non-client-visible logs — backfill will not close
          those, because the backfill endpoint filters them too.
        </p>
      )}

      <pre className="mt-6 max-h-96 overflow-auto rounded bg-neutral-900 p-3 text-xs text-neutral-100">
        {snap.logs.slice(-100).map((l) => `[${l.seq}] ${l.level} ${l.phase}: ${l.message}`).join("\n")}
      </pre>
    </main>
  );
}
