import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { Message } from "@/lib/envelope";
import { FakeEventSource } from "./__fixtures__/fakeEventSource";
import type { CachedRun } from "./db";
import { StreamHub, type HubPersistence, type PortLike } from "./hub";
import type { WorkerEvent } from "./protocol";

class RecordingPort implements PortLike {
  readonly events: WorkerEvent[] = [];
  postMessage(event: WorkerEvent): void {
    this.events.push(event);
  }
  of<K extends WorkerEvent["kind"]>(kind: K): Array<Extract<WorkerEvent, { kind: K }>> {
    return this.events.filter((e) => e.kind === kind) as Array<
      Extract<WorkerEvent, { kind: K }>
    >;
  }
  get messages(): Message[] {
    return this.events.flatMap((e) =>
      e.kind === "batch" || e.kind === "hydrate" ? e.messages : [],
    );
  }
}

class MemoryPersistence implements HubPersistence {
  readonly stored = new Map<string, Message[]>();
  readonly runs = new Map<string, CachedRun>();
  async putMessages(messages: readonly Message[]): Promise<void> {
    for (const msg of messages) {
      const bucket = this.stored.get(msg.run_id) ?? [];
      bucket.push(msg);
      this.stored.set(msg.run_id, bucket);
    }
  }
  async readMessages(runId: string): Promise<Message[]> {
    return [...(this.stored.get(runId) ?? [])];
  }
  async putRun(run: CachedRun): Promise<void> {
    this.runs.set(run.run_id, run);
  }
  async getRun(runId: string): Promise<CachedRun | undefined> {
    return this.runs.get(runId);
  }
}

const RUN = "run-abc";

function log(seq: number, extra: Record<string, unknown> = {}) {
  return { type: "log", run_id: RUN, seq, ts: 1_000 + seq, message: `line ${seq}`, ...extra };
}
function progress(seq: number, extra: Record<string, unknown> = {}) {
  return { type: "progress", run_id: RUN, seq, ts: 1_000 + seq, elapsed_seconds: seq, ...extra };
}
function status(seq: number, value: string) {
  return { type: "status", run_id: RUN, seq, ts: 1_000 + seq, status: value };
}

function makeHub(overrides: Partial<ConstructorParameters<typeof StreamHub>[0]> = {}) {
  const persistence = new MemoryPersistence();
  const hub = new StreamHub({
    createEventSource: (url) => new FakeEventSource(url),
    persistence,
    flushMs: 10,
    now: () => Date.now(),
    ...overrides,
  });
  return { hub, persistence };
}

/** Subscribe and let the hydrate read settle. */
async function subscribe(hub: StreamHub, port: PortLike, terminal = false) {
  hub.connect(port);
  hub.handle(port, { kind: "subscribe", run_id: RUN, terminal });
  await vi.waitFor(() => {
    const events = (port as RecordingPort).events;
    if (!events.some((e) => e.kind === "hydrate")) throw new Error("not hydrated");
  });
}

