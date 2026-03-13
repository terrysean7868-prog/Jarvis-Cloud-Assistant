// src/components/hud/JarvisHUD.jsx
import React, { useEffect, useState } from "react";
import MicWaveform from "./MicWaveform";
import HUDLogPanel from "./HUDLogPanel";
import "../../styles/HUD.css";

const JarvisHUD = ({ isListening, logs, addLog }) => {
  const [visible, setVisible] = useState(true);

  useEffect(() => {
    const timer = setTimeout(() => setVisible(true), 300);
    return () => clearTimeout(timer);
  }, [logs]);

  return (
    <div className={`jarvis-hud ${visible ? "visible" : ""}`}>
      <div className="hud-top">
        <h2 className="hud-title">J.A.R.V.I.S ONLINE</h2>
        <MicWaveform active={isListening} />
      </div>

      <div className="hud-middle">
        <HUDLogPanel logs={logs} />
      </div>

      <div className="hud-bottom">
        <span className="hud-status">System active • {new Date().toLocaleTimeString()}</span>
      </div>
    </div>
  );
};

export default JarvisHUD;
