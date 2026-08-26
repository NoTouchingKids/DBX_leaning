import { Suspense, lazy, useEffect, useState, type ReactNode } from "react";

import { usePrefersReducedMotion } from "@/components/models/useReducedMotion";
import { hasWebGl } from "./webgl";

/**
 * Everything that stands between a WebGL scene and the rest of the app.
 *
 * Three things it guarantees, each of which is a way the hero could otherwise
 * take the whole page down with it:
 *
 * **The chunk is lazy.** `three` is ~600 KB before gzip against a main bundle
 * already at 1.1 MB, and it is needed on exactly one route. `lazy()` puts it
 * in its own chunk that a user who never lands on `/` never downloads, and
 * `vite.config.ts` keeps it out of the main chunk. If this ever becomes a
 * static import the whole app pays for the hero.
 *
 * **WebGL may not be there.** A locked-down corporate browser, a VM with no
 * GPU, a headless Chromium taking screenshots — all real, all render nothing
 * and some throw. The context is probed once, up front, and the fallback is
 * rendered instead of a black rectangle.
 *
 * **Reduced motion is honoured before the download, not inside the scene.**
 * A user who has asked the OS for less movement should not pay 600 KB to be
 * shown a still frame; the fallback is served and the chunk is never
 * requested. That is the one case where reduced motion removes information —
 * and it removes none, because the scene is decorative by construction.
 */

const Scene = lazy(() => import("./HeroScene"));

export function SceneBoundary({ fallback }: { fallback: ReactNode }) {
  const reduced = usePrefersReducedMotion();
  const [failed, setFailed] = useState(false);
  const [mounted, setMounted] = useState(false);

  // Mount on a second frame, not the first. The hero is decorative and the
  // page's own text should paint first; requesting a 600 KB chunk during the
  // initial render competes with it for exactly no benefit.
  useEffect(() => {
    const id = requestAnimationFrame(() => setMounted(true));
    return () => cancelAnimationFrame(id);
  }, []);

  if (reduced || failed || !hasWebGl() || !mounted) return <>{fallback}</>;

  return (
    <SceneErrorBoundary onError={() => setFailed(true)} fallback={fallback}>
      <Suspense fallback={fallback}>
        <Scene />
      </Suspense>
    </SceneErrorBoundary>
  );
}

/**
 * A class component, because an error boundary can only be one.
 *
 * Scoped to the scene alone: a driver crash, a shader that will not compile
 * on some GPU, an OOM on a large texture — none of which should do more than
 * fall back to the static hero. Without this they unmount the whole route.
 */
import { Component, type ErrorInfo } from "react";

class SceneErrorBoundary extends Component<
  { children: ReactNode; fallback: ReactNode; onError: () => void },
  { crashed: boolean }
> {
  state = { crashed: false };

  static getDerivedStateFromError() {
    return { crashed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Logged, not swallowed: a GPU that cannot run this is worth knowing
    // about, and the user has already been given the fallback by then.
    console.warn("[hero] WebGL scene failed, falling back", error, info.componentStack);
    this.props.onError();
  }

  render() {
    return this.state.crashed ? <>{this.props.fallback}</> : <>{this.props.children}</>;
  }
}
