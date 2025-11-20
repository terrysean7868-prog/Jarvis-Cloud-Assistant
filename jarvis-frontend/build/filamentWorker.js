// public/filamentWorker.js
// OffscreenCanvas-based filament renderer
let canvas = null;
let ctx = null;
let width = 0;
let height = 0;
let devicePixelRatio = 1;
let nodes = [];
let running = false;
let t = 0;

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
      twist: Math.random() * 0.9 + 0.1,
      phase: Math.random() * Math.PI * 2
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
  } else if (data.type === "dispose") {
    running = false;
    close();
  }
};

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

  function frame(now) {
    if (!running) return;
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

    // update nodes
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const tw = Math.sin(t * 0.6 + n.phase) * 0.4 * n.twist;
      const volPush = 1 + state.volume * 0.8;
      n.angle = n.baseAngle + tw * (state.transformState === "twist" ? 2.2 : 1.0);
      const radius = n.r * (state.transformState === "expand" ? 1.25 : state.transformState === "contract" ? 0.7 : 1) * volPush;
      n.x = center.x + Math.cos(n.angle + t * 0.06) * radius;
      n.y = center.y + Math.sin(n.angle + t * 0.06) * radius;
    }

    ctx.lineWidth = 1 + Math.min(2.2, 1.2 + state.volume * 2);
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      const b = nodes[(i + 1) % nodes.length];
      const midx = (a.x + b.x) / 2 + Math.sin(t * 1.2 + i) * 8 * (state.volume + 0.05);
      const midy = (a.y + b.y) / 2 + Math.cos(t * 1.3 + i) * 8 * (state.volume + 0.05);
      const grad = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
      grad.addColorStop(0, `rgba(${emo.r},${emo.g},${emo.b},${0.85 * (0.6 + state.volume)})`);
      grad.addColorStop(1, `rgba(255,255,255,${0.08 + state.volume * 0.18})`);
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
