/**
 * The page <-> worker protocol.
 *
 * Deliberately NOT the server envelope. A `Message` is what a run emits; a
 * `WorkerEvent` is what the worker tells a tab about the *transport* — which
 * connections exist, whether they are healthy, and where the seq stream has
 * holes. Conflating the two is how a UI ends up rendering "reconnecting" as
 * if it were a run state.
 */

import type { Message, RunStatus } from "@/lib/envelope";

/** What a run's live channel is doing, as far as this browser can tell. */
export type ConnectionState =
  /** No EventSource — the run is terminal, or nobody is watching. */
  | "idle"
  /** Opening, or the browser is auto-retrying after a cut. */
  | "connecting"
  /** Open and receiving. */
  | "open"
  /** Consecutive failures exceeded the cap; we stopped trying. Not a run
   *  state — the run may well still be going. */
  | "failed";

export type WorkerRequest =
  /**
   * Watch a run.
   *
   * `terminal` is the caller's knowledge, and **`undefined` is a third state,
   * not a synonym for `false`**: it means "not known yet", and such a
   * subscription hydrates from IndexedDB while opening no channel until
   * `run-terminality` answers. Guessing `false` here is what opened a live
   * channel to runs that had already finished.
   */
  | { kind: "subscribe"; run_id: string; terminal?: boolean }
  | { kind: "unsubscribe"; run_id: string }
  /**
   * The answer to "is this run over", once the subscriber has it.
   *
   * `subscribe` asks what the caller knows AT THAT MOMENT, and on a cold page
   * that is "nothing yet" — the run row has not loaded. Re-subscribing when
   * the answer arrives is not an option: it would tear down and rebuild a
   * live run's stream every time it finished.
   *
   * So an unknown subscription hydrates from IndexedDB but **opens no
   * channel**, and this message releases it. `terminal: false` opens one;
   * `terminal: true` closes any that exists and prevents one being opened.
   *
   * A hint that merely *closed* a channel after the fact was tried first and
   * does not work: `GET /api/runs/{id}` is a network round trip while the
   * IndexedDB hydrate is sub-millisecond, so the channel is always already
   * open by the time the answer lands. Measured in a real browser — it is
   * why this gates rather than chases.
   */
  | { kind: "run-terminality"; run_id: string; terminal: boolean }
  /** Liveness. SharedWorker ports have no reliable close event, so tabs
   *  announce themselves and the worker prunes the ones that go quiet. */
  | { kind: "ping" }
  /** Sent on pagehide — the polite version of going quiet. */
  | { kind: "bye" };

export type WorkerEvent =
  /**
   * Everything already in IndexedDB for this run, sent once on subscribe,
   * before any live batch. Cheap history for a reopened tab; it is NOT a
   * backfill and may be empty or full of holes.
   */
  | { kind: "hydrate"; run_id: string; messages: Message[] }
  /** A coalesced batch of live messages, in arrival order. */
  | { kind: "batch"; run_id: string; messages: Message[] }
  | { kind: "state"; run_id: string; state: ConnectionState; consecutive_failures: number }
  /**
   * The seq stream skipped. NOT necessarily a fault, and NOT always
   * closeable — see `hub.ts`. Advisory: never auto-backfill on it.
   */
  | { kind: "gap"; run_id: string; from: number; to: number }
  /** A terminal status arrived; the worker has closed the channel itself. */
  | { kind: "terminal"; run_id: string; status: RunStatus };
