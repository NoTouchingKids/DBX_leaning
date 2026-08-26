/**
 * Hero variant A — CONVERGENCE.
 *
 * Same idea as `HeroScene.tsx`: eleven cohorts of candidate solutions stream in
 * from the edges of the frame, narrow onto a resolution node, and the node
 * pings as the packet lands. The engineering is lifted from that file almost
 * unchanged — the popped teardown stack, the visibility pause, the clamped
 * clock, the rethrow into `SceneErrorBoundary` — because none of it was what
 * was wrong.
 *
 * What is different, and why:
 *
 * **Positions are stored in FRAME units, not world units.** `x` and `y` are
 * fractions of the visible frame at that vertex's own depth (±1 is the edge),
 * and every shader converts with the same two helpers. The old scene picked
 * world coordinates by hand and then fought the projection: at a 3:1 hero
 * aspect the nodes covered 13% of the available height, which is exactly the
 * "collapses into a thin band" fault. Here a node at `y = 0.6` is at 60% of
 * the way to the top edge *by construction*, at any aspect ratio, on any
 * screen. It also removes the camera-pullback fudge the old file needed for
 * narrow viewports — the frame is correct at aspect 1 and aspect 4 alike.
 *
 * The cost is that depth no longer moves anything on screen. Depth is carried
 * by four other cues instead: size, haze, parallax under the camera truck, and
 * the scale of each cohort's cloud. That is enough, and it buys a composition
 * that cannot fall out of frame.
 *
 * **Quads, not points.** Every particle, node and ring is an instanced quad
 * billboarded in view space. `gl_PointSize` is capped by the driver (~64 on
 * some mobile GPUs, and oversized points get dropped rather than clamped), and
 * `gl_PointCoord` is the thing most likely to misbehave under a software
 * renderer. A quad has neither problem, gives sub-pixel-accurate round sprites
 * from its own interpolated corner, and costs three extra vertices each —
 * 21k vertices total, which is nothing.
 *
 * **The palette is blue on purpose.** The old scene used `--c-faint` for the
 * search colour. That token is grey by design, which is why the hero read as
 * grey. Near and flash still come from `--c-accent` / `--c-accent-ink` so the
 * brand blue stays single-sourced; only the far/search colour is stated here,
 * because the token ramp has no saturated mid-blue to borrow.
 *
 * ## Budget
 *
 * Three draw calls: rails (`LineSegments`), the particle field (5,166
 * instanced quads), and the nodes (33 instanced quads — eleven cores and two
 * expanding rings each). Per-frame CPU work is one uniform, 33 floats and a
 * camera translate; all motion is closed-form in the vertex shader.
 *
 * Reduced motion is not re-checked here — `SceneBoundary` answers it before
 * this chunk is ever downloaded.
 */

import { useEffect, useRef, useState } from "react";

import { DURATION } from "@/components/models/motion";

/** One lane per model on the platform today. */
const LANES = 11;
/** Packets in flight per lane, and points per packet. */
const COHORTS = 7;
const PER_COHORT = 58;
const STREAM_COUNT = LANES * COHORTS * PER_COHORT;
/**
 * Static deep-field motes, drawn from the same buffer as the streams. They are
 * what stops the corners between the lanes reading as empty black. Free: they
 * are just instances with `aSpeed = 0`, so they never travel and never flash,
 * and they cost no extra draw call and no extra branch in the shader.
 */
const MOTE_COUNT = 700;
const PARTICLE_COUNT = STREAM_COUNT + MOTE_COUNT;

/** Enough segments for the rail's alpha ramp to look continuous. */
const RAIL_SEGMENTS = 18;
/** A rail is only drawn over the last stretch of its lane; see `RAIL_FROM`. */
const RAIL_FROM = 0.28;

/** Eleven cores plus two rings each. */
const NODE_COUNT = LANES * 3;

/**
 * The camera never moves in z, and every frame-unit conversion is relative to
 * this. It is a uniform rather than a literal so the two are provably the same
 * number.
 */
const CAM_Z = 6.4;
const FOV = 46;
const TAN_HALF_FOV = Math.tan((FOV / 2) * (Math.PI / 180));

/** Golden angle. Eleven of anything spaced by this never forms a visible
 *  rhythm, which is what an evenly-divided circle always does. */
const GOLDEN = 2.399963;

/**
 * Where in a point's cycle the landing flash peaks, and the correction that
 * keeps the ring on the same instant.
 *
 * A cohort is brightest just *before* it reaches its node — a point that
 * flashes at p = 1 has already left. The ping counter runs COHORTS times
 * faster than a point's phase, so a flash at `LAND_PEAK` lands at
 * `fract(LAND_PEAK * COHORTS)` on the ping's own clock, not at zero. Without
 * `PING_LEAD` the ring fires a fraction of a second after the packet it is
 * supposed to be announcing, which is exactly long enough to read as two
 * unrelated events. Derived rather than typed in, so retuning `LAND_PEAK`
 * cannot desynchronise them.
 */
const LAND_PEAK = 0.962;
const PING_LEAD = 1 - ((LAND_PEAK * COHORTS) % 1);

