/**
 * The stream hub: one `EventSource` per watched non-terminal run, shared by
 * every tab, owning parse, gap detection and persistence.
 *
 * This file has no worker globals in it on purpose. `worker.ts` is a
 * thirty-line shell that wires `onconnect` to this class; everything worth
 * getting wrong lives here, where a test can inject a fake EventSource and a
 * fake clock. The alternative — logic inside the worker — is testable only
 * through a real browser, which is how transport bugs survive to production.
 *
 * Three behaviours are load-bearing:
 *
 *  1. **The reconnect counter counts CONSECUTIVE failures and resets on every
 *     success.** Databricks Apps' ingress cuts long-lived connections
 *     periodically. A counter that only ever increments will kill a perfectly
 *     healthy stream a few minutes in, and it will look exactly like the
 *     server dying.
 *
 *  2. **A gap is not necessarily a fault, and may never close.** `seq` is
 *     gap-free at the source, but the live path deliberately drops
 *     `client_visible=False` logs and may drop logs under pressure — and the
 *     backfill endpoint filters `client_visible` too, so backfilling will not
 *     close that kind of hole. Gaps are reported and never acted on
 *     automatically; a client that loops "backfill until contiguous" spins
 *     forever.
 *
 *  3. **A terminal run gets no live channel.** Nothing further will ever
 *     arrive, so an EventSource on it is a connection that exists only to be
 *     cut and retried.
 */

import { isTerminal, type Message, type RunStatus } from "@/lib/envelope";
import { parseFrame } from "./normalize";
import type { ConnectionState, WorkerEvent, WorkerRequest } from "./protocol";
import type { CachedRun } from "./db";

/** The slice of `EventSource` used here — the seam a test replaces. */
export interface EventSourceLike {
  readonly readyState: number;
  addEventListener(type: string, listener: (event: MessageEvent) => void): void;
  close(): void;
}

export const ES_CONNECTING = 0;
export const ES_OPEN = 1;
export const ES_CLOSED = 2;

/** Anything that can receive worker events. A real `MessagePort`, in prod. */
export interface PortLike {
  postMessage(event: WorkerEvent): void;
}

export interface HubPersistence {
  putMessages(messages: readonly Message[]): Promise<void>;
  readMessages(runId: string): Promise<Message[]>;
  putRun(run: CachedRun): Promise<void>;
  getRun(runId: string): Promise<CachedRun | undefined>;
}

export interface HubOptions {
  createEventSource?: (url: string) => EventSourceLike;
  persistence?: HubPersistence;
  streamUrl?: (runId: string) => string;
  /** Coalescing window for live messages. Status and result flush at once
   *  regardless — those are the ones a user is waiting on. */
  flushMs?: number;
  maxConsecutiveFailures?: number;
  /** A port that has not pinged within this is assumed gone. */
  portTimeoutMs?: number;
  now?: () => number;
  onError?: (context: string, error: unknown) => void;
}

const DEFAULTS = {
  flushMs: 60,
  // ~10 cuts in a row with nothing in between. Generous, because tripping
  // this on a healthy stream is far worse than being slow to give up.
  maxConsecutiveFailures: 10,
  portTimeoutMs: 45_000,
} as const;

interface PortEntry {
  /** Live events queue here until this port's hydrate has been posted, so it
   *  never sees a batch before the history it is meant to follow. */
  hydrating: boolean;
  pending: Message[];
}

interface RunConn {
  runId: string;
  ports: Map<PortLike, PortEntry>;
  es: EventSourceLike | null;
  state: ConnectionState;
  consecutiveFailures: number;
  lastSeq: number | null;
  status: RunStatus | null;
  terminal: boolean;
  /** Subscribed before the caller knew whether the run was over. Hydrated,
   *  but holding off on a channel until `run-terminality` says which. */
  pendingTerminality: boolean;
  buffer: Message[];
  flushTimer: ReturnType<typeof setTimeout> | null;
  retryTimer: ReturnType<typeof setTimeout> | null;
}

const NOOP_PERSISTENCE: HubPersistence = {
  async putMessages() {},
  async readMessages() {
    return [];
  },
  async putRun() {},
  async getRun() {
    return undefined;
  },
};

export class StreamHub {
  private readonly runs = new Map<string, RunConn>();
  private readonly portsSeen = new Map<PortLike, number>();

  private readonly createEventSource: (url: string) => EventSourceLike;
  private readonly persistence: HubPersistence;
  private readonly streamUrl: (runId: string) => string;
  private readonly flushMs: number;
  private readonly maxConsecutiveFailures: number;
  private readonly portTimeoutMs: number;
  private readonly now: () => number;
  private readonly onError: (context: string, error: unknown) => void;

