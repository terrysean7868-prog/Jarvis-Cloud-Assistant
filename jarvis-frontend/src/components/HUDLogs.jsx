import React from "react";
import "../styles/hudLogs.css";

export default function HUDLogs({ logs }) {
  return (
    <div className="hud-logs">
      {logs.map((log, i) => (
        <div key={i} className={`log-item ${log.type}`}>
          <span className="time">[{log.time}]</span> {log.message}
        </div>
      ))}
    </div>
  );
}