/**
 * Two tokens, one constant, per theme.
 *
 * `--c-accent` and `--c-accent-ink` are the brand blue and its emphatic end,
 * and they point the right way in both themes: on dark the ramp brightens
 * toward resolution, on light it darkens. Either direction reads as "this one
 * is done", and reading them keeps `index.css` the single source of truth.
 *
 * The search colour cannot come from the ramp. `--c-faint` is a neutral grey —
 * correct for de-emphasised *text*, and the direct cause of a hero that read
 * as dust rather than as the product's colour. So it is stated, per theme, as
 * the only literal in the palette.
 */
const TOKEN_NEAR = "--c-accent";
/**
 * The landing flash, on LIGHT only.
 *
 * `--c-accent-ink` is the emphatic end of the ramp, which on light means
 * DARKER — a saturated navy, and over white a dark dense mark is exactly what
 * a flash should be.
 */
const TOKEN_FLASH_LIGHT = "--c-accent-ink";
/**
 * The landing flash, on DARK, and NOT from the ramp — this was measured off a
 * screenshot rather than reasoned about.
 *
 * `--c-accent-ink` is `#b2ccff` in dark: rgb(0.70, 0.80, 1.00). Under additive
 * blending a cohort of 58 quads overlaps several deep at the moment of
 * landing, and a colour already that high in red and green saturates all three
 * channels within about two layers — so the climax of the whole animation
 * rendered as a soft GREY cloud, the one thing the brief was trying to fix.
 *
 * Hue survives accumulation only if the channels are far apart. This is
 * rgb(0.24, 0.53, 1.00): red still has four times the headroom of blue, so the
 * stack brightens through azure and reaches white only at the very core, which
 * is what a hot centre should do anyway.
 */
const DARK_FLASH = "#3d87ff";
/** Royal blue. Additive over `#0c111d`, a cohort of these reads unambiguously
 *  blue at the low alpha the far field needs. */
const DARK_FAR = "#2f62e6";
/** Pale sky. On white the far field can only be built out of *lightness*, so
 *  distance is a wash of blue rather than a dimming toward the ground. */
const LIGHT_FAR = "#8fb3f5";

/**
 * Blending genuinely has to differ by theme, and so does gain.
 *
 * Additive is what makes overlapping points glow on a near-black ground and is
 * worthless on a near-white one — on paper it can only move a pixel toward
 * white, so the scene disappears. Light gets normal blending, where density
 * reads as *darker* instead, and slightly lower gain because a saturated blue
 * at 0.5 alpha on white is already a strong mark.
 */
interface Blend {
  additive: boolean;
  field: number;
  rail: number;
  node: number;
}
const DARK_BLEND: Blend = { additive: true, field: 1.0, rail: 0.6, node: 1.0 };
const LIGHT_BLEND: Blend = { additive: false, field: 0.9, rail: 0.62, node: 0.95 };

interface Lane {
  /** Frame coords (x, y in ±1 frame fractions) plus world z. */
  far: [number, number, number];
  node: [number, number, number];
  /** Cycles per second. One cycle is one point's whole journey. */
  speed: number;
  /** Phase at t = 0, so the eleven lanes do not beat in unison. */
  offset: number;
  /** Apparent size multiplier for the node's marker, from its depth. Sizes are
   *  frame-relative and therefore depth-independent by default, so a deep node
   *  has to be shrunk explicitly or the depth cue is lost. */
  scale: number;
}

/**
 * Seeded, so the hero is the same picture on every load. A layout that
 * reshuffles each refresh has no identity — it looks generated, which is the
 * opposite of the intent. mulberry32, with the published constants.
 */