  /** Frames that parsed to nothing. Surfaced by `stats()` rather than thrown:
   *  one bad frame must not take the run's stream down with it. */
  private droppedFrames = 0;

  constructor(options: HubOptions = {}) {
    this.createEventSource =
      options.createEventSource ?? ((url) => new EventSource(url) as EventSourceLike);
    this.persistence = options.persistence ?? NOOP_PERSISTENCE;
    this.streamUrl =
      options.streamUrl ?? ((runId) => `/api/runs/${encodeURIComponent(runId)}/stream`);
    this.flushMs = options.flushMs ?? DEFAULTS.flushMs;
    this.maxConsecutiveFailures =
      options.maxConsecutiveFailures ?? DEFAULTS.maxConsecutiveFailures;
    this.portTimeoutMs = options.portTimeoutMs ?? DEFAULTS.portTimeoutMs;
    this.now = options.now ?? (() => Date.now());
    this.onError = options.onError ?? (() => {});
  }

  /* ---------------------------------------------------------------- *
   * Ports
   * ---------------------------------------------------------------- */

  connect(port: PortLike): void {
    this.portsSeen.set(port, this.now());
  }

  handle(port: PortLike, request: WorkerRequest): void {
    this.portsSeen.set(port, this.now());
    switch (request.kind) {
      case "subscribe":
        void this.subscribe(port, request.run_id, request.terminal ?? false);
        return;
      case "unsubscribe":
        this.unsubscribe(port, request.run_id);
        return;
      case "run-terminality":
        this.setTerminality(request.run_id, request.terminal);
        return;
      case "ping":
        return; // the timestamp above was the entire point
      case "bye":
        this.disconnect(port);
        return;
    }
  }

  disconnect(port: PortLike): void {
    this.portsSeen.delete(port);
    for (const runId of [...this.runs.keys()]) this.unsubscribe(port, runId);
  }

  /**
   * Drop ports that have gone quiet. SharedWorker gives no reliable
   * disconnect signal — a closed tab's port simply stops being spoken to —
   * so liveness is inferred, and without this the worker holds an
   * EventSource open for a tab that closed an hour ago.
   */
  sweep(): void {
    const cutoff = this.now() - this.portTimeoutMs;
    for (const [port, seen] of this.portsSeen) {
      if (seen < cutoff) this.disconnect(port);
    }
  }

  /* ---------------------------------------------------------------- *
   * Subscription
   * ---------------------------------------------------------------- */

  private conn(runId: string): RunConn {
    let conn = this.runs.get(runId);
    if (!conn) {
      conn = {
        runId,
        ports: new Map(),
        es: null,
        state: "idle",
        consecutiveFailures: 0,
        lastSeq: null,
        status: null,
        terminal: false,
        pendingTerminality: false,
        buffer: [],
        flushTimer: null,
        retryTimer: null,
      };
      this.runs.set(runId, conn);
    }
    return conn;
  }

  private async subscribe(
    port: PortLike,
    runId: string,
    terminal: boolean | undefined,
  ): Promise<void> {
    const conn = this.conn(runId);
    if (conn.ports.has(port)) return;
    conn.ports.set(port, { hydrating: true, pending: [] });
    if (terminal === true) conn.terminal = true;
    // Undefined is not "false". It means the caller does not know yet, and
    // opening a channel on a guess is how a finished run got one.
    if (terminal === undefined) conn.pendingTerminality = true;

    let history: Message[] = [];
    try {
      const [messages, cached] = await Promise.all([
        this.persistence.readMessages(runId),
        this.persistence.getRun(runId),
      ]);
      history = messages;
      if (cached?.terminal) conn.terminal = true;
      if (cached?.status && conn.status === null) conn.status = cached.status;
      const newest = messages.at(-1);
      if (conn.lastSeq === null && newest) conn.lastSeq = newest.seq;
    } catch (error) {
      // A cache read failing is not a reason to refuse the live stream.
      this.onError("hydrate", error);
    }

    const entry = conn.ports.get(port);
    if (!entry) return; // unsubscribed while we were reading

    port.postMessage({ kind: "hydrate", run_id: runId, messages: history });
    entry.hydrating = false;
    if (entry.pending.length > 0) {
      port.postMessage({ kind: "batch", run_id: runId, messages: entry.pending });
      entry.pending = [];
    }
    port.postMessage({
      kind: "state",
      run_id: runId,
      state: conn.state,
      consecutive_failures: conn.consecutiveFailures,
    });

    if (conn.terminal) {
      // Nothing further will ever arrive. Say so once, so the page can stop
      // showing a spinner, and open no connection.
      if (conn.status) {
        port.postMessage({ kind: "terminal", run_id: runId, status: conn.status });
      }
      return;
    }

    // Hydrated, but the caller has not said whether this run is over.
    // `run-terminality` releases it — see the note on that message.
    if (conn.pendingTerminality) return;

    this.open(conn);
  }

