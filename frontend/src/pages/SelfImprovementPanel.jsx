import React, { useCallback, useEffect, useState } from "react";
import { decideSelfImprovementProposal, getSelfImprovementProposals } from "../utils/api";
import "./autonomyPanel.css";

export default function SelfImprovementPanel({ sessionId }) {
  const [proposals, setProposals] = useState([]);
  const [diffText, setDiffText] = useState("");
  const [status, setStatus] = useState("");
  const [lastSyncAt, setLastSyncAt] = useState("");

  const refresh = useCallback(async () => {
    try {
      const res = await getSelfImprovementProposals(sessionId);
      setProposals(Array.isArray(res?.proposals) ? res.proposals : []);
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
        <h3 className="panel-title">Diff Viewer</h3>
        <div className="log-box">{diffText || "Select View diff from a proposal."}</div>
        <div className="log-box" style={{ marginTop: 8 }}>
          {status || `Awaiting approval actions.${lastSyncAt ? ` Last sync: ${lastSyncAt}` : ""}`}
        </div>
      </div>
    </div>
  );
}
