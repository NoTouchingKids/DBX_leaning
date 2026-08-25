/**
 * Page side of the transport.
 *
 * Owns exactly one worker for the whole tab, ref-counts subscriptions per
 * run, and routes worker events into per-run stores. React never talks to
 * this directly — `useRunStream` does.
 *
 * Three tiers, in order:
 *
 *  1. `SharedWorker` — one EventSource per run across ALL tabs. This is the
 *     one that matters: five tabs on the same run is one connection, and on
 *     Free Edition connections are the scarce thing.
 *  2. Dedicated `Worker` — per tab, parse and IndexedDB still off the main
 *     thread. Cheap defensive line for a browser without SharedWorker.
 *  3. In-page `StreamHub` — same logic, main thread. Not a design goal; it
 *     exists so a worker that fails to construct degrades to "works, slightly
 *     janky" instead of "blank page".
 */

import * as db from "./db";
import { StreamHub, type EventSourceLike, type PortLike } from "./hub";
import type { WorkerEvent, WorkerRequest } from "./protocol";
import { RunStore } from "./runStore";

export type TransportTier = "shared-worker" | "worker" | "in-page";

interface Channel {
  tier: TransportTier;
  post(request: WorkerRequest): void;
  dispose(): void;
}

const PING_MS = 15_000;
/** Idle run stores kept in memory. Each is capped internally; this caps the
 *  number of them. */
const MAX_CACHED_STORES = 20;

// NOTE: `new URL("./worker.ts", import.meta.url)` is written out at each
// construction site below and must stay that way. Vite detects the worker
// entry by matching that exact expression inside `new Worker(...)` /
// `new SharedWorker(...)`; hoisting it into a helper compiles cleanly, works
// in dev, and silently emits no worker chunk in the production build. That
// is not hypothetical — this file did exactly that until the build was
// checked for the chunk.

export interface TransportClientOptions {
  /**
   * Skip tier detection. Used by tests — jsdom has no SharedWorker and
   * cannot resolve a module Worker pointed at a `.ts` entry, so the React
   * binding is exercised against the in-page tier.
   */
  forceTier?: TransportTier;
  /** Only reaches the in-page tier; a real worker constructs its own. */
  createEventSource?: (url: string) => EventSourceLike;
}

export class TransportClient {
  private readonly options: TransportClientOptions;
  private channel: Channel | null = null;
  private readonly stores = new Map<string, { store: RunStore; refs: number }>();
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private disposed = false;

  constructor(options: TransportClientOptions = {}) {
    this.options = options;
  }

  /** Which tier actually started. Surfaced so a diagnostics view can show it
   *  — a silent downgrade to in-page is worth being able to see. */
  get tier(): TransportTier | null {
    return this.channel?.tier ?? null;
  }

  /**
   * The store for a run, created on demand. Pure and idempotent — it opens
   * nothing. Split from `acquire` because `useSyncExternalStore` needs a
   * stable store to read during render, BEFORE it calls subscribe, and
   * because subscribing during render double-counts under StrictMode.
   */
  getStore(runId: string): RunStore {
    let entry = this.stores.get(runId);
    if (!entry) {
      entry = { store: new RunStore(runId), refs: 0 };
      this.stores.set(runId, entry);
      this.evict();
    }
    return entry.store;
  }

  /**
   * Start watching a run. Returns the release function; the last release
   * closes the live channel. Safe to call repeatedly for the same run — the
   * connection is ref-counted, and StrictMode's mount/unmount/mount is
   * exactly the case this has to survive.
   *
   * @param terminal what the caller already knows from `GET /api/runs/{id}`.
   *                 A terminal run is hydrated from cache and never opens a
   *                 live channel.
   */
  acquire(runId: string, terminal = false): () => void {
    const channel = this.ensureChannel();
    this.getStore(runId);
    const entry = this.stores.get(runId);
    if (!entry) throw new Error("unreachable: store vanished");
    entry.refs += 1;
    if (entry.refs === 1) {
      channel.post({ kind: "subscribe", run_id: runId, terminal });
    }

    let released = false;
    return () => {
      if (released) return;
      released = true;
      const current = this.stores.get(runId);
      if (!current) return;
      current.refs -= 1;
      if (current.refs > 0) return;
      // The store stays — a remount should not re-read IndexedDB, and the
      // messages are already capped. Only the connection goes.
      this.channel?.post({ kind: "unsubscribe", run_id: runId });
    };
  }

  /**
   * Drop the least recently created idle store once the map grows past the
   * cap. A day of browsing run history would otherwise accumulate every run
   * ever opened; Map preserves insertion order, so the first idle entry is
   * the oldest.
   */
  private evict(): void {
    if (this.stores.size <= MAX_CACHED_STORES) return;
    for (const [runId, entry] of this.stores) {
      if (entry.refs === 0) {
        this.stores.delete(runId);
        if (this.stores.size <= MAX_CACHED_STORES) return;
      }
    }
  }

