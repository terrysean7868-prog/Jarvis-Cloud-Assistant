import React, { useMemo } from "react";
import "./AtomicBackground.css";

// React-logo-like: multiple rings around one center.
const RING_COUNT = 7;

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
  // Rotation is done with CSS keyframes (browser-optimized).

  const rgb = useMemo(() => colorForEmotion(emotion), [emotion]);
  const safeVolume = Math.max(0, Math.min(1, Number(volume) || 0));

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
          // Concentric rings: keep clear spacing so the rings don't visually merge.
          // Same radius for all rings (as requested). 3D depth comes from rotation/tilt, not size.
          const ringSizeVmin = 56;

          // De-sync motion so it feels continuous/premium.
          const ringSpinS = 18.5 + i * 2.9;
          const ringDelayS = -(i * 1.4);
          const durS = 10.9 + i * 2.6;
          const electronDelayS = -(i * 0.85);
          const reverse = i % 2 !== 0;

          // Extra 3D gyro rotations (X and Y) so the orbit rotates through all axes.
          const gyroXD = 26 + i * 3.1;
          const gyroYD = 19 + i * 2.4;
          const gyroDelayS = -(i * 0.9);

          // 3D orbits: 3 main planes + slight variation per ring.
          const orbit = i % 3;
          const tiltX = orbit === 0 ? 62 : orbit === 1 ? 58 : 64;
          const tiltY = orbit === 0 ? -18 : orbit === 1 ? 22 : 58;
          const tiltZ = `${orbit * 60}deg`;

          // Keep true circle shape (no ellipse squash).
          const sx = 1;
          const sy = 1;

          // Slight depth separation.
          // Spread rings through depth so the 3D rotations read clearly.
          const centerIdx = (RING_COUNT - 1) / 2;
          const ringZ = (i - centerIdx) * 7; // px

          // One bright electron per ring + a few glow nodes.
          const nElectrons = 1;
          const nodeAngles = [20, 160, 280, 330];

          return (
            <div
              key={i}
              className="atomic-ring"
              style={{
                "--ring-size": `${ringSizeVmin}vmin`,
                "--ring-alpha": Math.max(0.06, 0.17 - i * 0.012),
                "--tilt-x": `${tiltX}deg`,
                "--tilt-y": `${tiltY}deg`,
                "--tilt-z": tiltZ,
                "--gyro-x-dur": `${gyroXD}s`,
                "--gyro-y-dur": `${gyroYD}s`,
                "--gyro-delay": `${gyroDelayS}s`,
                "--ring-sx": sx,
                "--ring-sy": sy,
                "--ring-dur": `${ringSpinS}s`,
                "--ring-delay": `${ringDelayS}s`,
                "--ring-z": `${ringZ}px`,
              }}
            >
              <div className="atomic-ring-gyro-x">
                <div className="atomic-ring-gyro-y">
                  <div className="atomic-ring-rotator">
                    <div className="atomic-ring-shape">
                      <div className="atomic-ring-outline" />

                      <div className="atomic-ring-nodes">
                        {nodeAngles.map((ang, k) => (
                          <div
                            key={k}
                            className="atomic-ring-node"
                            style={{ "--n-ang": `${ang + i * 11}deg`, "--n-seed": k + i * 7 }}
                          />
                        ))}
                      </div>

                      <div
                        className={`atomic-electrons ${reverse ? "atomic-electrons-rev" : ""}`}
                        style={{ "--dur": `${durS}s`, "--e-delay": `${electronDelayS}s` }}
                      >
                        {Array.from({ length: nElectrons }).map((__, j) => (
                          <div
                            key={j}
                            className="atomic-electron"
                            style={{ "--e-ang": `${(j / nElectrons) * 360 + i * 80}deg` }}
                          />
                        ))}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
