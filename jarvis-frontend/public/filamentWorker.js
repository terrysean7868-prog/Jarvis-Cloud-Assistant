// public/filamentWorker.js
// OffscreenCanvas-based filament renderer
let canvas = null;
let ctx = null;
let width = 0;
let height = 0;
let devicePixelRatio = 1;
let nodes = [];
let running = false;
let paused = false;
let t = 0;

// Render cap for low CPU/GPU usage
const TARGET_FPS = 30;
const FRAME_MS = 1000 / TARGET_FPS;

function initNodes(center, nodeCount, baseRadius) {
  nodes = [];
  for (let i = 0; i < nodeCount; i++) {
    const angle = (i / nodeCount) * Math.PI * 2;
    const r = baseRadius * (0.8 + Math.random() * 0.6);
    nodes.push({
      baseAngle: angle,
      angle,
      r,
      x: center.x + Math.cos(angle) * r,
      y: center.y + Math.sin(angle) * r,
      z: 0,
      twist: Math.random() * 0.9 + 0.1,
      phase: Math.random() * Math.PI * 2,
      zAmp: 0.35 + Math.random() * 0.75,
    });
  }
}

function colorForEmotion(emotion) {
  switch (emotion) {
    case "calm": return { r: 8, g: 200, b: 220, a: 0.18 };
    case "analyzing": return { r: 220, g: 190, b: 40, a: 0.22 };
    case "action": return { r: 90, g: 255, b: 130, a: 0.22 };
    case "critical": return { r: 255, g: 80, b: 90, a: 0.26 };
    default: return { r: 8, g: 200, b: 220, a: 0.18 };
  }
}

// state updated from main thread
let state = { emotion: "calm", wakePulse: false, transformState: "normal", volume: 0 };

onmessage = (ev) => {
  const data = ev.data || {};
  if (data.type === "init") {
    canvas = data.canvas;
    devicePixelRatio = data.devicePixelRatio || 1;
    width = data.width || 800;
    height = data.height || 600;
    canvas.width = Math.floor(width * devicePixelRatio);
    canvas.height = Math.floor(height * devicePixelRatio);
    canvas.style = canvas.style || {};
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx = canvas.getContext("2d");
    ctx.scale(devicePixelRatio, devicePixelRatio);
    startLoop();
    postMessage({ type: "log", message: "filamentWorker initialized" });
  } else if (data.type === "resize") {
    width = data.width || width;
    height = data.height || height;
    canvas.width = Math.floor(width * devicePixelRatio);
    canvas.height = Math.floor(height * devicePixelRatio);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(devicePixelRatio, devicePixelRatio);
    startLoop(true);
  } else if (data.type === "audio") {
    state.volume = data.volume || state.volume;
  } else if (data.type === "state") {
    state = { ...state, ...data };
  } else if (data.type === "set_running") {
    paused = !data.running;
  } else if (data.type === "dispose") {
    running = false;
    close();
  }
};

function _project3D(center, x, y, z, fov) {
  // Classic perspective projection: scale = fov / (fov + z)
  // Keep denominator safe.
  const denom = Math.max(120, fov + z);
  const s = fov / denom;
  return { x: center.x + x * s, y: center.y + y * s, s };
}

