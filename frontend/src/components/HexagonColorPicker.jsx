import React, { useEffect, useMemo, useRef } from "react";

function clamp(n, min, max) {
  return Math.max(min, Math.min(max, n));
}

function normalizeHex(hex) {
  const s = (hex || "").toString().trim();
  if (!s) return null;
  const h = s.startsWith("#") ? s : `#${s}`;
  if (/^#[0-9a-f]{3}$/i.test(h)) {
    return `#${h[1]}${h[1]}${h[2]}${h[2]}${h[3]}${h[3]}`.toLowerCase();
  }
  if (/^#[0-9a-f]{6}$/i.test(h)) return h.toLowerCase();
  return null;
}

function hexToRgb(hex) {
  const h = normalizeHex(hex);
  if (!h) return null;
  return {
    r: parseInt(h.slice(1, 3), 16),
    g: parseInt(h.slice(3, 5), 16),
    b: parseInt(h.slice(5, 7), 16),
  };
}

function rgbToHex({ r, g, b }) {
  const to = (n) => clamp(Math.round(n), 0, 255).toString(16).padStart(2, "0");
  return `#${to(r)}${to(g)}${to(b)}`;
}

function hsvToRgb(h, s, v) {
  const hh = ((h % 360) + 360) % 360;
  const c = v * s;
  const x = c * (1 - Math.abs(((hh / 60) % 2) - 1));
  const m = v - c;

  let rp = 0, gp = 0, bp = 0;
  if (hh < 60) { rp = c; gp = x; bp = 0; }
  else if (hh < 120) { rp = x; gp = c; bp = 0; }
  else if (hh < 180) { rp = 0; gp = c; bp = x; }
  else if (hh < 240) { rp = 0; gp = x; bp = c; }
  else if (hh < 300) { rp = x; gp = 0; bp = c; }
  else { rp = c; gp = 0; bp = x; }

  return {
    r: (rp + m) * 255,
    g: (gp + m) * 255,
    b: (bp + m) * 255,
  };
}

function rgbToHsv({ r, g, b }) {
  const rr = r / 255;
  const gg = g / 255;
  const bb = b / 255;
  const max = Math.max(rr, gg, bb);
  const min = Math.min(rr, gg, bb);
  const d = max - min;

  let h = 0;
  if (d === 0) h = 0;
  else if (max === rr) h = 60 * (((gg - bb) / d) % 6);
  else if (max === gg) h = 60 * (((bb - rr) / d) + 2);
  else h = 60 * (((rr - gg) / d) + 4);

  if (h < 0) h += 360;
  const s = max === 0 ? 0 : d / max;
  const v = max;
  return { h, s, v };
}

function pointInPoly(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i].x, yi = poly[i].y;
    const xj = poly[j].x, yj = poly[j].y;
    const intersect = ((yi > y) !== (yj > y)) && (x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-9) + xi);
    if (intersect) inside = !inside;
  }
  return inside;
}