function rng(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/**
 * The frame-unit contract, shared verbatim by all three vertex shaders.
 *
 * `frameToWorld` maps (frame x, frame y, world z) to world space such that
 * ±1 lands exactly on the frame edge at that depth. `frameSpan` is the same
 * conversion for a *size*, using the height scale on both axes so a quad comes
 * out circular in pixels rather than stretched by the aspect.
 */
const FRAME_GLSL = /* glsl */ `
  uniform float uTanH;
  uniform float uAspect;
  uniform float uCamZ;

  vec3 frameToWorld(vec3 f) {
    float d = uCamZ - f.z;
    return vec3(f.x * uTanH * d * uAspect, f.y * uTanH * d, f.z);
  }

  float frameSpan(float s, float depth) {
    return s * uTanH * depth;
  }
`;

const FIELD_VERT = /* glsl */ `
  ${FRAME_GLSL}

  uniform float uTime;
  uniform float uSizeFar;
  uniform float uSizeNear;
  uniform float uGain;
  uniform vec3 uFar;
  uniform vec3 uNear;
  uniform vec3 uFlash;

  attribute vec3 aFar;
  attribute vec3 aNode;
  attribute vec3 aSpread;
  attribute float aPhase;
  attribute float aSpeed;
  attribute float aSeed;

  varying vec3 vColor;
  varying vec3 vHot;
  varying float vAlpha;
  varying float vHeat;
  varying vec2 vQuad;

  void main() {
    float p = fract(aPhase + uTime * aSpeed);

    // Quick across the empty distance, easing into the node. Perspective
    // already compresses motion at the far end, so linear travel would read as
    // a rush at the viewer and the packet would never be seen to settle.
    float travel = 1.0 - pow(1.0 - p, 1.5);

    // How much of the search is still open. A smoothstep rather than the power
    // curve the first version used: a power curve starts collapsing
    // immediately, so the cohort is already narrow by the time it is anywhere
    // near its node and the whole journey reads as sliding. This holds the
    // cloud at full width for the first sixth of the trip and then pinches it
    // shut over the last stretch, which is what makes it read as *narrowing*.
    float open = 0.03 + 0.97 * (1.0 - smoothstep(0.14, 0.90, p));

    vec3 pos = mix(frameToWorld(aFar), frameToWorld(aNode), travel);

    // The cloud's own extent, sized in frame units at the far plane so it is a
    // fixed fraction of the screen rather than a world-space blob that
    // perspective shrinks to nothing.
    float dFar = uCamZ - aFar.z;
    float sway = uTime * (0.30 + aSeed * 0.55) + aSeed * 43.0;
    pos.x += (aSpread.x + sin(sway) * 0.11) * uTanH * dFar * open;
    pos.y += (aSpread.y + cos(sway * 1.31 + 1.1) * 0.08) * uTanH * dFar * open;
    // z stays in world units, and deliberately breaks the exact frame mapping
    // above by a few percent — that error is the cloud having real thickness.
    pos.z += aSpread.z * open;

    vec4 mv = modelViewMatrix * vec4(pos, 1.0);
    float depth = max(-mv.z, 0.25);

    // Deliberately narrow. A wide ramp spreads the ignition over two or three
    // seconds of a twenty-second cycle and the cohort just gets gradually
    // brighter; keeping it inside the same stretch where \`open\` pinches shut
    // makes narrowing and igniting one gesture instead of two.
    float land = smoothstep(0.855, ${LAND_PEAK.toFixed(3)}, p);
    float gone = smoothstep(${LAND_PEAK.toFixed(3)}, 1.0, p);
    float live = 1.0 - gone;

    // Frame-relative sizes are depth-independent, which would flatten the
    // scene completely. A partial perspective term puts the depth cue back
    // without letting the far field collapse into the 1px dust it was.
    float persp = mix(1.0, 12.0 / depth, 0.45);
    float size = mix(uSizeFar, uSizeNear, travel) * persp * (1.0 + 2.2 * land * live);

    mv.xy += position.xy * frameSpan(size, depth);
    gl_Position = projectionMatrix * mv;

    // Depth haze. THREE.Fog does not reach a ShaderMaterial. The floor matters
    // as much as the ramp: without it the deep field vanishes and the frame
    // empties out again.
    float haze = 0.34 + 0.66 * smoothstep(-54.0, -18.0, mv.z);

    vColor = mix(mix(uFar, uNear, smoothstep(0.04, 0.66, p)), uFlash, land * live);
    vHot = uFlash;
    vHeat = 0.35 + 0.65 * land * live;
    vAlpha = uGain * haze * smoothstep(0.0, 0.04, p) * live * (0.72 + 0.85 * land);
    vQuad = position.xy;
  }
`;

const FIELD_FRAG = /* glsl */ `
  varying vec3 vColor;
  varying vec3 vHot;
  varying float vAlpha;
  varying float vHeat;
  varying vec2 vQuad;

  void main() {
    float r2 = dot(vQuad, vQuad);
    if (r2 > 1.0) discard;

    float f = 1.0 - r2;
    float a = vAlpha * f * f;
    if (a <= 0.003) discard;

    // Every particle carries a hotter centre, and the ones nearing their node
    // carry a much hotter one. This is the difference between a field of flat
    // discs and a field of things that are lit.
    vec3 c = mix(vColor, vHot, (1.0 - smoothstep(0.0, 0.55, r2)) * vHeat * 0.55);
    gl_FragColor = vec4(c, min(a, 1.0));
    #include <colorspace_fragment>
  }
`;

const RAIL_VERT = /* glsl */ `
  ${FRAME_GLSL}

  attribute vec3 aTo;
  attribute float aT;
  attribute float aAlpha;

  varying float vA;

  void main() {
    // Interpolated in world space after conversion, not before — the same
    // order the field shader uses, so a rail lies exactly under the path its
    // packets fly rather than beside it.
    vec3 p = mix(frameToWorld(position), frameToWorld(aTo), aT);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
    vA = aAlpha;
  }
`;

const RAIL_FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform float uGain;
  varying float vA;

  void main() {
    gl_FragColor = vec4(uColor, vA * uGain);
    #include <colorspace_fragment>
  }
`;

const NODE_VERT = /* glsl */ `
  ${FRAME_GLSL}

  uniform float uCoreSize;
  uniform float uRingSize;
  uniform vec3 uNear;
  uniform vec3 uFlash;

  attribute vec3 aPos;
  attribute float aScale;
  attribute float aRole;
  attribute float aU;

  varying vec3 vTint;
  varying float vGain;
  varying float vRole;
  varying float vU;
  varying vec2 vQuad;

  void main() {
    vec4 mv = modelViewMatrix * vec4(frameToWorld(aPos), 1.0);
    float depth = max(-mv.z, 0.25);

    float size;
    float gain;
    vec3 tint;
    float u = aU;

    if (aRole < 0.5) {
      // The node itself: always lit, so the eleven destinations are structure
      // rather than something that only exists for the instant of a ping.
      float flare = exp(-7.0 * u);
      size = uCoreSize * aScale * (1.0 + 0.9 * flare);
      gain = 0.55 + 1.25 * flare;
      tint = mix(uNear, uFlash, flare);
    } else if (aRole < 1.5) {
      // Sizes here are the QUAD, not the ring: the band sits at 0.62 of it, so
      // the gaussian's outer tail has somewhere to die. Drawing the band at the
      // rim instead clips it mid-slope and every ping wears a hard circle.
      float e = 1.0 - pow(1.0 - u, 2.4);
      size = (0.07 + uRingSize * e) * aScale;
      gain = pow(1.0 - u, 2.1);
      // The ring cools as it spreads: hot at the instant of arrival, brand
      // blue by the time it is wide.
      tint = mix(uFlash, uNear, u);
    } else {
      // A second, slower ring, offset in phase. One ring is a notification;
      // two is a shockwave, and it is what makes the resolve read as an event
      // instead of an outline.
      float u2 = clamp((u - 0.12) / 0.88, 0.0, 1.0);
      float e = 1.0 - pow(1.0 - u2, 2.8);
      size = (0.055 + uRingSize * 0.60 * e) * aScale;
      gain = pow(1.0 - u2, 3.0) * 0.55;
      tint = uNear;
      u = u2;
    }

    mv.xy += position.xy * frameSpan(size, depth);
    gl_Position = projectionMatrix * mv;

    vTint = tint;
    vGain = gain;
    vRole = aRole;
    vU = u;
    vQuad = position.xy;
  }
`;

const NODE_FRAG = /* glsl */ `
  uniform float uGain;

  varying vec3 vTint;
  varying float vGain;
  varying float vRole;
  varying float vU;
  varying vec2 vQuad;

  void main() {
    float r2 = dot(vQuad, vQuad);
    if (r2 > 1.0) discard;

    float a;
    if (vRole < 0.5) {
      // Tight core plus a broad bloom, from the one quad. The bloom is windowed
      // to zero at the rim — exp alone is still at ~0.004 out there, which is a
      // faint hard-edged disc rather than a glow once the flare multiplies it.
      a = exp(-30.0 * r2) + 0.20 * exp(-4.0 * r2) * (1.0 - r2);
    } else {
      // The band's inner tail never reaches this far in; skipping it is most of
      // the ring's fill cost at full expansion. The width is capped so the
      // outer tail is spent (< 1/1000) before the quad rim.
      if (r2 < 0.05) discard;
      float w = 0.055 + 0.085 * (1.0 - vU);
      float t = (sqrt(r2) - 0.62) / w;
      a = exp(-t * t);
    }

    a *= vGain * uGain;
    if (a <= 0.003) discard;
    gl_FragColor = vec4(vTint, min(a, 1.0));
    #include <colorspace_fragment>
  }
`;

export default function HeroSceneA() {
  const host = useRef<HTMLDivElement>(null);
  const [fatal, setFatal] = useState<Error | null>(null);

  useEffect(() => {
    const el = host.current;
    if (el === null) return;
    let disposed = false;

    // Built up as each resource is created rather than assembled into one
    // closure at the end. Setup can throw part-way — `applyPalette` does it
    // deliberately when a token is missing — and a single trailing cleanup
    // function is never assigned when that happens, so the renderer, its GL
    // context and every buffer created before the throw survive the unmount
    // that follows. Popped rather than iterated, so releasing twice is a no-op
    // the second time.
    const teardown: Array<() => void> = [];
    const release = () => {
      while (teardown.length > 0) teardown.pop()?.();
    };

    void (async () => {
      try {
        const THREE = await import("three");
        if (disposed) return;

        // low-power: this is decoration. On a dual-GPU laptop there is no case
        // for waking the discrete card to draw five thousand dots.
        const renderer = new THREE.WebGLRenderer({
          antialias: true,
          alpha: true,
          powerPreference: "low-power",
        });
        renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
        renderer.setClearColor(0x000000, 0);
        // The canvas is sized by CSS, not by setSize's inline styles.
        // setSize(w, h, false) writes only the backing store; without these the
        // canvas would lay out at its attribute size — twice the intended width
        // on a retina screen. Keeping styles out of the resize path is also
        // what guarantees the ResizeObserver cannot feed itself.
        renderer.domElement.style.width = "100%";
        renderer.domElement.style.height = "100%";
        renderer.domElement.style.display = "block";
        el.appendChild(renderer.domElement);
        teardown.push(() => {
          // `dispose()` frees the buffers, programs and render lists; it does
          // NOT release the context. The context then lives until the canvas is
          // collected, which is not a schedule anything here controls, and
          // Chrome caps live contexts around 16. `forceContextLoss` is the only
          // way to hand it back now.
          renderer.forceContextLoss();
          renderer.dispose();
          renderer.domElement.remove();
        });

        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(FOV, 1, 0.5, 90);
        // Never re-oriented. A `lookAt` would rotate the camera and the
        // frame-unit maths above assumes an axis-aligned one; the drift below
        // is a pure lateral truck, which keeps that assumption exactly true and
        // still gives real parallax between the near nodes and the deep field.
        camera.position.set(0, 0, CAM_Z);

        const rnd = rng(0x5eed1d4e);

        /**
         * The billboard both instanced draws are built on: four corners in ±1,
         * offset in view space by the vertex shader. Declared here rather than
         * at module scope only so nothing in this file needs a `three` type in
         * a signature — a `typeof import("three")` annotation is erased by the
         * compiler, but it is one more thing for `chunk.test.ts` to have to be
         * right about, and this costs nothing.
         */
        const quad = () => {
          const geo = new THREE.InstancedBufferGeometry();
          geo.setAttribute(
            "position",
            new THREE.BufferAttribute(
              new Float32Array([-1, -1, 0, 1, -1, 0, 1, 1, 0, -1, 1, 0]),
              3,
            ),
          );
          geo.setIndex([0, 1, 2, 2, 3, 0]);
          return geo;
        };

        // ---- lanes -------------------------------------------------------
        const lanes: Lane[] = [];
        for (let i = 0; i < LANES; i += 1) {
          const t = i / (LANES - 1);
          // Nodes walk left to right and get *nearer* as they go. The left of
          // this hero sits under a near-opaque scrim in `HomePage`, so the deep,
          // small, hazy end of the run belongs there and the big hot end
          // belongs in the clear right half.
          const nodeZ = -3.6 + 5.2 * t + (rnd() - 0.5) * 1.4;
          const spoke = i * GOLDEN + 0.35;
          const reach = 0.58 + rnd() * 0.42;
          lanes.push({
            node: [
              -0.78 + 1.52 * t + (rnd() - 0.5) * 0.07,
              0.6 * Math.sin(i * GOLDEN + 0.9) + (rnd() - 0.5) * 0.09,
              nodeZ,
            ],
            // Far anchors ring the frame by golden angle, so the cohorts enter
            // from every edge and corner instead of from one side. Radius is
            // jittered inside the frame rather than outside it: an anchor
            // parked off-screen takes its whole cloud with it, which is how the
            // top two-thirds ended up empty last time.
            far: [Math.cos(spoke) * reach * 1.08, Math.sin(spoke) * reach, -17 - rnd() * 15],
            // Cadence out of the shared vocabulary rather than picked by feel:
            // a packet resolves per lane roughly every DURATION.ambient
            // seconds, spread ±30%.
            speed: 1 / (DURATION.ambient * (0.75 + rnd() * 0.85) * COHORTS),
            offset: rnd(),
            scale: Math.pow((CAM_Z - 1.6) / (CAM_Z - nodeZ), 0.55),
          });
        }

        // ---- the field ---------------------------------------------------
        const far = new Float32Array(PARTICLE_COUNT * 3);
        const node = new Float32Array(PARTICLE_COUNT * 3);
        const spread = new Float32Array(PARTICLE_COUNT * 3);
        const phase = new Float32Array(PARTICLE_COUNT);
        const speed = new Float32Array(PARTICLE_COUNT);
        const seed = new Float32Array(PARTICLE_COUNT);

        // Triangular rather than uniform: a cloud with a dense core and a thin
        // fringe, which is what a search looks like. Uniform gives a slab.
        const bell = () => rnd() + rnd() - 1;

        let n = 0;
        for (const lane of lanes) {
          for (let cohort = 0; cohort < COHORTS; cohort += 1) {
            for (let k = 0; k < PER_COHORT; k += 1) {
              far.set(lane.far, n * 3);
              node.set(lane.node, n * 3);
              // Wider than tall, because the hero is: a circular cloud in a 3:1
              // letterbox leaves visible gaps between the lanes.
              spread[n * 3] = bell() * 0.78;
              spread[n * 3 + 1] = bell() * 0.36;
              spread[n * 3 + 2] = bell() * 3.2;
              // Cohort members share a phase, which is what makes them arrive
              // as a packet. The jitter is small enough not to break that and
              // large enough that the leading edge is ragged, not a wall.
              phase[n] = lane.offset + cohort / COHORTS + (rnd() - 0.5) * 0.012;
              speed[n] = lane.speed;
              seed[n] = rnd();
              n += 1;
            }
          }
        }

        for (let m = 0; m < MOTE_COUNT; m += 1) {
          const at: [number, number, number] = [
            (rnd() * 2 - 1) * 1.15,
            (rnd() * 2 - 1) * 1.05,
            -26 - rnd() * 20,
          ];
          // far === node, so `travel` moves them nowhere however it is eased.
          far.set(at, n * 3);
          node.set(at, n * 3);
          spread[n * 3] = bell() * 0.08;
          spread[n * 3 + 1] = bell() * 0.06;
          spread[n * 3 + 2] = bell() * 3;
          // Parked early in the cycle: past the fade-in, far short of `land`,
          // so a mote is always fully drawn and never flashes.
          phase[n] = 0.06 + rnd() * 0.05;
          speed[n] = 0;
          seed[n] = rnd();
          n += 1;
        }

        const fieldGeo = quad();
        fieldGeo.instanceCount = PARTICLE_COUNT;
        fieldGeo.setAttribute("aFar", new THREE.InstancedBufferAttribute(far, 3));
        fieldGeo.setAttribute("aNode", new THREE.InstancedBufferAttribute(node, 3));
        fieldGeo.setAttribute("aSpread", new THREE.InstancedBufferAttribute(spread, 3));
        fieldGeo.setAttribute("aPhase", new THREE.InstancedBufferAttribute(phase, 1));
        fieldGeo.setAttribute("aSpeed", new THREE.InstancedBufferAttribute(speed, 1));
        fieldGeo.setAttribute("aSeed", new THREE.InstancedBufferAttribute(seed, 1));

        // Shared by reference across all three materials, so one write updates
        // every shader that needs it.
        const frameUniforms = {
          uTanH: { value: TAN_HALF_FOV },
          uAspect: { value: 1 },
          uCamZ: { value: CAM_Z },
        };
        const colorFar = new THREE.Color();
        const colorNear = new THREE.Color();
        const colorFlash = new THREE.Color();

        // Held as named objects rather than reached through `material.uniforms`
        // so every write below is type-checked; the uniforms map is a string
        // index signature and reads out of it are `IUniform | undefined`.
        const fieldUniforms = {
          ...frameUniforms,
          uTime: { value: 0 },
          // Frame-height fractions, i.e. the quad's half-extent as a share of
          // half the canvas height. On a 416px hero that is a ~6px dot in the
          // deep field and ~7px mid-flight, opening past 25px at the instant a
          // packet lands. The old scene's were 1–3px, which is why it read as
          // dust; these are marks.
          uSizeFar: { value: 0.019 },
          uSizeNear: { value: 0.013 },
          uGain: { value: 1 },
          uFar: { value: colorFar },
          uNear: { value: colorNear },
          uFlash: { value: colorFlash },
        };

        const fieldMat = new THREE.ShaderMaterial({
          vertexShader: FIELD_VERT,
          fragmentShader: FIELD_FRAG,
          uniforms: fieldUniforms,
          transparent: true,
          // DoubleSide removes any chance of a winding mistake making the whole
          // field invisible; forceSinglePass is what stops three rendering a
          // transparent DoubleSide material twice, which would double every
          // draw call and every fragment in this file.
          side: THREE.DoubleSide,
          forceSinglePass: true,
          // Everything here is transparent and nothing occludes anything, so
          // the depth buffer could only ever produce a wrong answer. Order is
          // fixed by renderOrder instead.
          depthTest: false,
          depthWrite: false,
        });

        teardown.push(() => {
          fieldGeo.dispose();
          fieldMat.dispose();
        });

        const field = new THREE.Mesh(fieldGeo, fieldMat);
        // The shader places every vertex; a bounding sphere derived from the
        // corner attribute would cull the entire object on the first frame.
        field.frustumCulled = false;
        field.renderOrder = 1;
        scene.add(field);

        // ---- rails -------------------------------------------------------
        const railFrom = new Float32Array(LANES * RAIL_SEGMENTS * 2 * 3);
        const railTo = new Float32Array(LANES * RAIL_SEGMENTS * 2 * 3);
        const railT = new Float32Array(LANES * RAIL_SEGMENTS * 2);
        const railAlpha = new Float32Array(LANES * RAIL_SEGMENTS * 2);

        let v = 0;
        for (const lane of lanes) {
          for (let s = 0; s < RAIL_SEGMENTS; s += 1) {
            for (const step of [s / RAIL_SEGMENTS, (s + 1) / RAIL_SEGMENTS]) {
              railFrom.set(lane.far, v * 3);
              railTo.set(lane.node, v * 3);
              // Only the last stretch is drawn at all. Eleven full-length rails
              // from a ring of anchors to a row of nodes is a cat's cradle;
              // eleven short stubs leading into their nodes read as approach
              // vectors and never clutter the middle of the frame.
              railT[v] = RAIL_FROM + (1 - RAIL_FROM) * step;
              railAlpha[v] = Math.pow(step, 2.4);
              v += 1;
            }
          }
        }

        const railGeo = new THREE.BufferGeometry();
        railGeo.setAttribute("position", new THREE.BufferAttribute(railFrom, 3));
        railGeo.setAttribute("aTo", new THREE.BufferAttribute(railTo, 3));
        railGeo.setAttribute("aT", new THREE.BufferAttribute(railT, 1));
        railGeo.setAttribute("aAlpha", new THREE.BufferAttribute(railAlpha, 1));

        const railUniforms = {
          ...frameUniforms,
          uColor: { value: colorNear },
          uGain: { value: 1 },
        };
        const railMat = new THREE.ShaderMaterial({
          vertexShader: RAIL_VERT,
          fragmentShader: RAIL_FRAG,
          uniforms: railUniforms,
          transparent: true,
          depthTest: false,
          depthWrite: false,
        });

        teardown.push(() => {
          railGeo.dispose();
          railMat.dispose();
        });

        const rails = new THREE.LineSegments(railGeo, railMat);
        rails.frustumCulled = false;
        rails.renderOrder = 0;
        scene.add(rails);

        // ---- nodes and pings ---------------------------------------------
        // One draw for both: same quad, same radial shader, one branch on the
        // role. Splitting them would buy nothing but a fourth draw call.
        const nodePos = new Float32Array(NODE_COUNT * 3);
        const nodeScale = new Float32Array(NODE_COUNT);
        const nodeRole = new Float32Array(NODE_COUNT);
        const nodeU = new Float32Array(NODE_COUNT);

        lanes.forEach((lane, i) => {
          for (let role = 0; role < 3; role += 1) {
            const j = i * 3 + role;
            nodePos.set(lane.node, j * 3);
            nodeScale[j] = lane.scale;
            nodeRole[j] = role;
          }
        });

        const nodeGeo = quad();
        nodeGeo.instanceCount = NODE_COUNT;
        nodeGeo.setAttribute("aPos", new THREE.InstancedBufferAttribute(nodePos, 3));
        nodeGeo.setAttribute("aScale", new THREE.InstancedBufferAttribute(nodeScale, 1));
        nodeGeo.setAttribute("aRole", new THREE.InstancedBufferAttribute(nodeRole, 1));
        const nodeUAttr = new THREE.InstancedBufferAttribute(nodeU, 1);
        nodeUAttr.setUsage(THREE.DynamicDrawUsage);
        nodeGeo.setAttribute("aU", nodeUAttr);

        const nodeUniforms = {
          ...frameUniforms,
          // Frame-height fractions again: a ~31px core quad carrying a ~6px hot
          // centre, and a ring whose band expands to ~32% of the hero's
          // half-height — about 135px across on a 416px hero. That is the
          // "punchy", and it is in frame units so it is the same gesture on a
          // phone and on a 4K display rather than a different one on each.
          uCoreSize: { value: 0.15 },
          uRingSize: { value: 0.52 },
          uGain: { value: 1 },
          uNear: { value: colorNear },
          uFlash: { value: colorFlash },
        };
        const nodeMat = new THREE.ShaderMaterial({
          vertexShader: NODE_VERT,
          fragmentShader: NODE_FRAG,
          uniforms: nodeUniforms,
          transparent: true,
          side: THREE.DoubleSide,
          forceSinglePass: true,
          depthTest: false,
          depthWrite: false,
        });

        teardown.push(() => {
          nodeGeo.dispose();
          nodeMat.dispose();
        });

        const nodes = new THREE.Mesh(nodeGeo, nodeMat);
        nodes.frustumCulled = false;
        nodes.renderOrder = 2;
        scene.add(nodes);

        // ---- palette -----------------------------------------------------
        const dark = matchMedia("(prefers-color-scheme: dark)");
        const applyPalette = () => {
          const style = getComputedStyle(document.documentElement);
          const token = (name: string) => {
            const raw = style.getPropertyValue(name).trim();
            // An unset THREE.Color is white: invisible on light, wrong on dark.
            // Failing loudly hands the page back its static fallback, which is
            // a far better outcome than a scene nobody can see.
            if (raw === "") throw new Error(`hero: design token ${name} is not defined`);
            return raw;
          };

          const isDark = dark.matches;
          colorNear.setStyle(token(TOKEN_NEAR));
          colorFlash.setStyle(isDark ? DARK_FLASH : token(TOKEN_FLASH_LIGHT));
          colorFar.setStyle(isDark ? DARK_FAR : LIGHT_FAR);

          const blend = isDark ? DARK_BLEND : LIGHT_BLEND;
          const mode = blend.additive ? THREE.AdditiveBlending : THREE.NormalBlending;
          fieldMat.blending = mode;
          nodeMat.blending = mode;
          // Rails never glow: additive would turn a structural line into a
          // light source and pull the eye off the packets.
          railMat.blending = THREE.NormalBlending;
          fieldUniforms.uGain.value = blend.field;
          railUniforms.uGain.value = blend.rail;
          nodeUniforms.uGain.value = blend.node;
        };
        applyPalette();
        dark.addEventListener("change", applyPalette);
        teardown.push(() => dark.removeEventListener("change", applyPalette));

        // ---- sizing ------------------------------------------------------
        // Nothing here rebuilds geometry. Every position is frame-relative, so
        // a resize is one aspect uniform and the projection matrix — the whole
        // reason for the frame-unit contract.
        const resize = () => {
          const w = el.clientWidth;
          const h = Math.max(el.clientHeight, 1);
          if (w === 0) return;
          renderer.setSize(w, h, false);
          camera.aspect = w / h;
          camera.updateProjectionMatrix();
          frameUniforms.uAspect.value = camera.aspect;
        };
        resize();
        const observer = new ResizeObserver(resize);
        observer.observe(el);
        teardown.push(() => observer.disconnect());

        // ---- input -------------------------------------------------------
        let pointerX = 0;
        let pointerY = 0;
        const onPointer = (e: PointerEvent) => {
          pointerX = (e.clientX / innerWidth - 0.5) * 2;
          pointerY = -(e.clientY / innerHeight - 0.5) * 2;
        };
        window.addEventListener("pointermove", onPointer, { passive: true });
        teardown.push(() => window.removeEventListener("pointermove", onPointer));

        // ---- loop --------------------------------------------------------
        let camX = 0;
        let camY = 0;
        let elapsed = 0;
        let last = performance.now();
        let frame = 0;

        // Fraction of the way through the current inter-arrival gap, per lane.
        // Derived from the same phase arithmetic the field shader runs, so a
        // ping cannot drift out of step with the packet that caused it: cohorts
        // sit 1/COHORTS apart in phase, so an arrival lands every 1/COHORTS of
        // a cycle. Hoisted out of the loop because a closure allocated sixty
        // times a second for eleven floats is silly.
        const advancePing = (lane: Lane, i: number) => {
          const s = (lane.offset + elapsed * lane.speed) * COHORTS + PING_LEAD;
          const u = s - Math.floor(s);
          nodeU[i * 3] = u;
          nodeU[i * 3 + 1] = u;
          nodeU[i * 3 + 2] = u;
        };

        const tick = () => {
          frame = requestAnimationFrame(tick);

          const now = performance.now();
          // Clamped. A tab that was hidden, or a main thread that stalled on a
          // route transition, hands back a delta measured in seconds — and an
          // unclamped clock teleports every packet to a new phase, which reads
          // as a glitch rather than as time having passed.
          const dt = Math.min((now - last) / 1000, 0.05);
          last = now;
          elapsed += dt;

          fieldUniforms.uTime.value = elapsed;
          lanes.forEach(advancePing);
          nodeUAttr.needsUpdate = true;

          // Exponential rather than a fixed lerp factor: a fixed one makes the
          // camera settle twice as fast on a 120 Hz display. The amplitudes are
          // small on purpose — a truck moves the near nodes eight times as far
          // as the deep field, so a little goes a long way.
          const k = 1 - Math.exp(-dt * 2.6);
          camX += (Math.sin(elapsed * 0.085) * 0.09 + pointerX * 0.13 - camX) * k;
          camY += (Math.cos(elapsed * 0.062) * 0.05 + pointerY * 0.07 - camY) * k;
          camera.position.set(camX, camY, CAM_Z);

          try {
            renderer.render(scene, camera);
          } catch (error) {
            // The one place a shader that will not compile on some GPU actually
            // surfaces — compilation is deferred to first draw, so it throws
            // inside a rAF callback with nothing React owns on the stack. The
            // frame above is ALREADY re-requested, so without this the loop
            // rethrows sixty times a second forever and the boundary that
            // exists for exactly this case never hears about it.
            cancelAnimationFrame(frame);
            if (!disposed) setFatal(error instanceof Error ? error : new Error(String(error)));
          }
        };

        // Paused while hidden: a decorative canvas has no business holding the
        // GPU awake behind another tab.
        const onVisibility = () => {
          cancelAnimationFrame(frame);
          if (!document.hidden) {
            last = performance.now();
            frame = requestAnimationFrame(tick);
          }
        };
        document.addEventListener("visibilitychange", onVisibility);
        teardown.push(() => document.removeEventListener("visibilitychange", onVisibility));

        // A lost context otherwise leaves a live rAF loop rendering into
        // nothing. Hand it to the error boundary, which swaps in the static
        // hero.
        const onContextLost = (e: Event) => {
          e.preventDefault();
          cancelAnimationFrame(frame);
          if (!disposed) setFatal(new Error("hero: WebGL context lost"));
        };
        renderer.domElement.addEventListener("webglcontextlost", onContextLost);
        teardown.push(() =>
          renderer.domElement.removeEventListener("webglcontextlost", onContextLost),
        );

        frame = requestAnimationFrame(tick);
        // Registered last so it is released first: the loop must stop before
        // anything it renders is disposed out from under it.
        teardown.push(() => cancelAnimationFrame(frame));
      } catch (error) {
        // A failure in this async setup does not reach a React error boundary
        // on its own, so it is routed through state and rethrown during render
        // below. Released first and unconditionally — a partly-built scene
        // holds a GL context whether or not this component is still mounted.
        release();
        if (!disposed) setFatal(error instanceof Error ? error : new Error(String(error)));
      }
    })();

    return () => {
      disposed = true;
      release();
    };
  }, []);

  // Rethrown in the render phase, where `SceneErrorBoundary` can catch it and
  // show the static hero.
  if (fatal !== null) throw fatal;

  return <div ref={host} className="absolute inset-0" aria-hidden />;
}
