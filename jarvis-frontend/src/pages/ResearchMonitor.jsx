import React, { useCallback, useEffect, useMemo, useState } from "react";
import { getAutonomyGoals } from "../utils/api";
import "./autonomyPanel.css";

export default function ResearchMonitor({ sessionId }) {
  const [goals, setGoals] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const res = await getAutonomyGoals({ sessionId, statuses: "pending,running,awaiting_confirmation,completed,failed", limit: 50 });
      const rows = Array.isArray(res?.goals) ? res.goals : [];
      setGoals(rows);
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  }, [sessionId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 9000);
    return () => clearInterval(id);
  }, [refresh]);

  const researchGoals = useMemo(() => {
    return goals.filter((g) => {
      const text = `${g?.goal || ""}`.toLowerCase();
      return text.includes("research") || text.includes("analysis") || text.includes("compare");
    });
  }, [goals]);

  return (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Research Pipelines</h3>
        <div className="panel-row">
          <span className="panel-chip">Pipelines: {researchGoals.length}</span>
          <button className="panel-btn" onClick={refresh}>Refresh</button>
        </div>
        <div className="panel-list" style={{ marginTop: 10 }}>
          {researchGoals.map((g, idx) => {
            const reports = Array.isArray(g?.reports) ? g.reports : [];
            const sources = reports
              .filter((r) => Array.isArray(r?.result?.research?.results))
              .flatMap((r) => r.result.research.results)
              .slice(0, 8);
            const summary = reports
              .map((r) => r?.result?.research?.summary || r?.report?.summary)
              .filter(Boolean)
              .join("\n");

            return (
              <div className="panel-item" key={String(g?._id || idx)}>
                <h4>{String(g?.goal || "Research goal")}</h4>
                <p>Status: {String(g?.status || "unknown")}</p>
                <p>Progress: {reports.length} report entries</p>
                <div className="log-box" style={{ marginTop: 8 }}>
                  Sources:\n{sources.map((s) => `- ${s?.title || s?.url || "source"}`).join("\n") || "No sources yet."}
                </div>
                <div className="log-box" style={{ marginTop: 8 }}>
                  Final synthesized report:\n{summary || "No synthesized report yet."}
                </div>
              </div>
            );
          })}
          {!researchGoals.length && <div className="panel-item"><p>No active research pipelines.</p></div>}
        </div>
      </div>
      {!!error && <div className="panel-card"><h3 className="panel-title">Errors</h3><div className="log-box">{error}</div></div>}
    </div>
  );
}