function startLoop(reinit = false) {
  if (!ctx) return;
  running = true;
  // init nodes when needed
  const center = { x: width / 2, y: height / 2 };
  const nodeCount = Math.max(8, Math.floor(Math.min(width, height) / 60));
  const baseRadius = Math.min(width, height) * 0.18;
  if (reinit || nodes.length === 0) initNodes(center, nodeCount, baseRadius);

  t = 0;
  let last = performance.now();
  let lastRender = last;

  function frame(now) {
    if (!running) return;
    if (paused) {
      // Stay registered but do no work.
      self.requestAnimationFrame(frame);
      return;
    }

    // FPS cap (worker RAF can be 60fps+; keep it cheap)
    if (now - lastRender < FRAME_MS) {
      self.requestAnimationFrame(frame);
      return;
    }
    lastRender = now;

    const dt = (now - last) / 1000;
    last = now;
    t += dt;

    // clear
    ctx.clearRect(0, 0, width, height);

    // background glow
    const emo = colorForEmotion(state.emotion);
    const glowAlpha = emo.a + (state.wakePulse ? 0.08 : 0) + Math.min(0.22, state.volume * 0.5);
    ctx.fillStyle = `rgba(${emo.r},${emo.g},${emo.b},${Math.max(0.02, glowAlpha * 0.12)})`;
    ctx.fillRect(0, 0, width, height);

    // update nodes (pseudo-3D motion with perspective projection)
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const tw = Math.sin(t * 0.6 + n.phase) * 0.4 * n.twist;
      const volPush = 1 + state.volume * 0.8;
      n.angle = n.baseAngle + tw * (state.transformState === "twist" ? 2.2 : 1.0);

      const radius = n.r * (state.transformState === "expand" ? 1.25 : state.transformState === "contract" ? 0.7 : 1) * volPush;
      const baseX = Math.cos(n.angle + t * 0.06) * radius;
      const baseY = Math.sin(n.angle + t * 0.06) * radius;
      const baseZ = Math.sin(t * 0.85 + n.phase) * (Math.min(width, height) * 0.12) * n.zAmp * (0.25 + state.volume);

      // Rotations create a true 3D feel.
      const rx = 0.55 + Math.sin(t * 0.25) * 0.12 + state.volume * 0.18;
      const ry = 0.35 + Math.cos(t * 0.22) * 0.12 + state.volume * 0.14;

      // rotate around X
      const cosx = Math.cos(rx);
      const sinx = Math.sin(rx);
      const y1 = baseY * cosx - baseZ * sinx;
      const z1 = baseY * sinx + baseZ * cosx;

      // rotate around Y
      const cosy = Math.cos(ry);
      const siny = Math.sin(ry);
      const x2 = baseX * cosy + z1 * siny;
      const z2 = -baseX * siny + z1 * cosy;

      n.x = center.x + x2;
      n.y = center.y + y1;
      n.z = z2;
    }

    const fov = Math.min(width, height) * 0.9;
    const proj = new Array(nodes.length);
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      proj[i] = _project3D(center, n.x - center.x, n.y - center.y, n.z, fov);
    }

    ctx.lineWidth = 0.9 + Math.min(2.0, 1.1 + state.volume * 1.6);
    for (let i = 0; i < nodes.length; i++) {
      const a = proj[i];
      const b = proj[(i + 1) % proj.length];
      const depthAlpha = Math.max(0.18, Math.min(1, (a.s + b.s) * 0.55));

      const midx = (a.x + b.x) / 2 + Math.sin(t * 1.1 + i) * 10 * (state.volume + 0.05) * depthAlpha;
      const midy = (a.y + b.y) / 2 + Math.cos(t * 1.0 + i) * 10 * (state.volume + 0.05) * depthAlpha;

      const grad = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
      grad.addColorStop(0, `rgba(${emo.r},${emo.g},${emo.b},${(0.78 * (0.55 + state.volume)) * depthAlpha})`);
      grad.addColorStop(1, `rgba(255,255,255,${(0.06 + state.volume * 0.14) * depthAlpha})`);
      ctx.strokeStyle = grad;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(midx, midy, b.x, b.y);
      ctx.stroke();
    }

    const haloRadius = Math.min(width, height) * (0.08 + 0.02 * state.volume);
    const haloGrad = ctx.createRadialGradient(center.x, center.y, haloRadius * 0.1, center.x, center.y, haloRadius * 1.6);
    haloGrad.addColorStop(0, `rgba(${emo.r},${emo.g},${emo.b},${0.24 + state.volume * 0.5})`);
    haloGrad.addColorStop(1, `rgba(0,0,0,0)`);
    ctx.fillStyle = haloGrad;
    ctx.beginPath();
    ctx.arc(center.x, center.y, haloRadius * 1.6, 0, Math.PI * 2);
    ctx.fill();

    // request next frame inside worker — OffscreenCanvas supports RAF in worker
    self.requestAnimationFrame(frame);
  }

  self.requestAnimationFrame(frame);
}
