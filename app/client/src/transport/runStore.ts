/**
 * Per-run client state, split by message type.
 *
 * Not one flat `Message[]`. A run emits thousands of progress points and
 * thousands of log lines; every consumer wants exactly one of those groups,
 * and filtering the flat array on each render is O(n) work per component per
 * frame. Splitting once, in the store, is the same work done once.
 *
 * Deduplicated by `seq`, which is what makes re-subscribing safe. A store
 * outlives its subscription — the client keeps up to twenty idle ones so
 * navigating away and back does not re-read IndexedDB — while the worker
 * tears its connection down on the last unsubscribe and rebuilds it, hydrate
 * and all, on the next one. So a store WILL be handed history it already
 * holds. Without dedupe, navigating to a run, away, and back appends a second
 * copy of every log line, progress point and result: the log pane repeats
 * itself and, worse, every chart plots each point twice. `seq` is a single
 * monotonic counter per run shared across all four message types, so it is a
 * sufficient key on its own.
 *
 * Dedupe must be by key rather than "drop anything at or below the highest
 * seq seen": backfill legitimately delivers messages BELOW the high-water
 * mark when it fills an observed gap.
 *
 * Bounded on purpose. The app is open for a whole working day across many
 * runs, so "keep everything" is a slow leak. Logs and progress are capped
 * with oldest-dropped and the drop is *counted*, so the UI can say what it
 * lost instead of quietly showing a partial history. Statuses and results are
 * never dropped: there are few of them and each one is load-bearing.
 */

import {
  isTerminal,
  type LogMessage,
  type Message,
  type ProgressMessage,
  type ResultMessage,
  type RunStatus,
  type StatusMessage,
} from "@/lib/envelope";
import type { ConnectionState } from "./protocol";

export const MAX_LOGS = 5_000;
export const MAX_PROGRESS = 10_000;

export interface Gap {
  from: number;
  to: number;
}

export interface RunSnapshot {
  run_id: string;
  logs: readonly LogMessage[];
  progress: readonly ProgressMessage[];
  statuses: readonly StatusMessage[];
  results: readonly ResultMessage[];
  /** The most recent progress point — what a header renders. */
  latestProgress: ProgressMessage | null;
  /** Latest status seen ON THE STREAM. The authoritative one is the
   *  `run_status` row from the API; this is the live view of it. */
  status: RunStatus | null;
  terminal: boolean;
  lastSeq: number | null;
  connection: ConnectionState;
  consecutiveFailures: number;
  /** Observed holes in the seq stream. May never close — the live path drops
   *  non-client-visible logs and the backfill endpoint filters them too. */
  gaps: readonly Gap[];
  /** IndexedDB history has been delivered. Before this, an empty store means
   *  "not read yet", not "nothing happened". */
  hydrated: boolean;
  droppedLogs: number;
  droppedProgress: number;
}

function emptySnapshot(runId: string): RunSnapshot {
  return {
    run_id: runId,
    logs: [],
    progress: [],
    statuses: [],
    results: [],
    latestProgress: null,
    status: null,
    terminal: false,
    lastSeq: null,
    connection: "idle",
    consecutiveFailures: 0,
    gaps: [],
    hydrated: false,
    droppedLogs: 0,
    droppedProgress: 0,
  };
}

/** Append with a cap, returning the new array and how many fell off. */
function appendCapped<T>(
  existing: readonly T[],
  incoming: readonly T[],
  cap: number,
): [T[], number] {
  const next = existing.concat(incoming);
  if (next.length <= cap) return [next, 0];
  const dropped = next.length - cap;
  return [next.slice(dropped), dropped];
}

export class RunStore {
  private snapshot: RunSnapshot;
  private readonly listeners = new Set<() => void>();
  /**
   * Every seq this store has accepted, including ones since dropped by the
   * caps above — a message trimmed out of the log pane must not reappear at
   * the bottom of it, out of order, on the next hydrate.
   *
   * This grows with the run rather than with what is retained. At roughly 60
   * bytes an entry that is a few MB for the longest MCMC run, against the
   * messages themselves which are far larger; not worth a pruning scheme that
   * could reintroduce the bug it exists to prevent.
   */
  private readonly seen = new Set<number>();

