// src/components/hud/HUDLogPanel.jsx
import React from "react";

const HUDLogPanel = ({ logs }) => {
  return (
    <div className="hud-log">
      {logs.slice(-6).map((log, i) => (
        <div key={i} className="hud-log-item">
          <span className="timestamp">[{log.time}]</span>
          <span className={`log-type ${log.type}`}>{log.type.toUpperCase()}:</span>
          <span className="log-msg">{log.message}</span>
        </div>
      ))}
    </div>
  );
};

export default HUDLogPanel;
