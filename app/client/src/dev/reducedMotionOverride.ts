/**
 * A reduced-motion toggle for the gallery, and an honest account of what it
 * does and does not cover.
 *
 * WHAT IT DOES: patches `window.matchMedia` for the
 * `prefers-reduced-motion: reduce` query only, so
 * `usePrefersReducedMotion()` — the hook every signature animation is
 * supposed to consult — returns the forced value and fires a `change` at its
 * listeners. That is the JS path, and it is the path a view's own animation
 * logic runs on.
 *
 * WHAT IT DOES NOT DO: it cannot change what the browser thinks about CSS.
 * The `@media (prefers-reduced-motion: reduce)` block in `index.css` — the
 * one that stops `.bar-indeterminate` and `.live-dot` — is evaluated by the
 * engine against the real OS setting, and no amount of JS moves it. For that,
 * DevTools > Rendering > "Emulate CSS media feature
 * prefers-reduced-motion". The gallery says so on screen rather than letting
 * a reviewer conclude a CSS animation ignores the preference when in fact the
 * toggle never reached it.
 *
 * Every query other than the reduced-motion one is passed straight through to
 * the real implementation, so nothing else in the app changes behaviour while
 * the override is on.
 */

const QUERY = "(prefers-reduced-motion: reduce)";

type NativeMatchMedia = (query: string) => MediaQueryList;

/** Minimal but real `MediaQueryList`: an `EventTarget`, so `addEventListener`
 *  and `dispatchEvent` are the genuine article rather than a hand-rolled
 *  listener list that behaves subtly differently. */
class ForcedMediaQueryList extends EventTarget {
  media: string;
  matches: boolean;
  onchange: ((this: MediaQueryList, ev: MediaQueryListEvent) => unknown) | null = null;

  constructor(media: string, matches: boolean) {
    super();
    this.media = media;
    this.matches = matches;
  }

  // The deprecated pair. Present because some libraries still feature-detect
  // on it and a partial MediaQueryList is worse than none.
  addListener(listener: (ev: MediaQueryListEvent) => void): void {
    this.addEventListener("change", listener as EventListener);
  }

  removeListener(listener: (ev: MediaQueryListEvent) => void): void {
    this.removeEventListener("change", listener as EventListener);
  }
}

let installed = false;
/** The real implementation, or null where there is none — jsdom has no
 *  `matchMedia`, which `usePrefersReducedMotion` already copes with and this
 *  therefore has to as well. */
let native: NativeMatchMedia | null = null;
/** One shared list per patched query: the hook holds whatever it was given at
 *  mount, so handing out fresh objects would leave earlier consumers deaf. */
let shared: ForcedMediaQueryList | null = null;
let forced = false;

function changeEvent(matches: boolean): Event {
  if (typeof MediaQueryListEvent === "function") {
    return new MediaQueryListEvent("change", { matches, media: QUERY });
  }
  return new Event("change");
}

function isReducedMotionQuery(query: string): boolean {
  return query.replace(/\s+/g, "").includes("prefers-reduced-motion:reduce");
}

function install(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;
  native = typeof window.matchMedia === "function" ? window.matchMedia.bind(window) : null;
  const real = native;
  window.matchMedia = ((query: string): MediaQueryList => {
    if (!isReducedMotionQuery(query)) {
      if (real !== null) return real(query);
      // Nothing to delegate to. A permanently non-matching list is the least
      // surprising stand-in, and it is what jsdom's absence already means.
      return new ForcedMediaQueryList(query, false) as unknown as MediaQueryList;
    }
    if (shared === null) shared = new ForcedMediaQueryList(QUERY, forced);
    return shared as unknown as MediaQueryList;
  }) as typeof window.matchMedia;
}

/** Whether the toggle is currently forcing the preference on. */
export function isReducedMotionForced(): boolean {
  return forced;
}

/**
 * Force the JS-visible preference on or off.
 *
 * Call this BEFORE the render that shows the affected views: components that
 * already mounted hold the real `MediaQueryList` and will not see the patch.
 * The gallery handles that by remounting its content under a new key.
 */
export function setReducedMotionOverride(on: boolean): void {
  if (typeof window === "undefined") return;
  forced = on;
  install();
  // Turning the override OFF returns to the truth, which may itself be true
  // on a machine that has the OS setting on.
  const effective = on || (native !== null && native(QUERY).matches);
  if (shared !== null && shared.matches !== effective) {
    shared.matches = effective;
    shared.dispatchEvent(changeEvent(effective));
  }
}

/** Put the real `matchMedia` back — or remove the stand-in entirely where
 *  there was none. Exists so a test can leave the global as it found it. */
export function restoreMatchMedia(): void {
  if (installed && typeof window !== "undefined") {
    if (native !== null) {
      window.matchMedia = native as typeof window.matchMedia;
    } else {
      delete (window as { matchMedia?: unknown }).matchMedia;
    }
  }
  installed = false;
  native = null;
  shared = null;
  forced = false;
}