  /**
   * Answer the worker's open question about a run: is it over?
   *
   * A subscription taken out before this is known hydrates from IndexedDB but
   * opens no channel, so this is what releases it — `false` opens one, `true`
   * keeps it shut. Safe to call repeatedly, and safe for a run nothing is
   * watching.
   */
  setTerminality(runId: string, terminal: boolean): void {
    this.channel?.post({ kind: "run-terminality", run_id: runId, terminal });
  }

  dispose(): void {
    this.disposed = true;
    if (this.pingTimer !== null) clearInterval(this.pingTimer);
    this.pingTimer = null;
    this.channel?.post({ kind: "bye" });
    this.channel?.dispose();
    this.channel = null;
    this.stores.clear();
  }

  /* ---------------------------------------------------------------- */

  private ensureChannel(): Channel {
    if (this.channel) return this.channel;
    if (this.disposed) throw new Error("transport client disposed");

    const forced = this.options.forceTier;
    this.channel =
      forced === "in-page"
        ? this.openInPage()
        : forced === "worker"
          ? (this.openWorker() ?? this.openInPage())
          : (this.openSharedWorker() ?? this.openWorker() ?? this.openInPage());

    this.pingTimer = setInterval(() => this.channel?.post({ kind: "ping" }), PING_MS);
    if (typeof addEventListener === "function") {
      // pagehide, not beforeunload: it fires for bfcache navigations too, and
      // a port left behind by a bfcached tab is exactly what the worker's
      // sweep exists to clean up. Telling it directly is cheaper.
      addEventListener("pagehide", () => this.channel?.post({ kind: "bye" }));
    }
    return this.channel;
  }

  private route = (event: WorkerEvent): void => {
    const entry = this.stores.get(event.run_id);
    if (!entry) return; // unsubscribed between post and delivery
    switch (event.kind) {
      case "hydrate":
        entry.store.ingest(event.messages, { hydrate: true });
        return;
      case "batch":
        entry.store.ingest(event.messages);
        return;
      case "state":
        entry.store.setConnection(event.state, event.consecutive_failures);
        return;
      case "gap":
        entry.store.addGap({ from: event.from, to: event.to });
        return;
      case "terminal":
        entry.store.markTerminal(event.status);
        return;
    }
  };

  private openSharedWorker(): Channel | null {
    if (typeof SharedWorker === "undefined") return null;
    try {
      const worker = new SharedWorker(new URL("./worker.ts", import.meta.url), {
        type: "module",
        name: "dbx-stream",
      });
      worker.port.addEventListener("message", (event: MessageEvent<WorkerEvent>) =>
        this.route(event.data),
      );
      worker.port.start();
      return {
        tier: "shared-worker",
        post: (request) => worker.port.postMessage(request),
        dispose: () => worker.port.close(),
      };
    } catch {
      return null;
    }
  }

  private openWorker(): Channel | null {
    if (typeof Worker === "undefined") return null;
    try {
      const worker = new Worker(new URL("./worker.ts", import.meta.url), {
        type: "module",
        name: "dbx-stream",
      });
      worker.addEventListener("message", (event: MessageEvent<WorkerEvent>) =>
        this.route(event.data),
      );
      return {
        tier: "worker",
        post: (request) => worker.postMessage(request),
        dispose: () => worker.terminate(),
      };
    } catch {
      return null;
    }
  }

  private openInPage(): Channel {
    const hub = new StreamHub({
      persistence: db,
      createEventSource: this.options.createEventSource,
      onError: (context, error) => console.warn(`[stream-inpage] ${context}`, error),
    });
    const port: PortLike = { postMessage: (event) => this.route(event) };
    hub.connect(port);
    const sweep = setInterval(() => hub.sweep(), PING_MS);
    return {
      tier: "in-page",
      post: (request) => hub.handle(port, request),
      dispose: () => {
        clearInterval(sweep);
        hub.disconnect(port);
      },
    };
  }
}

let shared: TransportClient | null = null;

/** The tab's single client. Lazy so importing this module in a test or an
 *  SSR-ish context does not spawn a worker. */
export function getTransport(): TransportClient {
  shared ??= new TransportClient();
  return shared;
}

export function resetTransportForTests(): void {
  shared?.dispose();
  shared = null;
}

/** Install a preconfigured client. Tests only — production always uses the
 *  lazily-created singleton so nothing can quietly swap the transport. */
export function __setTransportForTests(client: TransportClient): void {
  shared?.dispose();
  shared = client;
}
