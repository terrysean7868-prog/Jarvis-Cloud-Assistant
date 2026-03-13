import React, { useCallback, useEffect, useState } from "react";
import { getAgents, getDelegatedTasks } from "../utils/api";
import "./autonomyPanel.css";

export default function AgentMonitor({ sessionId }) {
  const [agents, setAgents] = useState([]);
  const [deviceAgents, setDeviceAgents] = useState([]);
  const [delegatedSummary, setDelegatedSummary] = useState({});
  const [err, setErr] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [agentRes, delegatedRes] = await Promise.all([
        getAgents(sessionId),
        getDelegatedTasks(sessionId, { limit: 120 }),
      ]);
      setAgents(Array.isArray(agentRes?.agents) ? agentRes.agents : []);
      setDeviceAgents(Array.isArray(agentRes?.connected_device_agents) ? agentRes.connected_device_agents : []);
      setDelegatedSummary((delegatedRes && typeof delegatedRes.summary === "object") ? delegatedRes.summary : {});
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
        <div className="panel-row">
          <span className="panel-chip">Delegated: {Number(delegatedSummary?.delegated || 0)}</span>
          <span className="panel-chip">Queued: {Number(delegatedSummary?.queued_for_agent || 0)}</span>
          <span className="panel-chip">Awaiting Agent: {Number(delegatedSummary?.awaiting_agent || 0)}</span>
          <span className="panel-chip">Completed: {Number(delegatedSummary?.completed || 0)}</span>
          <span className="panel-chip">Failed: {Number(delegatedSummary?.failed || 0)}</span>
        </div>
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
