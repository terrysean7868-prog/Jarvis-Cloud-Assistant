// public/filamentWorker.js
// OffscreenCanvas-based atomic renderer (nucleus + circular orbit rings + electrons)
let canvas = null;
let ctx = null;
let width = 0;
let height = 0;
let devicePixelRatio = 1;
let running = false;
let paused = false;
let t = 0;
let loopToken = 0;

// Atomic model config
const RING_COUNT = 7;
let ringConfigs = []; // [{count, phase, speed, dir}]

const ELECTRON_RANDOMIZE_INTERVAL_S = 5;
let nextElectronRandomizeAt = 0;

function _seedFromWH() {
  // Deterministic seed based on canvas size (stable across frames).
  // Good enough for visuals; not used for security.
  return ((width | 0) * 73856093) ^ ((height | 0) * 19349663) ^ 0x9e3779b9;
}

function _makeRng(seed) {
  // Simple LCG
  let s = (seed >>> 0) || 1;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 4294967296;
  };
}

function initRings() {
  const rand = _makeRng(_seedFromWH());
  ringConfigs = [];
  for (let i = 0; i < RING_COUNT; i++) {
    // per-ring electrons: 2..5 (varies per ring)
    const count = 2 + Math.floor(rand() * 4);
    ringConfigs.push({
      count,
      phase: rand() * Math.PI * 2,
      speed: 0.55 + i * 0.16 + rand() * 0.08,
      dir: (i % 2 === 0) ? 1 : -1,
    });
  }
}

function randomizeElectronCounts() {
  if (!ringConfigs || ringConfigs.length !== RING_COUNT) return;
  for (let i = 0; i < ringConfigs.length; i++) {
    ringConfigs[i].count = 2 + Math.floor(Math.random() * 4);
  }
}

