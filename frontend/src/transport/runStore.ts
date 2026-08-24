/**
 * Per-run client state, split by message type.
 *
 * Not one flat `Message[]`. A run emits thousands of progress points and
 * thousands of log lines; every consumer wants exactly one of those groups,
 * and filtering the flat array on each render is O(n) work per component per
 * frame. Splitting once, in the store, is the same work done once.
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

    for (const msg of messages) {
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
