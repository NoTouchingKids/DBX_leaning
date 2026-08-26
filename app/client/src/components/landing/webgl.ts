/**
 * Is there a WebGL context to be had?
 *
 * Its own module rather than a second export from `SceneBoundary.tsx`, for a
 * mundane reason worth stating: React Fast Refresh only re-renders a file that
 * exports components and nothing else, so a helper living beside one costs
 * every edit to that file a full reload.
 */

/** Probed once. Creating a context is not free and the answer cannot change
 *  within a page load. */
let webglSupported: boolean | null = null;

export function hasWebGl(): boolean {
  if (webglSupported !== null) return webglSupported;
  if (typeof document === "undefined") {
    webglSupported = false;
    return false;
  }
  try {
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
    webglSupported = gl !== null;
    // Release it immediately. Browsers cap the number of live contexts (~16
    // in Chrome) and leaking the probe would cost the scene its own.
    const lose = (gl as WebGLRenderingContext | null)?.getExtension("WEBGL_lose_context");
    lose?.loseContext();
  } catch {
    webglSupported = false;
  }
  return webglSupported;
}
