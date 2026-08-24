import { act, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FakeEventSource } from "./__fixtures__/fakeEventSource";
import { closeDb } from "./db";
import { TransportClient, __setTransportForTests, resetTransportForTests } from "./client";
import { useRunStream } from "./useRunStream";

const RUN = "run-hook";

function Probe({ runId }: { runId: string | null }) {
  const snap = useRunStream(runId);
  return (
    <div>
      <span data-testid="conn">{snap.connection}</span>
      <span data-testid="logs">{snap.logs.length}</span>
      <span data-testid="status">{snap.status ?? "-"}</span>
    </div>
  );
}

/** The hub opens its EventSource only after the IndexedDB hydrate resolves,
 *  which is more than a microtask. */
const connected = () =>
  waitFor(() => expect(FakeEventSource.instances.length).toBeGreaterThan(0));

beforeEach(async () => {
  FakeEventSource.reset();
  await closeDb();
  // Force the in-page tier: jsdom has no SharedWorker and cannot resolve a
  // module Worker pointed at a `.ts` entry. What is under test here is the
  // React binding, not the worker plumbing — `hub.test.ts` covers that.
  __setTransportForTests(
    new TransportClient({
      forceTier: "in-page",
      createEventSource: (url) => new FakeEventSource(url),
    }),
  );
});

afterEach(async () => {
  resetTransportForTests();
  await closeDb();
});

describe("useRunStream", () => {
  it("opens exactly one connection under StrictMode's double mount", async () => {
    // The bug this guards: subscribing during render (or in a useMemo) runs
    // twice and is cleaned up once, leaking a live EventSource per mount.
    render(
      <StrictMode>
        <Probe runId={RUN} />
      </StrictMode>,
    );
    await connected();
    expect(FakeEventSource.instances).toHaveLength(1);
  });

  it("releases the connection on unmount", async () => {
    const view = render(<Probe runId={RUN} />);
    await connected();
    expect(FakeEventSource.last.closed).toBe(false);

    view.unmount();
    expect(FakeEventSource.last.closed).toBe(true);
  });

  it("re-renders as messages arrive", async () => {
    render(<Probe runId={RUN} />);
    await connected();
    const es = FakeEventSource.last;

    act(() => es.open());
    expect(screen.getByTestId("conn").textContent).toBe("open");

    act(() => {
      es.emit({
        type: "log", run_id: RUN, seq: 0, ts: 1,
        message: "hello", level: "INFO", source: "model", phase: "run", client_visible: true,
      });
      es.emit({ type: "status", run_id: RUN, seq: 1, ts: 2, status: "RUNNING", detail: null });
    });

    // The status flush is immediate; it carries the coalesced log with it.
    await waitFor(() => {
      expect(screen.getByTestId("logs").textContent).toBe("1");
      expect(screen.getByTestId("status").textContent).toBe("RUNNING");
    });
  });

  it("opens nothing when there is no run id", async () => {
    render(<Probe runId={null} />);
    await Promise.resolve();
    expect(FakeEventSource.instances).toHaveLength(0);
    expect(screen.getByTestId("conn").textContent).toBe("idle");
  });

  it("shares one connection between two components on the same run", async () => {
    render(
      <>
        <Probe runId={RUN} />
        <Probe runId={RUN} />
      </>,
    );
    await connected();
    // Give a second connection every chance to appear before asserting it did not.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(FakeEventSource.instances).toHaveLength(1);
  });
});