function makeHexVertices(cx, cy, r) {
  // Flat-top hex
  const verts = [];
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i;
    verts.push({ x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
  }
  return verts;
}

function drawHexPath(ctx, cx, cy, r) {
  ctx.beginPath();
  for (let i = 0; i < 6; i++) {
    const a = (Math.PI / 3) * i;
    const x = cx + r * Math.cos(a);
    const y = cy + r * Math.sin(a);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

function positionToColor(x, y, cx, cy, r) {
  const dx = x - cx;
  const dy = y - cy;
  const ang = Math.atan2(dy, dx);
  const hue = ((ang * 180) / Math.PI + 360) % 360;
  const dist = Math.sqrt(dx * dx + dy * dy);
  const sat = clamp(dist / r, 0, 1);
  const rgb = hsvToRgb(hue, sat, 1);
  return rgbToHex(rgb);
}

function colorToPosition(hex, cx, cy, r) {
  const rgb = hexToRgb(hex);
  if (!rgb) return { x: cx, y: cy };
  const hsv = rgbToHsv(rgb);
  const ang = (hsv.h * Math.PI) / 180;
  const dist = clamp(hsv.s, 0, 1) * r;
  return { x: cx + Math.cos(ang) * dist, y: cy + Math.sin(ang) * dist };
}

export default function HexagonColorPicker({ value = "#00eaff", size = 180, onChange }) {
  const canvasRef = useRef(null);
  const paletteRef = useRef(null);

  const cfg = useMemo(() => {
    const cssSize = Math.max(140, Number(size) || 180);
    const padding = Math.max(8, Math.floor(cssSize * 0.06));
    const r = Math.floor((cssSize - padding * 2) / 2);
    const cx = Math.floor(cssSize / 2);
    const cy = Math.floor(cssSize / 2);
    const verts = makeHexVertices(cx, cy, r);
    return { cssSize, padding, r, cx, cy, verts };
  }, [size]);

  const draw = useMemo(() => {
    return () => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const dpr = Math.max(1, Math.min(2, window.devicePixelRatio || 1));

      canvas.width = Math.floor(cfg.cssSize * dpr);
      canvas.height = Math.floor(cfg.cssSize * dpr);
      canvas.style.width = `${cfg.cssSize}px`;
      canvas.style.height = `${cfg.cssSize}px`;

      const ctx = canvas.getContext("2d", { alpha: true, desynchronized: true });
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, cfg.cssSize, cfg.cssSize);

      // Draw cached palette
      if (paletteRef.current) {
        ctx.drawImage(paletteRef.current, 0, 0);
      }

      // Draw selection marker
      const v = normalizeHex(value) || "#00eaff";
      const p = colorToPosition(v, cfg.cx, cfg.cy, cfg.r);

      ctx.save();
      ctx.globalCompositeOperation = "source-over";
      ctx.lineWidth = 3;
      ctx.shadowBlur = 10;
      ctx.shadowColor = "rgba(0,0,0,0.6)";
      ctx.strokeStyle = "rgba(255,255,255,0.95)";
      ctx.fillStyle = v;
      ctx.beginPath();
      ctx.arc(p.x, p.y, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    };
  }, [cfg, value]);

  useEffect(() => {
    // Build honeycomb palette once per size.
    const off = document.createElement("canvas");
    off.width = cfg.cssSize;
    off.height = cfg.cssSize;
    const ctx = off.getContext("2d", { alpha: true });
    if (!ctx) return;

    ctx.clearRect(0, 0, cfg.cssSize, cfg.cssSize);

    // Clip to big hex
    ctx.save();
    drawHexPath(ctx, cfg.cx, cfg.cy, cfg.r);
    ctx.clip();

    // Honeycomb grid
    const cellR = Math.max(5, Math.floor(cfg.cssSize * 0.03));
    const h = Math.sqrt(3) * cellR;

    // pointy-top grid spacing
    const dx = (3 / 2) * cellR;
    const dy = h;

    // We'll iterate over a bounding box around the big hex.
    const minX = cfg.cx - cfg.r - cellR;
    const maxX = cfg.cx + cfg.r + cellR;
    const minY = cfg.cy - cfg.r - cellR;
    const maxY = cfg.cy + cfg.r + cellR;

    // Precompute big hex poly for inclusion test
    const bigPoly = cfg.verts;

    // No outlines so colors touch (no gaps between cells).

    let row = 0;
    for (let y = minY; y <= maxY; y += dy, row++) {
      const xOffset = (row % 2) * dx;
      for (let x = minX; x <= maxX; x += 3 * cellR) {
        const cx = x + xOffset;
        const cy = y;

        // Include only cells whose center lies inside the big hex
        if (!pointInPoly(cx, cy, bigPoly)) continue;

        const col = positionToColor(cx, cy, cfg.cx, cfg.cy, cfg.r);
        ctx.fillStyle = col;
        // Draw small hex (pointy-top) to look like honeycomb
        ctx.beginPath();
        for (let i = 0; i < 6; i++) {
          const a = (Math.PI / 3) * i + Math.PI / 6;
          const px = cx + cellR * Math.cos(a);
          const py = cy + cellR * Math.sin(a);
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.fill();
      }
    }

    ctx.restore();

    paletteRef.current = off;
    draw();
  }, [cfg, draw]);

  useEffect(() => {
    draw();
  }, [draw]);

  const pickAtClientPoint = (clientX, clientY) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left;
    const y = clientY - rect.top;

    // Ignore outside the big hex
    if (!pointInPoly(x, y, cfg.verts)) return;

    const hex = positionToColor(x, y, cfg.cx, cfg.cy, cfg.r);
    if (typeof onChange === "function") onChange(hex);
  };

  const onPointerDown = (e) => {
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch {}
    pickAtClientPoint(e.clientX, e.clientY);
  };

  const onPointerMove = (e) => {
    if (e.buttons !== 1) return;
    pickAtClientPoint(e.clientX, e.clientY);
  };

  return (
    <canvas
      ref={canvasRef}
      role="application"
      aria-label="Hexagon color picker"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      style={{ touchAction: "none", cursor: "crosshair", display: "block" }}
    />
  );
}
