import React, { useEffect, useRef, useState } from "react";
import "./DottedRings.css";

/**
 * DottedRings
 * Props:
 *  - audioLevel: number (0..1) driving the animation intensity
 *  - status: optional string for styling based on bot status
 */
const DottedRings = ({ audioLevel = 0, status = "listening" }) => {
  const [level, setLevel] = useState(0);
  const raf = useRef(null);

  // Smooth the incoming audioLevel for nicer animation
  useEffect(() => {
    let mounted = true;
    let current = level;
    const target = Math.max(0, Math.min(1, audioLevel));

    const step = () => {
      current = current + (target - current) * 0.12; // simple lerp
      if (mounted) setLevel(current);
      raf.current = requestAnimationFrame(step);
    };

    raf.current = requestAnimationFrame(step);
    return () => {
      mounted = false;
      if (raf.current) cancelAnimationFrame(raf.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [audioLevel]);

  // derive per-ring scales/opacity from smoothed level
  const base = 1;
  const max = 1.6;
  const scale1 = base + (max - base) * level;
  const scale2 = base + (max - base) * Math.max(level - 0.08, 0);
  const scale3 = base + (max - base) * Math.max(level - 0.2, 0);

  const glow = Math.min(0.25 + level * 0.95, 1);

  return (
    <div className="dotted-rings" data-status={status}>
      <div
        className="ring-wrap ring1-wrap"
        style={{ "--scale": scale1 }}
      >
        <div
          className="ring-inner ring1"
          style={{ opacity: 0.5 + level * 0.5, boxShadow: `0 0 ${18 * level}px rgba(0,188,212,${glow})` }}
        />
      </div>

      <div
        className="ring-wrap ring2-wrap"
        style={{ "--scale": scale2 }}
      >
        <div
          className="ring-inner ring2"
          style={{ opacity: 0.45 + level * 0.5, boxShadow: `0 0 ${14 * level}px rgba(0,188,212,${glow})` }}
        />
      </div>

      <div
        className="ring-wrap ring3-wrap"
        style={{ "--scale": scale3 }}
      >
        <div
          className="ring-inner ring3"
          style={{ opacity: 0.4 + level * 0.5, boxShadow: `0 0 ${10 * level}px rgba(0,188,212,${glow})` }}
        />
      </div>
    </div>
  );
};

export default DottedRings;