  /**
   * A subscriber has learned this run is over.
   *
   * Two moments this can land, and it has to be right in both. If the channel
   * is already open, close it — nothing further will arrive on it. If
   * `subscribe` is still awaiting its IndexedDB hydrate, setting the flag is
   * enough: the check that decides whether to open runs *after* that await,
   * so no connection is made at all. That second case is the common one on a
   * cold page and is the point of the hint.
   */
  private setTerminality(runId: string, terminal: boolean): void {
    const conn = this.runs.get(runId);
    if (conn === undefined) return;

    if (!terminal) {
      // The subscription was taken out before the caller knew; this releases
      // it. `open` is idempotent and returns immediately if a channel exists.
      conn.pendingTerminality = false;
      this.open(conn);
      return;
    }

    if (conn.terminal) return;
    conn.terminal = true;
    conn.pendingTerminality = false;
    if (conn.es !== null || conn.retryTimer !== null) {
      this.teardown(conn);
      this.setState(conn, "idle");
    }
  }

  private unsubscribe(port: PortLike, runId: string): void {
    const conn = this.runs.get(runId);
    if (!conn || !conn.ports.delete(port)) return;
    if (conn.ports.size > 0) return;
    this.teardown(conn);
    this.runs.delete(runId);
  }

  private teardown(conn: RunConn): void {
    if (conn.flushTimer !== null) clearTimeout(conn.flushTimer);
    if (conn.retryTimer !== null) clearTimeout(conn.retryTimer);
    conn.flushTimer = null;
    conn.retryTimer = null;
    conn.es?.close();
    conn.es = null;
  }

  /* ---------------------------------------------------------------- *
   * The live channel
   * ---------------------------------------------------------------- */

  private open(conn: RunConn): void {
    if (conn.es !== null || conn.terminal || conn.state === "failed") return;
    if (conn.pendingTerminality) return;
    if (conn.retryTimer !== null) {
      clearTimeout(conn.retryTimer);
      conn.retryTimer = null;
    }

    let es: EventSourceLike;
    try {
      es = this.createEventSource(this.streamUrl(conn.runId));
    } catch (error) {
      this.onError("open", error);
      this.setState(conn, "failed");
      return;
    }
    conn.es = es;
    this.setState(conn, "connecting");

    es.addEventListener("open", () => this.onOpen(conn));
    es.addEventListener("error", () => this.onErrorEvent(conn));
    // Named events, one listener each: the server sets `event: <type>` on
    // every frame, so a single `onmessage` with a type switch would only ever
    // fire for frames that had no name — i.e. never.
    for (const type of ["log", "progress", "status", "result"] as const) {
      es.addEventListener(type, (event) => this.onFrame(conn, event));
    }
  }

  private onOpen(conn: RunConn): void {
    // Success. This — and a frame arriving — is what makes the counter
    // consecutive rather than cumulative.
    conn.consecutiveFailures = 0;
    this.setState(conn, "open");
  }

  private onErrorEvent(conn: RunConn): void {
    conn.consecutiveFailures += 1;

    if (conn.consecutiveFailures >= this.maxConsecutiveFailures) {
      conn.es?.close();
      conn.es = null;
      this.setState(conn, "failed");
      return;
    }

    this.setState(conn, "connecting");

    // readyState CONNECTING means EventSource is retrying on its own (the
    // server's `retry: 2000`), and creating a second one would double up.
    // CLOSED means it gave up — that one is ours to reopen.
    if (conn.es !== null && conn.es.readyState !== ES_CLOSED) return;

    conn.es?.close();
    conn.es = null;
    const delay = this.backoff(conn.consecutiveFailures);
    conn.retryTimer = setTimeout(() => {
      conn.retryTimer = null;
      if (conn.ports.size > 0) this.open(conn);
    }, delay);
  }

  /** Exponential with jitter, capped. Jitter matters because every tab in
   *  the account reconnects at once when the ingress cuts. */
  private backoff(failures: number): number {
    const base = Math.min(30_000, 500 * 2 ** (failures - 1));
    return Math.round(base * (0.8 + Math.random() * 0.4));
  }

