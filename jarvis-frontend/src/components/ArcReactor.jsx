// src/components/ArcReactor.jsx
import React, { useEffect, useRef } from "react";
import "../styles/arcReactor.css";

export default function ArcReactor({
  active,
  wakePulse,
  emotion,
  volume = 0,
  transformState = "normal",
}) {
  const reactorRef = useRef();

  useEffect(() => {
    if (!reactorRef.current) return;
    const core = reactorRef.current.querySelector(".core");
    const rings = reactorRef.current.querySelectorAll(".ring");

    // Emotion-based color system
    let glow = "#00ffff";
    if (emotion === "analyzing") glow = "#ffe066";
    else if (emotion === "critical") glow = "#ff4d4d";
    else if (emotion === "action") glow = "#00ff99";

    // Apply volume-based dynamic curvature
    rings.forEach((ring, i) => {
      const direction = i % 2 === 0 ? 1 : -1;
      const twistAmount = volume * 40 * direction;
      const depth = Math.sin(Date.now() / (2000 + i * 500)) * 25;

      ring.style.transform = `
        rotateY(${twistAmount}deg)
        rotateX(${depth * direction}deg)
        translateZ(${depth / 2}px)
        scale(${1 + volume * 0.2})
      `;
    });

    core.style.background = `radial-gradient(circle, ${glow} 0%, #001010 85%)`;
    reactorRef.current.style.filter = `drop-shadow(0 0 ${20 + volume * 40}px ${glow})`;

    // Transformation modes
    const baseTransform = "translate(-50%, -50%)";
    if (transformState === "twist")
      reactorRef.current.style.transform = `${baseTransform} rotateY(30deg) rotateX(15deg)`;
    else if (transformState === "expand")
      reactorRef.current.style.transform = `${baseTransform} scale(1.4)`;
    else if (transformState === "contract")
      reactorRef.current.style.transform = `${baseTransform} scale(0.75)`;
    else reactorRef.current.style.transform = `${baseTransform} scale(1)`;
  }, [volume, emotion, transformState]);

  return (
    <div
      className={`arc-reactor ${active ? "active" : ""} ${
        wakePulse ? "wake" : ""
      }`}
      data-emotion={emotion}
      ref={reactorRef}
    >
      <div className="ring ring1"></div>
      <div className="ring ring2"></div>
      <div className="ring ring3"></div>
      <div className="ring ring4"></div>
      <div className="core"></div>
    </div>
  );
}
