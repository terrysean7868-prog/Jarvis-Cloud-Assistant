import React, { useEffect, useMemo, useState } from "react";
import "./AtomicBackground.css";

const RING_COUNT = 7;
const RANDOMIZE_MS = 5000;

function randomElectronCounts() {
  return Array.from({ length: RING_COUNT }, () => 2 + Math.floor(Math.random() * 4));
}

function colorForEmotion(emotion) {
  switch (emotion) {
    case "calm":
      return { r: 8, g: 200, b: 220 };
    case "analyzing":
      return { r: 220, g: 190, b: 40 };
    case "action":
      return { r: 90, g: 255, b: 130 };
    case "critical":
      return { r: 255, g: 80, b: 90 };
    default:
      return { r: 8, g: 200, b: 220 };
  }
}

export default function AtomicBackground({ emotion = "calm", wakePulse = false, volume = 0 }) {
  // This component avoids any high-FPS JS animation loop.
  // Rotation is done with CSS keyframes (browser-optimized), and the only JS work
  // is re-randomizing electron counts on a low-frequency interval.
  const [counts, setCounts] = useState(() => randomElectronCounts());

  useEffect(() => {
    const id = window.setInterval(() => {
      setCounts(randomElectronCounts());
    }, RANDOMIZE_MS);
    return () => window.clearInterval(id);
  }, []);

  const rgb = useMemo(() => colorForEmotion(emotion), [emotion]);
  const safeVolume = Math.max(0, Math.min(1, Number(volume) || 0));

  // Match the worker geometry: inner ~14% and outer ~42% of min dimension.
  const innerVmin = 14;
  const outerVmin = 42;
  const gapVmin = (outerVmin - innerVmin) / Math.max(1, RING_COUNT - 1);

  return (
    <div
      className="atomic-bg"
      aria-hidden="true"
      style={{
        "--a-r": rgb.r,
        "--a-g": rgb.g,
        "--a-b": rgb.b,
        "--a-wake": wakePulse ? 1 : 0,
        "--a-vol": safeVolume,
      }}
    >
      <div className="atomic-center">
        <div className="atomic-nucleus" />

        {Array.from({ length: RING_COUNT }).map((_, i) => {
          const ringSizeVmin = innerVmin + i * gapVmin;
          const durS = 9.5 + i * 1.9;
          const reverse = i % 2 !== 0;
          const n = counts[i] || 2;

          return (
            <div
              key={i}
              className="atomic-ring"
              style={{
                "--ring-size": `${ringSizeVmin}vmin`,
                "--ring-alpha": Math.max(0.06, 0.18 - i * 0.012),
              }}
            >
              <div className="atomic-ring-outline" />

              <div
                className={`atomic-electrons ${reverse ? "atomic-electrons-rev" : ""}`}
                style={{ "--dur": `${durS}s` }}
              >
                {Array.from({ length: n }).map((__, j) => (
                  <div
                    key={j}
                    className="atomic-electron"
                    style={{ "--e-ang": `${(j / n) * 360}deg` }}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
