import React, { useCallback, useEffect, useState } from "react";
import { getAgents } from "../utils/api";
import "./autonomyPanel.css";

export default function AgentMonitor({ sessionId }) {
  const [agents, setAgents] = useState([]);
  const [deviceAgents, setDeviceAgents] = useState([]);
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const res = await getAgents(sessionId);
      setAgents(Array.isArray(res?.agents) ? res.agents : []);
      setDeviceAgents(Array.isArray(res?.connected_device_agents) ? res.connected_device_agents : []);
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

  return (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Autonomy Agents</h3>
        <div className="panel-row">
          <span className="panel-chip">Count: {agents.length}</span>
          <button className="panel-btn" onClick={refresh}>Refresh</button>
        </div>
        <div className="panel-list" style={{ marginTop: 10 }}>
          {agents.map((a, idx) => (
            <div className="panel-item" key={String(a?.name || idx)}>
              <h4>{String(a?.name || "Agent")}</h4>
              <p>Class: {String(a?.class || "-")}</p>
              <p>Capabilities: routing, planning, execute, evaluate</p>
              <p>Activity: active in autonomy loop dispatch</p>
            </div>
          ))}
          {!agents.length && <div className="panel-item"><p>No agents discovered.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Connected Device Agents</h3>
        <div className="panel-list">
          {deviceAgents.map((d, idx) => (
            <div className="panel-item" key={String(d?.device_id || idx)}>
              <h4>{String(d?.device_id || "device")}</h4>
              <p>Status: {String(d?.status || "connected")}</p>
              <p>Capabilities: {JSON.stringify(d?.capabilities || {})}</p>
            </div>
          ))}
          {!deviceAgents.length && <div className="panel-item"><p>No connected device agents.</p></div>}
        </div>
        {!!err && <div className="log-box" style={{ marginTop: 10 }}>{err}</div>}
      </div>
    </div>
  );
}
