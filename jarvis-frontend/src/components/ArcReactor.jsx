// src/components/ArcReactor.jsx
import React, { useEffect, useRef, useState } from "react";
import "../styles/arcReactor.css";

export default function ArcReactor({
  active = false,
  emotion = "calm",
  size = 340,
}) {
  const svgRef = useRef(null);
  const audioCtxRef = useRef(null);
  const analyserRef = useRef(null);
  const dataArrayRef = useRef(null);
  const rafRef = useRef(null);
  const micStreamRef = useRef(null);

  const [amp, setAmp] = useState(0); // 0..1 amplitude
  const [time, setTime] = useState(0); // animation time
  const [rotations, setRotations] = useState(
    Array.from({ length: 15 }, () => Math.random() * 360)
  );

  // Color palette (can expand)
  const colorMap = {
    calm: { core: "#00ffc8", ring: "#00d4ff", accent: "#00ffc8" },
    analyzing: { core: "#ffd24d", ring: "#ff9f43", accent: "#ffd24d" },
    critical: { core: "#ff4d4f", ring: "#ff6b6b", accent: "#ff4d4f" },
  };

  const color = colorMap[emotion] || colorMap.calm;

  // Setup microphone -> analyser when component mounts and active
  useEffect(() => {
    let started = false;

    async function initMic() {
      try {
        // Mobile-friendly audio constraints
        const audioConstraints = {
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            sampleRate: { ideal: 16000 }
          },
          video: false
        };
        
        const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);
        micStreamRef.current = stream;
        
        // Use webkitAudioContext for Safari compatibility
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        audioCtxRef.current = new AudioContext();
        
        const source = audioCtxRef.current.createMediaStreamSource(stream);
        const analyser = audioCtxRef.current.createAnalyser();
        analyser.fftSize = 256;
        source.connect(analyser);
        analyserRef.current = analyser;
        const bufferLen = analyser.frequencyBinCount;
        dataArrayRef.current = new Uint8Array(bufferLen);
        runAnalyser();
      } catch (e) {
        console.warn("ArcReactor: microphone init failed:", e?.message || e);
      }
    }


    function runAnalyser() {
      const analyser = analyserRef.current;
      const data = dataArrayRef.current;
      if (!analyser || !data) return;
      analyser.getByteTimeDomainData(data);
      // compute normalized RMS-like amplitude
      let sum = 0;
      for (let i = 0; i < data.length; i++) {
        const v = (data[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / data.length);
      // clamp and smooth
      setAmp((prev) => Math.min(1, prev * 0.85 + rms * 0.5));
    }

    // Poll the analyser at animation frame
    function frame() {
      setTime((t) => t + 0.016);
      if (analyserRef.current) runAnalyser();
      // slowly change rotations for life
      setRotations((r) => r.map((v, idx) => (v + (0.2 + idx * 0.04)) % 360));
      rafRef.current = requestAnimationFrame(frame);
    }

    // Start if active
    if (active) {
      if (!audioCtxRef.current) {
        initMic();
      }
      if (!rafRef.current) {
        rafRef.current = requestAnimationFrame(frame);
      }
    } else {
      // if not active, create a gentle idle animation and stop mic polling
      if (!rafRef.current) {
        rafRef.current = requestAnimationFrame(frame);
      }
      // reduce amplitude gradually
      setAmp((p) => Math.max(0, p * 0.9));
    }

    return () => {
      // cleanup if component unmounts or active toggles
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      // do not immediately stop mic — keep it running while mounted; user can decide.
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]); // re-run when active toggles

  // Handle complete unmount cleanup (stop mic)
  useEffect(() => {
    return () => {
      if (micStreamRef.current) {
        micStreamRef.current.getTracks().forEach((t) => t.stop());
        micStreamRef.current = null;
      }
      if (audioCtxRef.current && audioCtxRef.current.state !== "closed") {
        try {
          audioCtxRef.current.close();
        } catch { }
        audioCtxRef.current = null;
      }
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, []);

  // Utility: describe an arc path (circular arc)
  function arcPath(cx, cy, r, startAngle, endAngle) {
    const start = polarToCartesian(cx, cy, r, endAngle);
    const end = polarToCartesian(cx, cy, r, startAngle);
    const largeArcFlag = endAngle - startAngle <= 180 ? "0" : "1";
    return [
      "M", start.x, start.y,
      "A", r, r, 0, largeArcFlag, 0, end.x, end.y
    ].join(" ");
  }

  function polarToCartesian(cx, cy, r, angleDeg) {
    const a = ((angleDeg - 90) * Math.PI) / 180.0;
    return { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) };
  }

  // Shield/arc config
  const shieldCount = 15;
  const baseRadii = [72, 98, 128, 156, 186, 220]; // pixels
  const width = size;
  const height = size;
  const cx = width / 2;
  const cy = height / 2;

  // compute reactive scale from amplitude (0..1) -> 1..1.25
  const reactiveScale = 1 + amp * (active ? 0.28 : 0.06);

  // generate segments with slight noise to make them curved / snake-like
  function generateSegmentPath(r, segIndex, rotation) {
    // We'll make an arc spanning 260..320 degrees with a wobble path
    const segSpan = 260 + (segIndex * 4);
    const start = (rotation + segIndex * 10) % 360;
    const end = (start + segSpan) % 360;
    // build a slightly perturbed poly-arc by sampling many points and using a smooth path
    const samples = 64;
    const points = [];
    for (let i = 0; i <= samples; i++) {
      const t = i / samples;
      // angle from start -> end (wrap handled)
      let ang = start + (end - start) * t;
      // add perlin-like simple noise (sin + cos)
      const wobble = Math.sin((t + time * 0.6) * Math.PI * 4 + segIndex) * (2 + amp * 6);
      ang += wobble;
      const p = polarToCartesian(cx, cy, r * reactiveScale, ang);
      points.push(p);
    }
    // convert points into a smooth SVG path (Catmull-Rom -> Bezier approximation)
    return pointsToSmoothPath(points);
  }

  // Convert points array into smooth path string
  function pointsToSmoothPath(pts) {
    if (pts.length === 0) return "";
    // Move to first
    let d = `M ${pts[0].x.toFixed(2)} ${pts[0].y.toFixed(2)}`;
    // Use simple quadratic smoothing between points
    for (let i = 1; i < pts.length - 1; i++) {
      const xc = (pts[i].x + pts[i + 1].x) / 2;
      const yc = (pts[i].y + pts[i + 1].y) / 2;
      d += ` Q ${pts[i].x.toFixed(2)} ${pts[i].y.toFixed(2)} ${xc.toFixed(2)} ${yc.toFixed(2)}`;
    }
    // last segment
    const last = pts[pts.length - 1];
    d += ` T ${last.x.toFixed(2)} ${last.y.toFixed(2)}`;
    return d;
  }

  // Filaments: generate N filaments connecting core -> ring points
  const filamentCount = 200;
  function filamentPoints(index, radius, rotation) {
    const angle = (index / filamentCount) * 360 + rotation;

    // Inner anchor (near core)
    const inner = polarToCartesian(
      cx,
      cy,
      radius * 0.35 * reactiveScale,
      angle + Math.sin(time + index * 0.7) * 3
    );

    // Outer endpoint — keep slightly inside shield radius
    const maxOuter = radius * 0.95 * reactiveScale;
    const outer = polarToCartesian(
      cx,
      cy,
      maxOuter,
      angle + Math.cos(time * 0.8 + index * 0.5) * 4
    );

    // Mid control point with subtle curved motion (no large deviation)
    const mid = {
      x: inner.x + (outer.x - inner.x) * 0.5 + Math.sin(index * 0.7 + time * 1.8) * 4,
      y: inner.y + (outer.y - inner.y) * 0.5 + Math.cos(index * 0.9 + time * 1.6) * 4,
    };

    return { inner, mid, outer };
  }


  // core gradient & glow radii
  const coreRadius = 25 * reactiveScale;

  // render
  return (
    <div
      className={`arc-reactor-root ${active ? "active" : "idle"} emotion-${emotion}`}
      style={{ width: `${size}px`, height: `${size}px` }}
    >
      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="xMidYMid meet"
        className="arc-reactor-svg"
      >
        {/* background radial vignette */}
        <defs>
          <radialGradient id="coreGrad" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor={color.core} stopOpacity="1" />
            <stop offset="40%" stopColor={color.core} stopOpacity="0.85" />
            <stop offset="100%" stopColor="#000000" stopOpacity="0.0" />
          </radialGradient>

          <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
            <feGaussianBlur stdDeviation={10 + amp * 18} result="coloredBlur" />
            <feMerge>
              <feMergeNode in="coloredBlur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* outer ambient rings (soft) */}
        {baseRadii.map((r, idx) => (
          <circle
            key={`soft-${idx}`}
            cx={cx}
            cy={cy}
            r={r * reactiveScale}
            fill="none"
            stroke={color.ring}
            strokeOpacity={0.03 + idx * 0.02}
            strokeWidth={1}
            className="ambient-ring"
          />
        ))}

        {/* shield segments (curved, segmented strokes) */}
        <g className="shield-group" filter="url(#glow)">
          {baseRadii.map((r, sIdx) => {
            // multiple arcs per radius for variety
            const arcsPerRadius = 2;
            return Array.from({ length: arcsPerRadius }).map((_, arcIdx) => {
              const segId = `shield-${sIdx}-${arcIdx}`;
              const rot = rotations[(sIdx + arcIdx) % rotations.length];
              const pathD = generateSegmentPath(r + arcIdx * 6, sIdx + arcIdx, rot);
              const dash = `${6 + Math.sin((time + sIdx) * 2) * 4}, ${20 + sIdx * 6}`;
              return (
                <path
                  key={segId}
                  d={pathD}
                  stroke={color.ring}
                  strokeWidth={1.8 - sIdx * 0.12}
                  fill="none"
                  strokeLinecap="round"
                  strokeOpacity={0.85 - sIdx * 0.08}
                  style={{
                    transformOrigin: `${cx}px ${cy}px`,
                    // independent rotation animation using inline style for smooth GPU transforms
                    transform: `rotate(${rot * (0.2 + (sIdx / 8))}deg)`,
                    transition: "transform 0.6s linear",
                    strokeDasharray: dash,
                    mixBlendMode: "screen",
                    filter: `url(#glow)`,
                  }}
                />
              );
            });
          })}
        </g>

        {/* filaments */}
        <g className="filament-group">
          {Array.from({ length: filamentCount }).map((_, i) => {
            const baseR = baseRadii[2];
            const rot = rotations[i % rotations.length];
            const { inner, mid, outer } = filamentPoints(i, baseR, rot);
            const opacity = 0.15 + amp * 0.9 * (0.6 + (i % 3) * 0.15);
            const strokeW = 0.6 + amp * 2.2 * (0.6 + ((i + 2) % 4) * 0.25);
            return (
              <path
                key={`fil-${i}`}
                d={`M ${inner.x.toFixed(2)} ${inner.y.toFixed(2)} Q ${mid.x.toFixed(
                  2
                )} ${mid.y.toFixed(2)} ${outer.x.toFixed(2)} ${outer.y.toFixed(2)}`}
                stroke={color.accent}
                strokeWidth={strokeW}
                strokeOpacity={opacity}
                fill="none"
                strokeLinecap="round"
                style={{ mixBlendMode: "screen", filter: `url(#glow)` }}
              />
            );
          })}
        </g>

        {/* inner concentric rings */}
        <g className="inner-rings">
          {Array.from({ length: 5 }).map((_, i) => (
            <circle
              key={`inner-${i}`}
              cx={cx}
              cy={cy}
              r={(coreRadius * 1.2 + i * 8) * (1 + Math.sin(time * (0.6 + i * 0.15)) * 0.02)}
              fill="none"
              stroke={color.core}
              strokeOpacity={0.12 + i * 0.06}
              strokeWidth={1}
              style={{
                transformOrigin: `${cx}px ${cy}px`,
                transform: `rotate(${time * (6 + i * 12)}deg)`,
              }}
            />
          ))}
        </g>

        {/* core (gradient + center glow) */}
        <g className="core" transform={`translate(0,0)`}>
          <circle
            cx={cx}
            cy={cy}
            r={coreRadius * 1.8}
            fill="url(#coreGrad)"
            opacity={0.85}
            style={{
              filter: `url(#glow)`,
              transition: "r 0.24s linear",
            }}
          />
          <circle
            cx={cx}
            cy={cy}
            r={coreRadius}
            fill={color.core}
            style={{ mixBlendMode: "screen", filter: `url(#glow)` }}
          />
          {/* tiny center spark */}
          <circle
            cx={cx}
            cy={cy}
            r={2 + amp * 6}
            fill="#ffffff"
            opacity={0.9}
            style={{ transition: "r 0.08s linear" }}
          />
        </g>
      </svg>

      {/* subtle HUD caption */}
      <div className="reactor-caption">
        <div className="status-dot" style={{ background: color.core }} />
        <div className="status-text">
          {emotion === "calm" && (active ? "Listening (always-on)" : "Idle")}
          {emotion === "analyzing" && "Analyzing"}
          {emotion === "critical" && "Critical"}
        </div>
      </div>
    </div>
  );
}