beforeEach(() => {
  FakeEventSource.reset();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe("StreamHub — connection lifecycle", () => {
  it("opens one EventSource per run, shared by every port", async () => {
    const { hub } = makeHub();
    const a = new RecordingPort();
    const b = new RecordingPort();
    await subscribe(hub, a);
    await subscribe(hub, b);

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(hub.stats().connections[0]?.ports).toBe(2);
  });

  it("closes the connection when the last port unsubscribes", async () => {
    const { hub } = makeHub();
    const a = new RecordingPort();
    const b = new RecordingPort();
    await subscribe(hub, a);
    await subscribe(hub, b);

    hub.handle(a, { kind: "unsubscribe", run_id: RUN });
    expect(FakeEventSource.last.closed).toBe(false);

    hub.handle(b, { kind: "unsubscribe", run_id: RUN });
    expect(FakeEventSource.last.closed).toBe(true);
    expect(hub.stats().runs).toBe(0);
  });

  it("opens no connection for a run the caller knows is terminal", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port, true);
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("drops ports that stop pinging, and closes their connections", async () => {
    let clock = 0;
    const { hub } = makeHub({ now: () => clock, portTimeoutMs: 1_000 });
    const port = new RecordingPort();
    await subscribe(hub, port);
    expect(FakeEventSource.last.closed).toBe(false);

    clock = 5_000;
    hub.sweep();
    expect(FakeEventSource.last.closed).toBe(true);
    expect(hub.stats().ports).toBe(0);
  });
});

describe("StreamHub — the reconnect counter", () => {
  it("resets on every success, so repeated ingress cuts never trip it", async () => {
    // The trap: Databricks Apps' ingress cuts long-lived connections
    // periodically. A cumulative counter kills a healthy stream a few
    // minutes in and it looks exactly like the server dying.
    const { hub } = makeHub({ maxConsecutiveFailures: 3 });
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    for (let i = 0; i < 20; i += 1) {
      es.cut(); // browser retries this same instance
      es.open(); // ...and succeeds
    }

    expect(hub.stats().connections[0]?.state).toBe("open");
    expect(hub.stats().connections[0]?.consecutive_failures).toBe(0);
    expect(es.closed).toBe(false);
  });

  it("a received frame also counts as success", async () => {
    const { hub } = makeHub({ maxConsecutiveFailures: 3 });
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.cut();
    expect(hub.stats().connections[0]?.consecutive_failures).toBe(1);
    es.emit(progress(0));
    expect(hub.stats().connections[0]?.consecutive_failures).toBe(0);
  });

  it("gives up after N failures with nothing in between", async () => {
    const { hub } = makeHub({ maxConsecutiveFailures: 3 });
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;

    es.cut();
    es.cut();
    expect(hub.stats().connections[0]?.state).toBe("connecting");
    es.cut();

    expect(hub.stats().connections[0]?.state).toBe("failed");
    expect(es.closed).toBe(true);
    expect(port.of("state").at(-1)?.state).toBe("failed");
  });

  it("reopens itself when the browser gives up on an instance", async () => {
    const { hub } = makeHub({ maxConsecutiveFailures: 10 });
    const port = new RecordingPort();
    await subscribe(hub, port);

    FakeEventSource.last.fail(); // readyState CLOSED — the browser is done
    expect(FakeEventSource.instances).toHaveLength(1);

    await vi.advanceTimersByTimeAsync(2_000);
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("does not stack a second EventSource while the browser is retrying", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);

    FakeEventSource.last.cut(); // readyState CONNECTING — retry in flight
    await vi.advanceTimersByTimeAsync(10_000);
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});

describe("StreamHub — messages", () => {
  it("listens per named event, not a single onmessage", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emit(log(0));
    es.emit(progress(1));
    es.emit(status(2, "RUNNING"));
    await vi.advanceTimersByTimeAsync(50);

    expect(port.messages.map((m) => m.type)).toEqual(["log", "progress", "status"]);
  });

  it("coalesces progress but flushes status and result at once", async () => {
    const { hub } = makeHub({ flushMs: 100 });
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emit(progress(0));
    es.emit(progress(1));
    expect(port.of("batch")).toHaveLength(0); // still inside the window

    es.emit(status(2, "RUNNING"));
    expect(port.of("batch")).toHaveLength(1);
    expect(port.of("batch")[0]?.messages).toHaveLength(3);
  });

  it("drops an unparseable frame without taking the stream down", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emitRaw("progress", "{not json");
    es.emitRaw("progress", JSON.stringify({ type: "progress" })); // no run_id/seq
    es.emit(progress(0));
    await vi.advanceTimersByTimeAsync(50);

    expect(hub.stats().droppedFrames).toBe(2);
    expect(port.messages).toHaveLength(1);
  });

  it("ignores a repeat of a seq it already has", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emit(progress(5));
    es.emit(progress(5));
    es.emit(progress(4));
    await vi.advanceTimersByTimeAsync(50);

    expect(port.messages).toHaveLength(1);
  });

  it("persists what it broadcasts", async () => {
    const { hub, persistence } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emit(progress(0));
    es.emit(progress(1));
    await vi.advanceTimersByTimeAsync(50);
    await vi.waitFor(() => {
      if ((persistence.stored.get(RUN) ?? []).length !== 2) throw new Error("not yet");
    });
    expect(persistence.runs.get(RUN)?.last_seq).toBe(1);
  });
});

describe("StreamHub — gaps", () => {
  it("reports a hole in the seq stream and never acts on it", async () => {
    // seq is gap-free at the source, but the live path drops
    // client_visible=False logs — and so does the backfill endpoint, so
    // this hole may never close. Reported, never auto-backfilled.
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emit(progress(0));
    es.emit(progress(5));
    await vi.advanceTimersByTimeAsync(50);

    expect(port.of("gap")).toEqual([{ kind: "gap", run_id: RUN, from: 1, to: 4 }]);
  });

  it("reports nothing for a contiguous stream", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    for (let seq = 0; seq < 5; seq += 1) es.emit(progress(seq));
    await vi.advanceTimersByTimeAsync(50);

    expect(port.of("gap")).toHaveLength(0);
  });
});

