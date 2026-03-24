import React, { useCallback, useEffect, useState } from "react";
import {
  decideSelfImprovementProposal,
  getLearningMetrics,
  getSelfImprovementProposals,
  getSelfImprovementSuggestions,
} from "../utils/api";
import "./autonomyPanel.css";

export default function SelfImprovementPanel({ sessionId }) {
  const [proposals, setProposals] = useState([]);
  const [suggestions, setSuggestions] = useState([]);
  const [learningMetrics, setLearningMetrics] = useState(null);
  const [diffText, setDiffText] = useState("");
  const [status, setStatus] = useState("");
  const [lastSyncAt, setLastSyncAt] = useState("");

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setProposals([]);
      setSuggestions([]);
      setLearningMetrics(null);
      setStatus("Login required to load self-improvement data.");
      return;
    }
    try {
      const [proposalRes, suggestionRes, metricsRes] = await Promise.all([
        getSelfImprovementProposals(sessionId),
        getSelfImprovementSuggestions(sessionId, 120),
        getLearningMetrics(sessionId),
      ]);
      setProposals(Array.isArray(proposalRes?.proposals) ? proposalRes.proposals : []);
      setSuggestions(Array.isArray(suggestionRes?.suggestions) ? suggestionRes.suggestions : []);
      setLearningMetrics(metricsRes?.metrics && typeof metricsRes.metrics === "object" ? metricsRes.metrics : null);
      setLastSyncAt(new Date().toLocaleTimeString());
      setStatus("");
    } catch (e) {
      setStatus(`Failed: ${e?.message || e}`);
    }
  }, [sessionId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 12000);
    return () => clearInterval(id);
  }, [refresh]);

  const decide = async (proposalId, decision) => {
    try {
      const res = await decideSelfImprovementProposal(proposalId, decision, sessionId);
      setStatus(res?.message || `Proposal ${decision}`);
      await refresh();
    } catch (e) {
      setStatus(`Decision failed: ${e?.message || e}`);
    }
  };

  return (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Learning Metrics</h3>
        <div className="panel-item">
          <p>Latest success score: {String(learningMetrics?.latest_success_score ?? "0")}</p>
          <p>Top failure pattern: {String(learningMetrics?.top_failure_pattern || "-")}</p>
          <p>Open suggestion count: {String(learningMetrics?.open_suggestion_count ?? 0)}</p>
          <p>
            Top failing model: {String(learningMetrics?.top_failing_model?.model_id || "-")} |
            Failure rate: {String(learningMetrics?.top_failing_model?.failure_rate ?? "0")}
          </p>
          <p>Last learning cycle: {String(learningMetrics?.last_learning_cycle_at || "-")}</p>
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Self Improvement Proposals</h3>
        <div className="panel-row" style={{ marginBottom: 8, justifyContent: "space-between" }}>
          <div style={{ opacity: 0.85 }}>Pending proposals: {proposals.length}</div>
          <button className="panel-btn" onClick={refresh}>Refresh</button>
        </div>
        <div className="panel-list">
          {proposals.map((p, idx) => (
            <div className="panel-item" key={String(p?.proposal_id || idx)}>
              <h4>{String(p?.title || "Patch proposal")}</h4>
              <p>Status: {String(p?.status || "pending")}</p>
              <p>Risk score: {String(p?.risk_score ?? "unknown")}</p>
              <p>Generated tools: {JSON.stringify(p?.generated_tools || [])}</p>
              <p>Created: {String(p?.created_at || "-")}</p>
              <div className="panel-row">
                <button className="panel-btn" onClick={() => decide(p?.proposal_id, "approve")}>Approve</button>
                <button className="panel-btn danger" onClick={() => decide(p?.proposal_id, "reject")}>Reject</button>
                <button className="panel-btn warn" onClick={() => setDiffText(String(p?.diff || p?.patch || "No diff available"))}>View diff</button>
              </div>
            </div>
          ))}
          {!proposals.length && <div className="panel-item"><p>No pending proposals.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Learning Suggestions</h3>
        <div className="panel-list">
          {suggestions.map((s, idx) => (
            <div className="panel-item" key={String(s?.suggestion_id || idx)}>
              <h4>{String(s?.issue || "Improvement opportunity")}</h4>
              <p>Root cause: {String(s?.root_cause || "-")}</p>
              <p>Suggested fix: {String(s?.suggested_fix || "-")}</p>
              <p>Affected module: {String(s?.affected_module || "-")}</p>
              <p>Priority: {String(s?.priority ?? "5")} | Status: {String(s?.status || "open")}</p>
              <p>Created: {String(s?.created_at || "-")}</p>
            </div>
          ))}
          {!suggestions.length && <div className="panel-item"><p>No learning suggestions yet.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Diff Viewer</h3>
        <div className="log-box">{diffText || "Select View diff from a proposal."}</div>
        <div className="log-box" style={{ marginTop: 8 }}>
          {status || `Awaiting approval actions.${lastSyncAt ? ` Last sync: ${lastSyncAt}` : ""}`}
        </div>
      </div>
    </div>
  );
}
