import { describe, expect, it, vi } from "vitest";

import type { Message } from "@/lib/envelope";
import { MAX_LOGS, RunStore } from "./runStore";

function log(seq: number): Message {
  return {
    type: "log", run_id: "r", seq, ts: seq,
    message: `line ${seq}`, level: "INFO", source: "model", phase: "run", client_visible: true,
  };
}
function progress(seq: number, metric: number | null = null): Message {
  return {
    type: "progress", run_id: "r", seq, ts: seq, elapsed_seconds: seq,
    percent_complete: null, primary_metric: metric, primary_metric_label: "gap", payload: {},
  };
}
function status(seq: number, value: "RUNNING" | "SUCCEEDED"): Message {
  return { type: "status", run_id: "r", seq, ts: seq, status: value, detail: null };
}

describe("RunStore", () => {
  it("splits by type so consumers do not filter on every render", () => {
    const store = new RunStore("r");
    store.ingest([log(0), progress(1, 2), status(2, "RUNNING")]);
    const snap = store.getSnapshot();
    expect(snap.logs).toHaveLength(1);
    expect(snap.progress).toHaveLength(1);
    expect(snap.statuses).toHaveLength(1);
    expect(snap.latestProgress?.primary_metric).toBe(2);
  });

  it("replaces the snapshot once per batch, not once per message", () => {
    const store = new RunStore("r");
    const listener = vi.fn();
    store.subscribe(listener);
    store.ingest([log(0), log(1), log(2)]);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("keeps snapshot identity stable when nothing changes", () => {
    const store = new RunStore("r");
    const first = store.getSnapshot();
    store.ingest([]);
    expect(store.getSnapshot()).toBe(first);
  });

  it("takes the highest-seq status, not the last appended", () => {
    // Hydrate can deliver history after a live message has already landed.
    const store = new RunStore("r");
    store.ingest([status(9, "SUCCEEDED")]);
    store.ingest([status(1, "RUNNING")], { hydrate: true });
    expect(store.getSnapshot().status).toBe("SUCCEEDED");
    expect(store.getSnapshot().terminal).toBe(true);
  });

  it("caps logs and counts what it dropped", () => {
    const store = new RunStore("r");
    store.ingest(Array.from({ length: MAX_LOGS + 25 }, (_, i) => log(i)));
    const snap = store.getSnapshot();
    expect(snap.logs).toHaveLength(MAX_LOGS);
    expect(snap.droppedLogs).toBe(25);
    // Oldest go first, so the tail — the interesting part — is intact.
    expect(snap.logs[0]?.seq).toBe(25);
  });

  it("never drops statuses or results", () => {
    const store = new RunStore("r");
    store.ingest(Array.from({ length: MAX_LOGS * 2 }, (_, i) => log(i)));
    store.ingest([status(99_999, "SUCCEEDED")]);
    expect(store.getSnapshot().statuses).toHaveLength(1);
  });

  it("distinguishes 'not read yet' from 'nothing happened'", () => {
    const store = new RunStore("r");
    expect(store.getSnapshot().hydrated).toBe(false);
    store.ingest([], { hydrate: true });
    expect(store.getSnapshot().hydrated).toBe(true);
  });

  it("records gaps without closing them", () => {
    const store = new RunStore("r");
    store.addGap({ from: 3, to: 7 });
    expect(store.getSnapshot().gaps).toEqual([{ from: 3, to: 7 }]);
  });

  it("tracks connection state separately from run state", () => {
    const store = new RunStore("r");
    store.ingest([status(0, "RUNNING")]);
    store.setConnection("failed", 10);
    const snap = store.getSnapshot();
    // The run is still RUNNING; it is our view of it that failed.
    expect(snap.status).toBe("RUNNING");
    expect(snap.connection).toBe("failed");
    expect(snap.consecutiveFailures).toBe(10);
  });
});
