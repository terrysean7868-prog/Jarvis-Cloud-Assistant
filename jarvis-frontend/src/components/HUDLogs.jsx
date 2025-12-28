import React from "react";
import "../styles/hudLogs.css";

function linkify(text) {
  const s = String(text || "");
  const re = /(https?:\/\/[^\s]+|www\.[^\s]+)/gi;
  const parts = [];
  let last = 0;
  let m;
  while ((m = re.exec(s)) !== null) {
    const start = m.index;
    const rawUrl = m[0];
    if (start > last) parts.push(s.slice(last, start));
    const href = rawUrl.startsWith("http") ? rawUrl : `https://${rawUrl}`;
    parts.push(
      <a key={`${start}-${href}`} className="hud-link" href={href} target="_blank" rel="noopener noreferrer">
        {rawUrl}
      </a>
    );
    last = start + rawUrl.length;
  }
  if (last < s.length) parts.push(s.slice(last));
  return parts.length ? parts : s;
}

export default function HUDLogs({ logs }) {
  return (
    <div className="hud-logs">
      {logs.map((log, i) => (
        <div key={i} className={`log-item ${log.type}`}>
          <span className="time">[{log.time}]</span> {linkify(log.message)}
        </div>
      ))}
    </div>
  );
}
