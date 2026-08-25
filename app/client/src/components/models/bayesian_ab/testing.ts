/**
 * Snapshot builder for this directory's tests.
 *
 * `RunSnapshot` has sixteen fields and only three of them matter to anything
 * in here; spelling the other thirteen out in every test buries the one that
 * is the point of the test. `latestProgress` is derived rather than passed,
 * because `RunStore` derives it — a test that sets it independently could
 * pass against a snapshot the store can never actually produce.
 */

import type { RunSnapshot } from "@/transport/runStore";

export function snapshotOf(partial: Partial<RunSnapshot>): RunSnapshot {
  const progress = partial.progress ?? [];
  return {
    run_id: "run-1",
    logs: [],
    statuses: [],
    results: [],
    status: null,
    terminal: false,
    lastSeq: null,
    connection: "idle",
    consecutiveFailures: 0,
    gaps: [],
    hydrated: true,
    droppedLogs: 0,
    droppedProgress: 0,
    ...partial,
    progress,
    latestProgress: partial.latestProgress ?? progress.at(-1) ?? null,
  };
}
