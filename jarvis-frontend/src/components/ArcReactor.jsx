// src/components/ArcReactor.jsx
import React, { useEffect, useMemo, useRef } from "react";
import "../styles/arcReactor.css";

export default function ArcReactor({
  active = false,
  emotion = "calm",
  size = 340,
  volume = 0,
  showCaption = true,
}) {
  const canvasRef = useRef(null);
  const rafRef = useRef(0);
  const timeRef = useRef(0);
  const ampRef = useRef(0);
  const lastFrameTsRef = useRef(0);
  const fpsWindowStartRef = useRef(0);
  const fpsFramesRef = useRef(0);
  const dynamicFilamentCountRef = useRef(0);
  const qualityRef = useRef(1);
  const noiseCanvasRef = useRef(null);
  const scratchesRef = useRef(null);

  const color = useMemo(() => {
    const colorMap = {
      calm: { core: "#00ffc8", ring: "#00d4ff", accent: "#00ffc8" },
      analyzing: { core: "#ffd24d", ring: "#ff9f43", accent: "#ffd24d" },
      critical: { core: "#ff4d4f", ring: "#ff6b6b", accent: "#ff4d4f" },
    };
    return colorMap[emotion] || colorMap.calm;
  }, [emotion]);

  const baseFilamentCount = useMemo(() => {
    try {
      const small = typeof window !== "undefined" && window.matchMedia && window.matchMedia("(max-width: 520px)").matches;
      // Keep this lightweight; adaptive quality will raise/lower further.
      return small ? 90 : 130;
    } catch {
      return 110;
    }
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d", { alpha: true, desynchronized: true });
    if (!ctx) return;

    const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));
    const resize = () => {
      const cssSize = Math.max(120, Number(size) || 340);
      canvas.style.width = `${cssSize}px`;
      canvas.style.height = `${cssSize}px`;
      canvas.width = Math.floor(cssSize * dpr);
      canvas.height = Math.floor(cssSize * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };

    resize();
    window.addEventListener("resize", resize);

    // Scale all geometry from a baseline design size so rings never get clipped
    // by the square canvas bounds on smaller reactor sizes.
    const DESIGN_SIZE = 480;

    // Adaptive load shedding for low-end devices.
    // We track FPS over ~1s and reduce filament count if FPS drops.
    const minFilaments = 60;
    const maxFilaments = Math.max(minFilaments, baseFilamentCount);
    dynamicFilamentCountRef.current = Math.max(minFilaments, Math.min(maxFilaments, dynamicFilamentCountRef.current || baseFilamentCount));
    qualityRef.current = Math.max(0.35, Math.min(1, dynamicFilamentCountRef.current / maxFilaments));
    lastFrameTsRef.current = 0;
    fpsWindowStartRef.current = 0;
    fpsFramesRef.current = 0;

    // Precompute a tiny noise texture (drawn as a subtle overlay) for realism.
    if (!noiseCanvasRef.current) {
      const n = document.createElement("canvas");
      n.width = 128;
      n.height = 128;
      const nctx = n.getContext("2d");
      if (nctx) {
        const img = nctx.createImageData(n.width, n.height);
        for (let i = 0; i < img.data.length; i += 4) {
          const v = (Math.random() * 255) | 0;
          img.data[i] = v;
          img.data[i + 1] = v;
          img.data[i + 2] = v;
          img.data[i + 3] = (Math.random() * 55) | 0; // low alpha
        }
        nctx.putImageData(img, 0, 0);
      }
      noiseCanvasRef.current = n;
    }

    // Precompute micro-scratch segments (very cheap to render).
    if (!scratchesRef.current) {
      const scratches = [];
      const count = 22;
      for (let i = 0; i < count; i++) {
        // angle around center
        const a = Math.random() * Math.PI * 2;
        // radius band (near outer mid rings)
        const r0 = 0.34 + Math.random() * 0.22;
        const len = 0.04 + Math.random() * 0.10;
        const width = 0.5 + Math.random() * 1.1;
        const alpha = 0.04 + Math.random() * 0.08;
        scratches.push({ a, r0, len, width, alpha });
      }
      scratchesRef.current = scratches;
    }

    const draw = (ts) => {
      const cssSize = Math.max(120, Number(size) || 340);
      const cx = cssSize / 2;
      const cy = cssSize / 2;
      const scale = cssSize / DESIGN_SIZE;

      // Ring radii expressed as fractions of the canvas size.
      // Derived from the original absolute radii assuming a ~480px design canvas.
      const baseRadii = [0.154, 0.221, 0.296, 0.384, 0.462].map((f) => f * cssSize);

      // FPS tracking + adaptive filament count
      if (!fpsWindowStartRef.current) fpsWindowStartRef.current = ts;
      fpsFramesRef.current += 1;
      const windowElapsed = ts - fpsWindowStartRef.current;
      if (windowElapsed >= 1000) {
        const fps = (fpsFramesRef.current * 1000) / windowElapsed;
        fpsFramesRef.current = 0;
        fpsWindowStartRef.current = ts;

        const current = dynamicFilamentCountRef.current;
        // Hysteresis to avoid oscillation.
        if (fps < 45 && current > minFilaments) {
          dynamicFilamentCountRef.current = Math.max(minFilaments, Math.floor(current * 0.8));
        } else if (fps > 55 && current < maxFilaments) {
          dynamicFilamentCountRef.current = Math.min(maxFilaments, Math.ceil(current * 1.1));
        }

        // Derive a quality scalar from current load level.
        // Lower quality means fewer filaments AND less expensive glow/shadow.
        qualityRef.current = Math.max(0.35, Math.min(1, dynamicFilamentCountRef.current / maxFilaments));
      }

      // Smooth amp from provided volume.
      const target = Math.max(0, Math.min(1, Number(volume) || 0));
      const smooth = ampRef.current * 0.82 + target * 0.18;
      ampRef.current = smooth;

      // Gentle motion even when idle.
      timeRef.current += 0.016;
      const t = timeRef.current;
      const amp = smooth;
      const reactiveScale = 1 + amp * (active ? 0.24 : 0.06);
      const q = qualityRef.current;

      ctx.clearRect(0, 0, cssSize, cssSize);

      // Background vignette
      const bg = ctx.createRadialGradient(cx, cy, 6, cx, cy, cssSize * 0.55);
      bg.addColorStop(0, "rgba(0,0,0,0.00)");
      bg.addColorStop(0.65, "rgba(0,0,0,0.30)");
      bg.addColorStop(1, "rgba(0,0,0,0.72)");
      ctx.fillStyle = bg;
      ctx.beginPath();
      ctx.arc(cx, cy, cssSize * 0.5, 0, Math.PI * 2);
      ctx.fill();

      // Metal bezel (adds realism with a single stroke)
      const outerR = baseRadii[baseRadii.length - 1] * reactiveScale;
      ctx.save();
      ctx.globalCompositeOperation = "source-over";
      const bezel = ctx.createRadialGradient(cx, cy, outerR * 0.72, cx, cy, outerR * 1.06);
      bezel.addColorStop(0, "rgba(0,0,0,0.00)");
      bezel.addColorStop(0.55, "rgba(255,255,255,0.08)");
      bezel.addColorStop(0.72, "rgba(255,255,255,0.14)");
      bezel.addColorStop(0.90, "rgba(0,0,0,0.28)");
      bezel.addColorStop(1, "rgba(0,0,0,0.00)");
      ctx.strokeStyle = bezel;
      ctx.globalAlpha = 0.95;
      ctx.lineWidth = Math.max(6, 16 * scale);
      ctx.beginPath();
      ctx.arc(cx, cy, outerR, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();

      // Subtle glassy rim highlight (adds depth)
      ctx.save();
      ctx.globalCompositeOperation = "screen";
      const rimR = baseRadii[baseRadii.length - 1] * reactiveScale;
      const rimGrad = ctx.createRadialGradient(cx, cy, rimR * 0.85, cx, cy, rimR * 1.03);
      rimGrad.addColorStop(0, "rgba(255,255,255,0.00)");
      rimGrad.addColorStop(0.55, "rgba(255,255,255,0.05)");
      rimGrad.addColorStop(0.78, "rgba(255,255,255,0.12)");
      rimGrad.addColorStop(1, "rgba(255,255,255,0.00)");
      ctx.strokeStyle = rimGrad;
      ctx.globalAlpha = 0.55;
      ctx.lineWidth = Math.max(4, 10 * scale);
      ctx.beginPath();
      ctx.arc(cx, cy, rimR, 0, Math.PI * 2);
      ctx.stroke();
      ctx.restore();

      // Ambient rings
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      for (let i = 0; i < baseRadii.length; i++) {
        const r = baseRadii[i] * reactiveScale;
        ctx.strokeStyle = `rgba(255,255,255,${0.015 + i * 0.01})`;
        ctx.lineWidth = Math.max(1, 1 * scale);
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.stroke();

        ctx.strokeStyle = `rgba(0,212,255,${0.02 + i * 0.012})`;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();

      // Shield arcs (cheaper than full SVG paths)
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.lineCap = "round";
      for (let i = 0; i < baseRadii.length; i++) {
        const r = baseRadii[i] * reactiveScale;
        const rot = (t * (0.25 + i * 0.06)) % (Math.PI * 2);
        const a0 = rot;
        const a1 = rot + (Math.PI * 1.55);

        ctx.shadowColor = color.ring;
        ctx.shadowBlur = (3 + amp * 10) * (0.40 + 0.60 * q);
        ctx.strokeStyle = color.ring;
        ctx.globalAlpha = (0.20 + (0.10 * (1 - i / baseRadii.length)) + amp * 0.18) * (0.75 + 0.25 * q);
        ctx.lineWidth = Math.max(0.75, (2 - i * 0.12) * scale);
        ctx.beginPath();
        ctx.arc(cx, cy, r, a0, a1);
        ctx.stroke();

        // highlight edge
        ctx.shadowBlur = 0;
        ctx.globalAlpha = 0.05 + amp * 0.18;
        ctx.strokeStyle = "rgba(255,255,255,1)";
        ctx.lineWidth = Math.max(0.6, 0.8 * scale);
        ctx.beginPath();
        ctx.arc(cx, cy, r, a0 + 0.12, a1 - 0.12);
        ctx.stroke();
      }
      ctx.restore();

      // Mechanical tick marks (adds realism with low cost)
      ctx.save();
      ctx.globalCompositeOperation = "screen";
      const ticks = 36;
      const tickR = baseRadii[baseRadii.length - 2] * reactiveScale;
      const tickLen = Math.max(4, 7 * scale);
      ctx.translate(cx, cy);
      // Keep ticks mostly static for a more grounded, mechanical feel.
      ctx.rotate(Math.PI / 48);
      ctx.lineCap = "round";
      ctx.strokeStyle = "rgba(255,255,255,0.22)";
      ctx.lineWidth = Math.max(1, 1 * scale);
      for (let i = 0; i < ticks; i++) {
        const a = (i / ticks) * Math.PI * 2;
        const x0 = Math.cos(a) * (tickR - tickLen);
        const y0 = Math.sin(a) * (tickR - tickLen);
        const x1 = Math.cos(a) * tickR;
        const y1 = Math.sin(a) * tickR;
        ctx.globalAlpha = (i % 6 === 0 ? 0.36 : 0.18) * (0.7 + 0.3 * q);
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
        ctx.stroke();
      }
      ctx.restore();

      // Filaments (cheap canvas curves)
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      ctx.lineCap = "round";
      const baseR = baseRadii[2] * reactiveScale;
      const filamentCount = dynamicFilamentCountRef.current || baseFilamentCount;
      for (let i = 0; i < filamentCount; i++) {
        const ang = (i / filamentCount) * Math.PI * 2 + t * 0.35;
        const wob = Math.sin(t * 1.6 + i * 0.25) * (2 + amp * 7);
        const innerR = baseR * 0.28;
        const outerR = baseR * 1.02;

        const ix = cx + innerR * Math.cos(ang + wob * 0.01);
        const iy = cy + innerR * Math.sin(ang + wob * 0.01);
        const ox = cx + outerR * Math.cos(ang + wob * 0.02);
        const oy = cy + outerR * Math.sin(ang + wob * 0.02);
        const mx = ix + (ox - ix) * 0.5 + Math.sin(t * 1.8 + i * 0.6) * (2.2 + amp * 4.5);
        const my = iy + (oy - iy) * 0.5 + Math.cos(t * 1.7 + i * 0.5) * (2.2 + amp * 4.5);

        const alpha = 0.045 + amp * 0.18 + (i % 3) * 0.010;
        ctx.strokeStyle = color.accent;
        ctx.globalAlpha = alpha;
        ctx.lineWidth = Math.max(0.6, (0.55 + amp * 1.25) * scale);
        ctx.shadowColor = color.accent;
        ctx.shadowBlur = (4 + amp * 11) * (0.30 + 0.70 * q);
        ctx.beginPath();
        ctx.moveTo(ix, iy);
        ctx.quadraticCurveTo(mx, my, ox, oy);
        ctx.stroke();
      }
      ctx.restore();

      // Core
      ctx.save();
      ctx.globalCompositeOperation = "lighter";
      const coreR = (22 + amp * 10) * reactiveScale;
      ctx.shadowColor = color.core;
      ctx.shadowBlur = (12 + amp * 22) * (0.55 + 0.45 * q);

      // Lens reflection sweep (cheap premium look)
      // Single moving arc with a soft gradient; scaled down on low quality.
      ctx.save();
      ctx.globalCompositeOperation = "screen";
      ctx.globalAlpha = (0.10 + amp * 0.10) * (0.45 + 0.55 * q);
      const sweepR = coreR * 2.05;
      const sweepA = (t * 0.55) % (Math.PI * 2);
      const sweepSpan = 0.55 + amp * 0.25; // radians
      const sx = cx + Math.cos(sweepA) * (sweepR * 0.15);
      const sy = cy + Math.sin(sweepA) * (sweepR * 0.15);
      const sweepGrad = ctx.createRadialGradient(sx, sy, 1, cx, cy, sweepR);
      sweepGrad.addColorStop(0, "rgba(255,255,255,0.22)");
      sweepGrad.addColorStop(0.45, "rgba(255,255,255,0.06)");
      sweepGrad.addColorStop(1, "rgba(255,255,255,0.00)");
      ctx.strokeStyle = sweepGrad;
      ctx.lineWidth = Math.max(2, ((6 + amp * 6) * (0.55 + 0.45 * q)) * scale);
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.arc(cx, cy, sweepR, sweepA - sweepSpan, sweepA + sweepSpan);
      ctx.stroke();
      ctx.restore();

      // Iris shutter pattern (faint rotating blades)
      // Cheap: a small number of clipped arc wedges with subtle shading.
      const irisR = coreR * 1.9;
      const bladeCount = 9;
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, cy, irisR, 0, Math.PI * 2);
      ctx.clip();
      ctx.translate(cx, cy);
      ctx.rotate(t * 0.35);
      for (let i = 0; i < bladeCount; i++) {
        const a0 = (i / bladeCount) * Math.PI * 2;
        const a1 = a0 + (Math.PI * 2) / bladeCount;
        const mid = (a0 + a1) / 2;

        const gx = Math.cos(mid) * (irisR * 0.35);
        const gy = Math.sin(mid) * (irisR * 0.35);
        const grad = ctx.createLinearGradient(gx, gy, gx * 0.15, gy * 0.15);
        grad.addColorStop(0, `rgba(255,255,255,${0.10 * q})`);
        grad.addColorStop(0.45, `rgba(255,255,255,${0.04 * q})`);
        grad.addColorStop(1, `rgba(0,0,0,${0.10 * (1 - 0.5 * q)})`);

        ctx.fillStyle = grad;
        ctx.globalAlpha = (0.22 + amp * 0.10) * (0.6 + 0.4 * q);
        ctx.beginPath();
        const rOuter = irisR;
        const rInner = irisR * (0.18 + 0.06 * Math.sin(t * 0.9 + i));
        ctx.arc(0, 0, rOuter, a0 + 0.06, a1 - 0.06);
        ctx.arc(0, 0, rInner, a1 - 0.08, a0 + 0.08, true);
        ctx.closePath();
        ctx.fill();

        ctx.strokeStyle = `rgba(255,255,255,${0.16 * q})`;
        ctx.lineWidth = Math.max(1, 1 * scale);
        ctx.globalAlpha = (0.10 + amp * 0.10) * (0.5 + 0.5 * q);
        ctx.beginPath();
        ctx.arc(0, 0, irisR * 0.96, a0 + 0.085, a0 + 0.16);
        ctx.stroke();
      }
      ctx.restore();

      const cg = ctx.createRadialGradient(cx, cy, 1, cx, cy, coreR * 2.2);
      cg.addColorStop(0, "rgba(255,255,255,0.65)");
      cg.addColorStop(0.25, color.core);
      cg.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = cg;
      ctx.globalAlpha = 0.85;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR * 2.0, 0, Math.PI * 2);
      ctx.fill();

      ctx.globalAlpha = 0.9;
      ctx.fillStyle = color.core;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();

      ctx.globalAlpha = 0.9;
      ctx.fillStyle = "rgba(255,255,255,1)";
      ctx.beginPath();
      ctx.arc(cx, cy, 2 + amp * 6, 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();

      // Film grain overlay (cheap: precomputed texture)
      const noiseCanvas = noiseCanvasRef.current;
      if (noiseCanvas) {
        ctx.save();
        ctx.globalCompositeOperation = "overlay";
        ctx.globalAlpha = (0.035 + amp * 0.02) * q;
        const off = ((t * 60) | 0) % noiseCanvas.width;
        ctx.drawImage(noiseCanvas, -off, -off, cssSize + noiseCanvas.width, cssSize + noiseCanvas.height);
        ctx.restore();
      }

      // Micro-scratch sweep overlay (adds realism, extremely low cost)
      const scratches = scratchesRef.current;
      if (scratches && scratches.length) {
        ctx.save();
        ctx.globalCompositeOperation = "screen";
        // Clip to a donut region so scratches sit on the "glass" ring.
        const outerR = baseRadii[baseRadii.length - 1] * reactiveScale;
        const innerR = baseRadii[2] * reactiveScale;
        ctx.beginPath();
        ctx.arc(cx, cy, outerR, 0, Math.PI * 2);
        ctx.arc(cx, cy, innerR, 0, Math.PI * 2, true);
        ctx.closePath();
        ctx.clip();

        ctx.translate(cx, cy);
        ctx.rotate(t * 0.18);
        // Subtle tint; avoid hard-coded new palette beyond whites.
        ctx.strokeStyle = "rgba(255,255,255,1)";
        for (let i = 0; i < scratches.length; i++) {
          const s = scratches[i];
          const rr = outerR * s.r0;
          const a0 = s.a + Math.sin(t * 0.6 + i) * 0.02;
          const x0 = Math.cos(a0) * rr;
          const y0 = Math.sin(a0) * rr;
          const x1 = Math.cos(a0) * (rr + outerR * s.len);
          const y1 = Math.sin(a0) * (rr + outerR * s.len);
          ctx.globalAlpha = s.alpha * (0.35 + 0.65 * q) * (0.55 + 0.45 * (0.6 + amp * 0.4));
          ctx.lineWidth = Math.max(0.6, s.width * scale);
          ctx.beginPath();
          ctx.moveTo(x0, y0);
          ctx.lineTo(x1, y1);
          ctx.stroke();
        }
        ctx.restore();
      }

      rafRef.current = requestAnimationFrame(draw);
    };

    rafRef.current = requestAnimationFrame(draw);
    return () => {
      try { cancelAnimationFrame(rafRef.current); } catch {}
      window.removeEventListener("resize", resize);
    };
  }, [active, baseFilamentCount, color, size, volume]);

  return (
    <div className={`arc-reactor-root ${active ? "active" : "idle"} emotion-${emotion}`} style={{ width: `${size}px`, height: `${size}px` }}>
      <canvas ref={canvasRef} className="arc-reactor-canvas" />

      {showCaption && (
        <div className="reactor-caption">
          <div className="status-dot" style={{ background: color.core }} />
          <div className="status-text">
            {emotion === "calm" && (active ? "Listening (always-on)" : "Idle")}
            {emotion === "analyzing" && "Analyzing"}
            {emotion === "critical" && "Critical"}
          </div>
        </div>
      )}
    </div>
  );
}