  private onFrame(conn: RunConn, event: MessageEvent): void {
    conn.consecutiveFailures = 0;
    if (conn.state !== "open") this.setState(conn, "open");

    const data = typeof event.data === "string" ? event.data : "";
    const msg = parseFrame(data);
    if (msg === null) {
      this.droppedFrames += 1;
      return;
    }
    if (msg.run_id !== conn.runId) {
      // Cannot happen against this server; if it ever does, storing it under
      // the wrong run is worse than dropping it.
      this.droppedFrames += 1;
      return;
    }

    // A reconnect replays nothing at or below Last-Event-ID, but a snapshot on
    // a fresh connection can repeat what we already hold. Dedupe on seq.
    if (conn.lastSeq !== null && msg.seq <= conn.lastSeq) return;

    if (conn.lastSeq !== null && msg.seq > conn.lastSeq + 1) {
      this.broadcast(conn, {
        kind: "gap",
        run_id: conn.runId,
        from: conn.lastSeq + 1,
        to: msg.seq - 1,
      });
    }
    conn.lastSeq = msg.seq;
    conn.buffer.push(msg);

    if (msg.type === "status") {
      conn.status = msg.status;
      if (isTerminal(msg.status)) conn.terminal = true;
    }

    // Progress can arrive many times a second; status and result are what a
    // user is actually waiting for, so they skip the coalescing window.
    const urgent = msg.type === "status" || msg.type === "result";
    if (urgent) {
      this.flush(conn);
    } else if (conn.flushTimer === null) {
      conn.flushTimer = setTimeout(() => this.flush(conn), this.flushMs);
    }

    if (conn.terminal) {
      this.teardown(conn);
      this.setState(conn, "idle");
      if (conn.status) {
        this.broadcast(conn, { kind: "terminal", run_id: conn.runId, status: conn.status });
      }
    }
  }

  /* ---------------------------------------------------------------- *
   * Fan-out
   * ---------------------------------------------------------------- */

  private flush(conn: RunConn): void {
    if (conn.flushTimer !== null) {
      clearTimeout(conn.flushTimer);
      conn.flushTimer = null;
    }
    if (conn.buffer.length === 0) return;
    const messages = conn.buffer;
    conn.buffer = [];

    // Ports first, persistence second: the UI should not wait on a disk
    // write, and a persistence failure costs a cache entry, not a frame.
    for (const [port, entry] of conn.ports) {
      if (entry.hydrating) {
        entry.pending.push(...messages);
      } else {
        port.postMessage({ kind: "batch", run_id: conn.runId, messages });
      }
    }

    void this.persist(conn, messages);
  }

  private async persist(conn: RunConn, messages: readonly Message[]): Promise<void> {
    try {
      await this.persistence.putMessages(messages);
      await this.persistence.putRun({
        run_id: conn.runId,
        model: null,
        status: conn.status,
        terminal: conn.terminal,
        last_seq: conn.lastSeq ?? -1,
        updated_ts: this.now(),
      });
    } catch (error) {
      this.onError("persist", error);
    }
  }

  private setState(conn: RunConn, state: ConnectionState): void {
    if (conn.state === state) return;
    conn.state = state;
    this.broadcast(conn, {
      kind: "state",
      run_id: conn.runId,
      state,
      consecutive_failures: conn.consecutiveFailures,
    });
  }

  private broadcast(conn: RunConn, event: WorkerEvent): void {
    for (const [port, entry] of conn.ports) {
      // State and gap events are about the transport, not the message
      // stream, so they do not need to wait behind a hydrate.
      if (entry.hydrating && event.kind === "batch") continue;
      port.postMessage(event);
    }
  }

  /* ---------------------------------------------------------------- *
   * Introspection (tests, and a future diagnostics panel)
   * ---------------------------------------------------------------- */

  stats(): {
    runs: number;
    ports: number;
    droppedFrames: number;
    connections: Array<{
      run_id: string;
      state: ConnectionState;
      consecutive_failures: number;
      last_seq: number | null;
      terminal: boolean;
      pending_terminality: boolean;
      ports: number;
      open: boolean;
    }>;
  } {
    return {
      runs: this.runs.size,
      ports: this.portsSeen.size,
      droppedFrames: this.droppedFrames,
      connections: [...this.runs.values()].map((c) => ({
        run_id: c.runId,
        state: c.state,
        consecutive_failures: c.consecutiveFailures,
        last_seq: c.lastSeq,
        terminal: c.terminal,
        pending_terminality: c.pendingTerminality,
        ports: c.ports.size,
        open: c.es !== null,
      })),
    };
  }
}
