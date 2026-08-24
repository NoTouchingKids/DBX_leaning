/**
 * A controllable stand-in for `EventSource`.
 *
 * jsdom does not implement EventSource, and even where it does, the
 * behaviour that matters here — a cut mid-stream, the browser's own retry,
 * a hard close — is not something a real one can be made to do on cue. The
 * hub takes a factory precisely so this can exist.
 */

import { ES_CLOSED, ES_CONNECTING, ES_OPEN, type EventSourceLike } from "../hub";

export class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = [];

  readyState = ES_CONNECTING;
  closed = false;
  private readonly listeners = new Map<string, Array<(event: MessageEvent) => void>>();

  readonly url: string;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  static reset(): void {
    FakeEventSource.instances = [];
  }

  static get last(): FakeEventSource {
    const found = FakeEventSource.instances.at(-1);
    if (!found) throw new Error("no EventSource was created");
    return found;
  }

  addEventListener(type: string, listener: (event: MessageEvent) => void): void {
    const bucket = this.listeners.get(type) ?? [];
    bucket.push(listener);
    this.listeners.set(type, bucket);
  }

  close(): void {
    this.closed = true;
    this.readyState = ES_CLOSED;
  }

  private dispatch(type: string, data?: unknown): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data } as MessageEvent);
    }
  }

  /* -- what a test drives ------------------------------------------- */

  open(): void {
    this.readyState = ES_OPEN;
    this.dispatch("open");
  }

  /** A named frame, as the server sends it: `event: <type>`, JSON `data`. */
  emit(payload: Record<string, unknown>): void {
    this.dispatch(String(payload.type), JSON.stringify(payload));
  }

  /** A frame that will not parse. */
  emitRaw(type: string, data: string): void {
    this.dispatch(type, data);
  }

  /** The ingress cut a healthy connection. The browser will retry itself, so
   *  readyState goes back to CONNECTING and this object stays alive. */
  cut(): void {
    this.readyState = ES_CONNECTING;
    this.dispatch("error");
  }

  /** A hard failure — non-2xx, wrong content type. The browser gives up on
   *  this instance and it is ours to replace. */
  fail(): void {
    this.readyState = ES_CLOSED;
    this.dispatch("error");
  }
}
