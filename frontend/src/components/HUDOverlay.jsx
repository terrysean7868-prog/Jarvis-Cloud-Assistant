// src/components/HUDOverlay.jsx
import React, { useState, useEffect } from "react";
import "./../styles/hudOverlay.css";

export default function HUDOverlay({ active }) {
  const [cpuLoad, setCpuLoad] = useState(0);
  const [networkPing, setNetworkPing] = useState(0);
  const [aiMode, setAiMode] = useState("IDLE");

  // Fake system metrics for realism
  useEffect(() => {
    const interval = setInterval(() => {
      setCpuLoad(Math.floor(20 + Math.random() * 60));
      setNetworkPing(Math.floor(40 + Math.random() * 40));
      setAiMode(active ? "ACTIVE" : "IDLE");
    }, 1000);
    return () => clearInterval(interval);
  }, [active]);

  return (
    <div className="hud-overlay">
      {/* Left panel */}
      <div className="hud-panel left">
        <div className="hud-label">CPU LOAD</div>
        <div className="hud-bar">
          <div
            className="hud-bar-fill"
            style={{ width: `${cpuLoad}%` }}
          ></div>
        </div>
        <div className="hud-metric">{cpuLoad}%</div>

        <div className="hud-label">NETWORK</div>
        <div className="hud-bar">
          <div
            className="hud-bar-fill net"
            style={{ width: `${networkPing / 2}%` }}
          ></div>
        </div>
        <div className="hud-metric">{networkPing} ms</div>
      </div>

      {/* Right panel */}
      <div className="hud-panel right">
        <div className="hud-mode">
          MODE: <span className="hud-mode-value">{aiMode}</span>
        </div>
        <div className="ring-group">
          <div className="ring ring1"></div>
          <div className="ring ring2"></div>
          <div className="ring ring3"></div>
        </div>
      </div>
    </div>
  );
}
