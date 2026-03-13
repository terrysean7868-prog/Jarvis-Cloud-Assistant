import React, { useCallback, useEffect, useMemo, useState } from "react";
import { createAutonomyGoal, getTasks } from "../utils/api";
import "./autonomyPanel.css";

function statusChipClass(status) {
  const s = String(status || "").toLowerCase();
  if (["completed", "success"].includes(s)) return "panel-chip";
  if (["failed", "error", "blocked"].includes(s)) return "panel-chip";
  return "panel-chip";
}

export default function TaskManager({ sessionId }) {
  const [tasks, setTasks] = useState([]);
  const [delegatedTasks, setDelegatedTasks] = useState([]);
  const [loading, setLoading] = useState(false);
  const [logText, setLogText] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getTasks(sessionId);
      setTasks(Array.isArray(res?.tasks) ? res.tasks : []);
      setDelegatedTasks(Array.isArray(res?.delegated_tasks) ? res.delegated_tasks : []);
    } catch (e) {
      setLogText(`Task fetch failed: ${e?.message || e}`);
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, [refresh]);

  const activeCount = useMemo(
    () => tasks.filter((t) => ["pending", "in_progress", "running"].includes(String(t?.status || ""))).length,
    [tasks]
  );

  const delegatedActive = useMemo(
    () => delegatedTasks.filter((t) => ["delegated", "queued_for_agent", "awaiting_agent", "executing", "pending_permission"].includes(String(t?.status || ""))).length,
    [delegatedTasks]
  );

  const retryTask = async (task) => {
    const title = String(task?.description || task?.title || "retry task");
    try {
      await createAutonomyGoal(`Retry task: ${title}`, sessionId, 5);
      setLogText(`Retry requested for: ${title}`);
      await refresh();
    } catch (e) {
      setLogText(`Retry failed: ${e?.message || e}`);
    }
  };

  return (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Task Queue</h3>
        <div className="panel-row">
          <span className="panel-chip">Total: {tasks.length}</span>
          <span className="panel-chip">Running: {activeCount}</span>
          <span className="panel-chip">Delegated Active: {delegatedActive}</span>
          <button className="panel-btn" onClick={refresh} disabled={loading}>{loading ? "Refreshing" : "Refresh"}</button>
        </div>
        <div className="panel-list" style={{ marginTop: 10 }}>
          {tasks.map((task, idx) => (
            <div className="panel-item" key={String(task?.id || idx)}>
              <h4>{String(task?.description || task?.title || "Task")}</h4>
              <p>Status: <span className={statusChipClass(task?.status)}>{String(task?.status || "unknown")}</span></p>
              <p>Updated: {String(task?.updated_at || task?.created_at || "-")}</p>
              <div className="panel-row">
                <button className="panel-btn warn" onClick={() => retryTask(task)}>Retry</button>
              </div>
              <div className="log-box" style={{ marginTop: 8 }}>
                {JSON.stringify(task?.result || task?.steps || {}, null, 2)}
              </div>
            </div>
          ))}
          {!tasks.length && <div className="panel-item"><p>No tasks found.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Delegated Queue</h3>
        <div className="panel-list" style={{ marginTop: 10 }}>
          {delegatedTasks.map((task, idx) => (
            <div className="panel-item" key={String(task?.task_id || idx)}>
              <h4>{String(task?.feature || "delegated_task")}</h4>
              <p>Status: <span className={statusChipClass(task?.status)}>{String(task?.status || "unknown")}</span></p>
              <p>Device: {String(task?.device_id || "unassigned")}</p>
              <p>Updated: {String(task?.updated_at_iso || task?.updated_at || "-")}</p>
              <div className="log-box" style={{ marginTop: 8 }}>
                {JSON.stringify({
                  source_text: task?.source_text,
                  reason: task?.reason,
                  attempts: task?.attempts,
                  last_job_id: task?.last_job_id,
                }, null, 2)}
              </div>
            </div>
          ))}
          {!delegatedTasks.length && <div className="panel-item"><p>No delegated tasks.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Execution Logs</h3>
        <div className="log-box">{logText || "Select retry or refresh to view actions."}</div>
      </div>
    </div>
  );
}
