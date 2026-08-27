const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/three-DwIcnsOs.js","assets/rolldown-runtime-hePW80VL.js"])))=>i.map(i=>d[i]);
import{r as e}from"./rolldown-runtime-hePW80VL.js";import{f as t,h as n,p as r,s as i}from"./index-DALehavr.js";var a=e(n(),1),o=r(),ee=11,s=7,te=58,ne=700,c=5166,l=18,re=.28,u=33,d=6.4,ie=46,ae=Math.tan(ie/2*(Math.PI/180)),f=2.399963,p=.962,oe=1-p*s%1,se=`--c-accent`,ce=`--c-accent-ink`,le=`#3d87ff`,ue=`#2f62e6`,de=`#8fb3f5`,fe={additive:!0,field:1,rail:.6,node:1},pe={additive:!1,field:.9,rail:.62,node:.95};function me(e){let t=e>>>0;return()=>{t=t+1831565813>>>0;let e=Math.imul(t^t>>>15,1|t);return e=e+Math.imul(e^e>>>7,61|e)^e,((e^e>>>14)>>>0)/4294967296}}var m=`
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
`,he=`
  ${m}

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
    float land = smoothstep(0.855, ${p.toFixed(3)}, p);
    float gone = smoothstep(${p.toFixed(3)}, 1.0, p);
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
`,ge=`
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
`,_e=`
  ${m}

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
`,ve=`
  uniform vec3 uColor;
  uniform float uGain;
  varying float vA;

  void main() {
    gl_FragColor = vec4(uColor, vA * uGain);
    #include <colorspace_fragment>
  }
`,ye=`
  ${m}

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
`,be=`
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
`;function h(){let e=(0,a.useRef)(null),[n,r]=(0,a.useState)(null);if((0,a.useEffect)(()=>{let n=e.current;if(n===null)return;let a=!1,o=[],p=()=>{for(;o.length>0;)o.pop()?.()};return(async()=>{try{let e=await t(()=>import(`./three-DwIcnsOs.js`).then(e=>e.t),__vite__mapDeps([0,1]));if(a)return;let p=new e.WebGLRenderer({antialias:!0,alpha:!0,powerPreference:`low-power`});p.setPixelRatio(Math.min(devicePixelRatio,2)),p.setClearColor(0,0),p.domElement.style.width=`100%`,p.domElement.style.height=`100%`,p.domElement.style.display=`block`,n.appendChild(p.domElement),o.push(()=>{p.forceContextLoss(),p.dispose(),p.domElement.remove()});let m=new e.Scene,h=new e.PerspectiveCamera(ie,1,.5,90);h.position.set(0,0,d);let g=me(1592597838),_=()=>{let t=new e.InstancedBufferGeometry;return t.setAttribute(`position`,new e.BufferAttribute(new Float32Array([-1,-1,0,1,-1,0,1,1,0,-1,1,0]),3)),t.setIndex([0,1,2,2,3,0]),t},v=[];for(let e=0;e<ee;e+=1){let t=e/10,n=-3.6+5.2*t+(g()-.5)*1.4,r=e*f+.35,a=.58+g()*.42;v.push({node:[-.78+1.52*t+(g()-.5)*.07,.6*Math.sin(e*f+.9)+(g()-.5)*.09,n],far:[Math.cos(r)*a*1.08,Math.sin(r)*a,-17-g()*15],speed:1/(i.ambient*(.75+g()*.85)*s),offset:g(),scale:(4.800000000000001/(d-n))**.55})}let y=new Float32Array(c*3),b=new Float32Array(c*3),x=new Float32Array(c*3),S=new Float32Array(c),C=new Float32Array(c),w=new Float32Array(c),T=()=>g()+g()-1,E=0;for(let e of v)for(let t=0;t<s;t+=1)for(let n=0;n<te;n+=1)y.set(e.far,E*3),b.set(e.node,E*3),x[E*3]=T()*.78,x[E*3+1]=T()*.36,x[E*3+2]=T()*3.2,S[E]=e.offset+t/s+(g()-.5)*.012,C[E]=e.speed,w[E]=g(),E+=1;for(let e=0;e<ne;e+=1){let e=[(g()*2-1)*1.15,(g()*2-1)*1.05,-26-g()*20];y.set(e,E*3),b.set(e,E*3),x[E*3]=T()*.08,x[E*3+1]=T()*.06,x[E*3+2]=T()*3,S[E]=.06+g()*.05,C[E]=0,w[E]=g(),E+=1}let D=_();D.instanceCount=c,D.setAttribute(`aFar`,new e.InstancedBufferAttribute(y,3)),D.setAttribute(`aNode`,new e.InstancedBufferAttribute(b,3)),D.setAttribute(`aSpread`,new e.InstancedBufferAttribute(x,3)),D.setAttribute(`aPhase`,new e.InstancedBufferAttribute(S,1)),D.setAttribute(`aSpeed`,new e.InstancedBufferAttribute(C,1)),D.setAttribute(`aSeed`,new e.InstancedBufferAttribute(w,1));let O={uTanH:{value:ae},uAspect:{value:1},uCamZ:{value:d}},xe=new e.Color,k=new e.Color,A=new e.Color,j={...O,uTime:{value:0},uSizeFar:{value:.019},uSizeNear:{value:.013},uGain:{value:1},uFar:{value:xe},uNear:{value:k},uFlash:{value:A}},M=new e.ShaderMaterial({vertexShader:he,fragmentShader:ge,uniforms:j,transparent:!0,side:e.DoubleSide,forceSinglePass:!0,depthTest:!1,depthWrite:!1});o.push(()=>{D.dispose(),M.dispose()});let N=new e.Mesh(D,M);N.frustumCulled=!1,N.renderOrder=1,m.add(N);let P=new Float32Array(1188),F=new Float32Array(1188),I=new Float32Array(396),Se=new Float32Array(396),L=0;for(let e of v)for(let t=0;t<l;t+=1)for(let n of[t/l,(t+1)/l])P.set(e.far,L*3),F.set(e.node,L*3),I[L]=re+.72*n,Se[L]=n**2.4,L+=1;let R=new e.BufferGeometry;R.setAttribute(`position`,new e.BufferAttribute(P,3)),R.setAttribute(`aTo`,new e.BufferAttribute(F,3)),R.setAttribute(`aT`,new e.BufferAttribute(I,1)),R.setAttribute(`aAlpha`,new e.BufferAttribute(Se,1));let Ce={...O,uColor:{value:k},uGain:{value:1}},z=new e.ShaderMaterial({vertexShader:_e,fragmentShader:ve,uniforms:Ce,transparent:!0,depthTest:!1,depthWrite:!1});o.push(()=>{R.dispose(),z.dispose()});let B=new e.LineSegments(R,z);B.frustumCulled=!1,B.renderOrder=0,m.add(B);let we=new Float32Array(99),Te=new Float32Array(u),Ee=new Float32Array(u),V=new Float32Array(u);v.forEach((e,t)=>{for(let n=0;n<3;n+=1){let r=t*3+n;we.set(e.node,r*3),Te[r]=e.scale,Ee[r]=n}});let H=_();H.instanceCount=u,H.setAttribute(`aPos`,new e.InstancedBufferAttribute(we,3)),H.setAttribute(`aScale`,new e.InstancedBufferAttribute(Te,1)),H.setAttribute(`aRole`,new e.InstancedBufferAttribute(Ee,1));let U=new e.InstancedBufferAttribute(V,1);U.setUsage(e.DynamicDrawUsage),H.setAttribute(`aU`,U);let De={...O,uCoreSize:{value:.15},uRingSize:{value:.52},uGain:{value:1},uNear:{value:k},uFlash:{value:A}},W=new e.ShaderMaterial({vertexShader:ye,fragmentShader:be,uniforms:De,transparent:!0,side:e.DoubleSide,forceSinglePass:!0,depthTest:!1,depthWrite:!1});o.push(()=>{H.dispose(),W.dispose()});let G=new e.Mesh(H,W);G.frustumCulled=!1,G.renderOrder=2,m.add(G);let K=matchMedia(`(prefers-color-scheme: dark)`),q=()=>{let t=getComputedStyle(document.documentElement),n=e=>{let n=t.getPropertyValue(e).trim();if(n===``)throw Error(`hero: design token ${e} is not defined`);return n},r=K.matches;k.setStyle(n(se)),A.setStyle(r?le:n(ce)),xe.setStyle(r?ue:de);let i=r?fe:pe,a=i.additive?e.AdditiveBlending:e.NormalBlending;M.blending=a,W.blending=a,z.blending=e.NormalBlending,j.uGain.value=i.field,Ce.uGain.value=i.rail,De.uGain.value=i.node};q(),K.addEventListener(`change`,q),o.push(()=>K.removeEventListener(`change`,q));let Oe=()=>{let e=n.clientWidth,t=Math.max(n.clientHeight,1);e!==0&&(p.setSize(e,t,!1),h.aspect=e/t,h.updateProjectionMatrix(),O.uAspect.value=h.aspect)};Oe();let ke=new ResizeObserver(Oe);ke.observe(n),o.push(()=>ke.disconnect());let Ae=0,je=0,Me=e=>{Ae=(e.clientX/innerWidth-.5)*2,je=-(e.clientY/innerHeight-.5)*2};window.addEventListener(`pointermove`,Me,{passive:!0}),o.push(()=>window.removeEventListener(`pointermove`,Me));let J=0,Y=0,X=0,Z=performance.now(),Q=0,Ne=(e,t)=>{let n=(e.offset+X*e.speed)*s+oe,r=n-Math.floor(n);V[t*3]=r,V[t*3+1]=r,V[t*3+2]=r},$=()=>{Q=requestAnimationFrame($);let e=performance.now(),t=Math.min((e-Z)/1e3,.05);Z=e,X+=t,j.uTime.value=X,v.forEach(Ne),U.needsUpdate=!0;let n=1-Math.exp(-t*2.6);J+=(Math.sin(X*.085)*.09+Ae*.13-J)*n,Y+=(Math.cos(X*.062)*.05+je*.07-Y)*n,h.position.set(J,Y,d);try{p.render(m,h)}catch(e){cancelAnimationFrame(Q),a||r(e instanceof Error?e:Error(String(e)))}},Pe=()=>{cancelAnimationFrame(Q),document.hidden||(Z=performance.now(),Q=requestAnimationFrame($))};document.addEventListener(`visibilitychange`,Pe),o.push(()=>document.removeEventListener(`visibilitychange`,Pe));let Fe=e=>{e.preventDefault(),cancelAnimationFrame(Q),a||r(Error(`hero: WebGL context lost`))};p.domElement.addEventListener(`webglcontextlost`,Fe),o.push(()=>p.domElement.removeEventListener(`webglcontextlost`,Fe)),Q=requestAnimationFrame($),o.push(()=>cancelAnimationFrame(Q))}catch(e){p(),a||r(e instanceof Error?e:Error(String(e)))}})(),()=>{a=!0,p()}},[]),n!==null)throw n;return(0,o.jsx)(`div`,{ref:e,className:`absolute inset-0`,"aria-hidden":!0})}export{h as default};
//# sourceMappingURL=HeroScene-w_9K5ze7.js.map