  readonly runId: string;

  constructor(runId: string) {
    this.runId = runId;
    this.snapshot = emptySnapshot(runId);
  }

  getSnapshot = (): RunSnapshot => this.snapshot;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  get listenerCount(): number {
    return this.listeners.size;
  }

  /**
   * Apply a batch. One snapshot replacement per batch, not per message —
   * `useSyncExternalStore` re-renders on identity change, and the worker
   * already coalesced these.
   */
  ingest(messages: readonly Message[], options: { hydrate?: boolean } = {}): void {
    if (messages.length === 0 && !options.hydrate) return;

    const logs: LogMessage[] = [];
    const progress: ProgressMessage[] = [];
    const statuses: StatusMessage[] = [];
    const results: ResultMessage[] = [];
    let accepted = 0;

    for (const msg of messages) {
      if (this.seen.has(msg.seq)) continue;
      this.seen.add(msg.seq);
      accepted += 1;
      switch (msg.type) {
        case "log":
          logs.push(msg);
          break;
        case "progress":
          progress.push(msg);
          break;
        case "status":
          statuses.push(msg);
          break;
        case "result":
          results.push(msg);
          break;
      }
    }

    const prev = this.snapshot;
    if (accepted === 0) {
      // Everything in this batch was already held. Re-emitting an identical
      // snapshot under a new identity would re-render every subscriber for
      // nothing, which on a re-subscribe is the whole run's history.
      if (options.hydrate === true && !prev.hydrated) {
        this.snapshot = { ...prev, hydrated: true };
        this.emit();
      }
      return;
    }
    const [nextLogs, droppedLogs] = appendCapped(prev.logs, logs, MAX_LOGS);
    const [nextProgress, droppedProgress] = appendCapped(
      prev.progress,
      progress,
      MAX_PROGRESS,
    );

    // Statuses can arrive out of order across a hydrate/live boundary; the
    // highest seq is the current one, not the last appended.
    const nextStatuses = prev.statuses.concat(statuses);
    const latestStatus = nextStatuses.reduce<StatusMessage | null>(
      (best, s) => (best === null || s.seq > best.seq ? s : best),
      null,
    );

    const lastSeqInBatch = messages.reduce(
      (max, m) => (m.seq > max ? m.seq : max),
      prev.lastSeq ?? -1,
    );

    this.snapshot = {
      ...prev,
      logs: nextLogs,
      progress: nextProgress,
      statuses: nextStatuses,
      results: prev.results.concat(results),
      latestProgress: nextProgress.at(-1) ?? prev.latestProgress,
      status: latestStatus?.status ?? prev.status,
      terminal: prev.terminal || (latestStatus ? isTerminal(latestStatus.status) : false),
      lastSeq: lastSeqInBatch >= 0 ? lastSeqInBatch : null,
      hydrated: prev.hydrated || options.hydrate === true,
      droppedLogs: prev.droppedLogs + droppedLogs,
      droppedProgress: prev.droppedProgress + droppedProgress,
    };
    this.emit();
  }

  setConnection(state: ConnectionState, consecutiveFailures: number): void {
    if (
      this.snapshot.connection === state &&
      this.snapshot.consecutiveFailures === consecutiveFailures
    ) {
      return;
    }
    this.snapshot = { ...this.snapshot, connection: state, consecutiveFailures };
    this.emit();
  }

  addGap(gap: Gap): void {
    this.snapshot = { ...this.snapshot, gaps: this.snapshot.gaps.concat(gap) };
    this.emit();
  }

  markTerminal(status: RunStatus): void {
    if (this.snapshot.terminal && this.snapshot.status === status) return;
    this.snapshot = { ...this.snapshot, terminal: true, status };
    this.emit();
  }

  private emit(): void {
    for (const listener of this.listeners) listener();
  }
}