// Render cap for low CPU/GPU usage
const TARGET_FPS = 30;
const FRAME_MS = 1000 / TARGET_FPS;

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
    postMessage({ type: "log", message: "atomicWorker initialized" });
  } else if (data.type === "resize") {
    width = data.width || width;
    height = data.height || height;
    canvas.width = Math.floor(width * devicePixelRatio);
    canvas.height = Math.floor(height * devicePixelRatio);
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.scale(devicePixelRatio, devicePixelRatio);
    // Re-seed ring configs for the new viewport to keep spacing looking intentional.
    initRings();
    startLoop();
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

function _setStrokeGlow(color, blurPx) {
  // canvas shadow is cheap and matches the minimal neon aesthetic
  ctx.shadowColor = color;
  ctx.shadowBlur = blurPx;
}

function _drawOrbitRing(center, radius, rgba, alpha, lineWidth) {
  ctx.save();
  ctx.lineWidth = lineWidth;
  ctx.strokeStyle = `rgba(${rgba.r},${rgba.g},${rgba.b},${alpha})`;
  _setStrokeGlow(`rgba(${rgba.r},${rgba.g},${rgba.b},${Math.min(1, alpha * 0.8)})`, Math.max(2, lineWidth * 4));
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
  ctx.stroke();
  ctx.restore();
}

function _drawElectron(x, y, rgba, sizePx, glowPx, alpha) {
  ctx.save();
  const core = `rgba(255,255,255,${Math.min(1, alpha * 0.9)})`;
  const aura = `rgba(${rgba.r},${rgba.g},${rgba.b},${alpha})`;
  ctx.fillStyle = core;
  _setStrokeGlow(aura, glowPx);
  ctx.beginPath();
  ctx.arc(x, y, sizePx, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function _drawNucleus(center, rgba, intensity) {
  ctx.save();
  const r = Math.min(width, height) * 0.03;
  const outer = Math.min(width, height) * 0.14;
  const a = Math.max(0.08, Math.min(0.55, intensity));

  const g = ctx.createRadialGradient(center.x, center.y, r * 0.2, center.x, center.y, outer);
  g.addColorStop(0, `rgba(255,255,255,${Math.min(0.9, a * 1.2)})`);
  g.addColorStop(0.22, `rgba(${rgba.r},${rgba.g},${rgba.b},${Math.min(0.65, a)})`);
  g.addColorStop(1, `rgba(0,0,0,0)`);
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(center.x, center.y, outer, 0, Math.PI * 2);
  ctx.fill();

  // crisp nucleus dot
  ctx.shadowBlur = 0;
  ctx.fillStyle = `rgba(255,255,255,${Math.min(1, a * 0.95)})`;
  ctx.beginPath();
  ctx.arc(center.x, center.y, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.restore();
}

function startLoop() {
  if (!ctx) return;
  // Ensure only one RAF loop runs at a time (resize can call startLoop again).
  loopToken += 1;
  const myToken = loopToken;
  running = true;
  const center = { x: width / 2, y: height / 2 };

  if (!ringConfigs || ringConfigs.length !== RING_COUNT) {
    initRings();
  }

  // Schedule first re-randomization after the interval.
  nextElectronRandomizeAt = t + ELECTRON_RANDOMIZE_INTERVAL_S;

  // Keep time continuous across resizes for smooth motion.
  let last = performance.now();
  let lastRender = last;

  function frame(now) {
    if (myToken !== loopToken) return;
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

    // Re-randomize electron counts periodically (keeps rings stable).
    if (t >= nextElectronRandomizeAt) {
      randomizeElectronCounts();
      nextElectronRandomizeAt = t + ELECTRON_RANDOMIZE_INTERVAL_S;
    }

    // clear
    ctx.clearRect(0, 0, width, height);

    const emo = colorForEmotion(state.emotion);

    // Subtle background wash (keeps dark, increases contrast)
    const bgAlpha = 0.015 + (state.wakePulse ? 0.01 : 0) + Math.min(0.02, state.volume * 0.03);
    ctx.fillStyle = `rgba(${emo.r},${emo.g},${emo.b},${bgAlpha})`;
    ctx.fillRect(0, 0, width, height);

    // Orbital rings: perfectly circular, symmetrical, evenly spaced.
    const minDim = Math.min(width, height);
    const ringCount = RING_COUNT;
    // Fit all 7 rings inside the viewport with a stable margin.
    const outerRadius = minDim * 0.42;
    const innerRadius = minDim * 0.14;
    const baseRadius = innerRadius;
    const gap = (outerRadius - innerRadius) / Math.max(1, ringCount - 1);
    const lineW = 0.9;
    const ringAlpha = 0.18 + (state.wakePulse ? 0.03 : 0);

    // Draw all rings first for stable geometry.
    for (let i = 0; i < ringCount; i++) {
      const radius = baseRadius + i * gap;
      _drawOrbitRing(center, radius, emo, ringAlpha * (1 - i * 0.06), lineW);
    }

    // Electrons: small luminous particles moving along the rings.
    const speedBase = 0.55; // rad/s
    const electronAlpha = 0.55 + Math.min(0.25, state.volume * 0.35);
    const dotSize = Math.max(1.6, minDim * 0.002);
    const glow = Math.max(6, minDim * 0.02);

    for (let i = 0; i < ringCount; i++) {
      const radius = baseRadius + i * gap;
      const cfg = ringConfigs[i] || { count: 2, phase: 0, speed: speedBase + i * 0.16, dir: 1 };
      const w = cfg.speed || (speedBase + i * 0.16);
      const dir = cfg.dir || 1;
      const basePhase = (cfg.phase || 0) + t * w * dir;
      const n = Math.max(2, Math.min(5, cfg.count || 2));

      for (let j = 0; j < n; j++) {
        const ang = basePhase + (j / n) * Math.PI * 2;
        const x = center.x + Math.cos(ang) * radius;
        const y = center.y + Math.sin(ang) * radius;
        // tiny per-electron alpha variance, but keep it subtle
        const a = electronAlpha * (0.92 + 0.08 * ((j % 2) ? 1 : 0.6));
        _drawElectron(x, y, emo, dotSize, glow, a);
      }
    }

    // Nucleus glow at exact center.
    const nucleusIntensity = (emo.a * 1.55) + (state.wakePulse ? 0.18 : 0) + Math.min(0.22, state.volume * 0.35);
    _drawNucleus(center, emo, nucleusIntensity);

    // request next frame inside worker — OffscreenCanvas supports RAF in worker
    self.requestAnimationFrame(frame);
  }

  self.requestAnimationFrame(frame);
}