describe("StreamHub — terminal runs", () => {
  it("closes the channel on a terminal status and says so", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    es.emit(status(0, "RUNNING"));
    expect(es.closed).toBe(false);

    es.emit(status(1, "SUCCEEDED"));
    expect(es.closed).toBe(true);
    expect(port.of("terminal").at(-1)?.status).toBe("SUCCEEDED");
  });

  it.each(["FAILED", "CANCELLED", "INFEASIBLE"])("treats %s as terminal too", async (value) => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();
    es.emit(status(0, value));
    expect(es.closed).toBe(true);
  });

  it("opens no channel when the terminal hint lands during hydration", async () => {
    // The cold-page case, and the reason the hint exists. A page subscribes
    // before `GET /api/runs/{id}` has resolved, so it says "not terminal"
    // because it does not know yet. The answer arrives a moment later, while
    // the worker is still reading IndexedDB — and the check that decides
    // whether to open runs after that read, so nothing is opened.
    const { hub } = makeHub();
    const port = new RecordingPort();
    hub.connect(port);
    hub.handle(port, { kind: "subscribe", run_id: RUN });
    hub.handle(port, { kind: "run-terminality", run_id: RUN, terminal: true });

    await vi.waitFor(() => {
      if (!port.events.some((e) => e.kind === "hydrate")) throw new Error("not hydrated");
    });

    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("closes an open channel when the terminal hint arrives late", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    await subscribe(hub, port);
    FakeEventSource.last.open();
    expect(FakeEventSource.last.closed).toBe(false);

    hub.handle(port, { kind: "run-terminality", run_id: RUN, terminal: true });

    expect(FakeEventSource.last.closed).toBe(true);
    expect(hub.stats().connections[0]?.terminal).toBe(true);
  });

  it("ignores a terminal hint for a run nothing is watching", async () => {
    const { hub } = makeHub();
    const port = new RecordingPort();
    hub.connect(port);
    // Must not create a connection record as a side effect of being told
    // about a run — that would leak one per hint.
    hub.handle(port, { kind: "run-terminality", run_id: "run-nobody-watches", terminal: true });
    expect(hub.stats().runs).toBe(0);
  });

  it("reopens nothing for a run already cached as terminal", async () => {
    const { hub, persistence } = makeHub();
    await persistence.putRun({
      run_id: RUN,
      model: "mcmc",
      status: "SUCCEEDED",
      terminal: true,
      last_seq: 9,
      updated_ts: 0,
    });
    const port = new RecordingPort();
    await subscribe(hub, port);

    expect(FakeEventSource.instances).toHaveLength(0);
    expect(port.of("terminal").at(-1)?.status).toBe("SUCCEEDED");
  });
});

describe("StreamHub — hydration ordering", () => {
  it("delivers cached history before any live batch", async () => {
    const { hub, persistence } = makeHub();
    await persistence.putMessages([
      { type: "log", run_id: RUN, seq: 0, ts: 1, message: "old", level: "INFO", source: "model", phase: "run", client_visible: true },
    ]);
    const port = new RecordingPort();
    await subscribe(hub, port);

    const kinds = port.events.map((e) => e.kind);
    expect(kinds[0]).toBe("hydrate");
    expect(port.of("hydrate")[0]?.messages).toHaveLength(1);
  });

  it("does not replay cached seqs from the live stream", async () => {
    const { hub, persistence } = makeHub();
    await persistence.putMessages([
      { type: "progress", run_id: RUN, seq: 7, ts: 1, elapsed_seconds: 1, percent_complete: null, primary_metric: null, primary_metric_label: null, payload: {} },
    ]);
    const port = new RecordingPort();
    await subscribe(hub, port);
    const es = FakeEventSource.last;
    es.open();

    // The snapshot a fresh EventSource receives can repeat what we hold.
    es.emit(progress(7));
    es.emit(progress(8));
    await vi.advanceTimersByTimeAsync(50);

    const live = port.of("batch").flatMap((e) => e.messages);
    expect(live.map((m) => m.seq)).toEqual([8]);
  });
});
