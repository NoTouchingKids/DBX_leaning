/// <reference lib="webworker" />
/**
 * The worker shell. Everything interesting is in `hub.ts`; this file exists
 * to hand it a real MessagePort and a real IndexedDB, and it is kept small
 * enough to be read rather than tested.
 *
 * It is the body of BOTH the SharedWorker and the per-tab dedicated-Worker
 * fallback — the two differ only in how a port arrives.
 */

import * as db from "./db";
import { StreamHub, type PortLike } from "./hub";
import type { WorkerRequest } from "./protocol";

const hub = new StreamHub({
  persistence: db,
  onError: (context, error) => {
    // Nothing to report to; a tab has no channel for worker-internal faults
    // and inventing one would be a UI for something users cannot act on.
    console.warn(`[stream-worker] ${context}`, error);
  },
});

const SWEEP_MS = 15_000;
setInterval(() => hub.sweep(), SWEEP_MS);

function wire(port: MessagePort): void {
  hub.connect(port as PortLike);
  port.addEventListener("message", (event: MessageEvent<WorkerRequest>) => {
    hub.handle(port as PortLike, event.data);
  });
  // addEventListener on a port requires an explicit start(); `onmessage`
  // would have implied it. This is the classic silent-nothing-happens bug.
  port.start();
}

const scope = self as unknown as {
  onconnect?: ((event: MessageEvent) => void) | null;
  addEventListener: typeof addEventListener;
  postMessage?: (message: unknown) => void;
};

if (typeof SharedWorkerGlobalScope !== "undefined" && self instanceof SharedWorkerGlobalScope) {
  scope.onconnect = (event: MessageEvent) => {
    const port = (event as unknown as { ports: MessagePort[] }).ports[0];
    if (port) wire(port);
  };
} else {
  // Dedicated worker: the global scope IS the port.
  const selfPort: PortLike = {
    postMessage: (message) => (scope.postMessage as (m: unknown) => void)(message),
  };
  hub.connect(selfPort);
  scope.addEventListener("message", (event) => {
    hub.handle(selfPort, (event as MessageEvent<WorkerRequest>).data);
  });
}
