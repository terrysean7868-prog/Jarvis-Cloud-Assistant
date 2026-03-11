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
  const renderStructured = (log) => {
    const messageType = String(log?.messageType || "text");
    if (messageType === "plan") {
      const steps = Array.isArray(log?.payload?.steps) ? log.payload.steps : [];
      return (
        <div>
          <div>{linkify(log.message)}</div>
          {!!steps.length && (
            <ol style={{ margin: "6px 0 0 16px" }}>
              {steps.map((s, i) => <li key={i}>{String(s)}</li>)}
            </ol>
          )}
        </div>
      );
    }

    if (messageType === "task_graph") {
      const nodes = Array.isArray(log?.payload?.nodes) ? log.payload.nodes : [];
      return (
        <div>
          <div>{linkify(log.message)}</div>
          <pre style={{ marginTop: 6, whiteSpace: "pre-wrap" }}>
            {nodes.map((n) => `- ${n?.title || n?.task_id || "task"} [${n?.status || "pending"}]`).join("\n") || "No nodes"}
          </pre>
        </div>
      );
    }

    if (messageType === "code_block") {
      return (
        <div>
          <div>{linkify(log.message)}</div>
          <pre style={{ marginTop: 6, whiteSpace: "pre-wrap", overflowX: "auto" }}>{String(log?.payload?.code || "")}</pre>
        </div>
      );
    }

    if (messageType === "research_report") {
      const sources = Array.isArray(log?.payload?.sources) ? log.payload.sources : [];
      return (
        <div>
          <div>{linkify(log.message)}</div>
          <div style={{ marginTop: 6 }}>
            {sources.slice(0, 6).map((s, i) => (
              <div key={i}>{linkify(String(s?.title || s?.url || "source"))}</div>
            ))}
          </div>
        </div>
      );
    }

    return linkify(log?.message);
  };

  return (
    <div className="hud-logs">
      {logs.map((log, i) => (
        <div key={i} className={`log-item ${log.type}`}>
          <span className="time">[{log.time}]</span> {renderStructured(log)}
        </div>
      ))}
    </div>
  );
}
