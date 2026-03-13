import React, { useEffect, useRef, useState } from "react";
import "./DottedRings.css";

/**
 * Enhanced DottedRings with multiple rings
 * Props:
 *  - audioLevel: number (0..1) driving the animation intensity
 *  - status: optional string for styling based on bot status
 *  - ringCount: number of rings to display (default: 5)
 */
const DottedRings = ({ audioLevel = 0, status = "listening", ringCount = 5 }) => {
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

  // Color variations for different rings
  const ringColors = [
    { main: "rgba(0,188,212", accent: "rgba(0,255,200" }, // Cyan
    { main: "rgba(0,255,200", accent: "rgba(0,212,255" }, // Mint
    { main: "rgba(0,212,255", accent: "rgba(100,255,150" }, // Cyan-blue
    { main: "rgba(100,255,150", accent: "rgba(255,200,40" }, // Green-yellow
    { main: "rgba(255,200,40", accent: "rgba(255,159,67" }, // Yellow-orange
  ];

  // Generate rings dynamically
  const rings = Array.from({ length: ringCount }, (_, i) => {
    const base = 1;
    const max = 1.6;
    const delay = i * 0.08;
    const scale = base + (max - base) * Math.max(level - delay, 0);
    const opacity = Math.max(0.3, 0.5 + level * 0.5 - i * 0.1);
    const glow = Math.min(0.25 + level * 0.95, 1);
    const color = ringColors[i % ringColors.length];
    const size = 80 + i * 40; // Progressive sizing
    const rotationSpeed = 8 + i * 2; // Different rotation speeds
    const animationDelay = i * 0.3;

    return {
      id: i,
      scale,
      opacity,
      glow,
      color,
      size,
      rotationSpeed,
      animationDelay,
    };
  });

  return (
    <div className="dotted-rings" data-status={status}>
      {rings.map((ring) => (
        <div
          key={ring.id}
          className={`ring-wrap ring${ring.id + 1}-wrap`}
          style={{
            "--scale": ring.scale,
            "--size": `${ring.size}px`,
            "--rotation-speed": `${ring.rotationSpeed}s`,
            "--delay": `${ring.animationDelay}s`,
          }}
        >
          <div
            className={`ring-inner ring${ring.id + 1}`}
            style={{
              opacity: ring.opacity,
              boxShadow: `0 0 ${(20 - ring.id * 2) * level}px ${ring.color.main},${ring.opacity}), inset 0 0 ${(15 - ring.id) * level}px ${ring.color.accent},${ring.opacity * 0.5})`,
              borderColor: `${ring.color.main},${0.6 + level * 0.4})`,
              width: "var(--size)",
              height: "var(--size)",
              animation: `spin-cw var(--rotation-speed) linear infinite, pulse-glow ${3 + ring.id * 0.5}s ease-in-out infinite`,
              animationDelay: `var(--delay)`,
            }}
          />
        </div>
      ))}
    </div>
  );
};

export default DottedRings;