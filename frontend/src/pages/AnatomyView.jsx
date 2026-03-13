import React, { useCallback, useEffect, useState } from "react";
import { getAnatomyState } from "../utils/api";
import "./autonomyPanel.css";

export default function AnatomyView({ sessionId }) {
  const [state, setState] = useState(null);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const res = await getAnatomyState(sessionId);
      setState(res || null);
      setErr("");
    } catch (e) {
      setErr(e?.message || String(e));
    }
  }, [sessionId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 7000);
    return () => clearInterval(id);
  }, [refresh]);

  const goals = state?.task_graph?.goals || {};
  const tasks = state?.task_graph?.tasks || {};
  const runtime = state?.runtime || {};
  const knowledge = state?.knowledge_store || {};
  const services = state?.runtime_services || {};
  const devices = state?.device_connections || {};

  return (
    <div className="panel-grid anatomy-grid">
      <div className="panel-card">
        <h3 className="panel-title">System Anatomy</h3>
        <div className="log-box">
          {JSON.stringify({
            updated_at: state?.updated_at,
            control: runtime?.control || {},
            health: runtime?.health || {},
          }, null, 2)}
        </div>
      </div>

      <div className="panel-card">
        <h3 className="panel-title">Agents + Task Graph</h3>
        <div className="log-box">
{`Agent definitions: ${state?.agents?.count || 0}
Goals -> pending: ${goals.pending || 0}, running: ${goals.running || 0}, completed: ${goals.completed || 0}, failed: ${goals.failed || 0}, blocked: ${goals.blocked || 0}
Tasks -> pending: ${tasks.pending || 0}, in_progress: ${tasks.in_progress || 0}, running: ${tasks.running || 0}, completed: ${tasks.completed || 0}, failed: ${tasks.failed || 0}`}
        </div>
      </div>

      <div className="panel-card">
        <h3 className="panel-title">Knowledge + Services</h3>
        <div className="log-box">
{`Knowledge store: learning_examples=${knowledge.learning_examples || 0}, web_training_items=${knowledge.web_training_items || 0}
Tools registered: ${services.tool_count || 0}
Self-improvement: ${JSON.stringify(services.self_improvement || {})}`}
        </div>
      </div>

      <div className="panel-card">
        <h3 className="panel-title">Device Connections</h3>
        <div className="log-box">
          {JSON.stringify({
            connected_count: devices.connected_count || 0,
            devices: devices.devices || [],
          }, null, 2)}
        </div>
      </div>

      {!!err && (
        <div className="panel-card">
          <h3 className="panel-title">Errors</h3>
          <div className="log-box">{err}</div>
        </div>
      )}
    </div>
  );
}
