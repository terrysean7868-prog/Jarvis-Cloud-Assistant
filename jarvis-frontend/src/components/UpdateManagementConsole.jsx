import React, { useCallback, useEffect, useMemo, useState } from "react";

import {
  getAdminUpdateHistory,
  getAdminProgressiveUpdateReport,
  runAdminUpdate,
  rollbackAdminUpdate,
  sendMessage,
  deleteTaskByTitle,
} from "../utils/api";

export default function UpdateManagementConsole({ sessionId, isOpen, onClose, onTasksChanged }) {
  const [filePath, setFilePath] = useState("src/core/llm_adapter.py");
  const [description, setDescription] = useState("Improve error handling for provider fallback path");
  const [autoInstallDeps, setAutoInstallDeps] = useState(false);
  const [history, setHistory] = useState([]);
  const [backupPath, setBackupPath] = useState("");
  const [moduleTitle, setModuleTitle] = useState("currency converter");
  const [moduleInstruction, setModuleInstruction] = useState("Use exchangerate.host, add caching, retries, and tests.");
  const [deleteTaskTitle, setDeleteTaskTitle] = useState("");
  const [progressiveReport, setProgressiveReport] = useState(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  const disabled = !sessionId || busy;

  const historyPreview = useMemo(() => {
    return Array.isArray(history) ? history.slice(0, 40) : [];
  }, [history]);

  const refreshHistory = async () => {
    if (!sessionId) return;
    setBusy(true);
    setStatus("Loading update history...");
    try {
      const res = await getAdminUpdateHistory(sessionId, 100);
      if (res?.status === "success") {
        setHistory(Array.isArray(res.history) ? res.history : []);
        setStatus(`Loaded ${Array.isArray(res.history) ? res.history.length : 0} events.`);
      } else {
        setStatus(res?.message || "Failed to load update history.");
      }
    } catch (e) {
      setStatus(e?.message || "Failed to load update history.");
    } finally {
      setBusy(false);
    }
  };

  const refreshProgressiveReport = useCallback(async () => {
    if (!sessionId) return;
    try {
      const res = await getAdminProgressiveUpdateReport(sessionId, 20000);
      if (res?.status === "success") {
        setProgressiveReport(res?.report || null);
      } else {
        setProgressiveReport(null);
      }
    } catch {
      setProgressiveReport(null);
    }
  }, [sessionId]);

  const runUpdate = async (dryRun = false) => {
    if (!sessionId) return;
    if (!filePath.trim() || !description.trim()) {
      setStatus("File path and description are required.");
      return;
    }

    setBusy(true);
    setStatus(dryRun ? "Running validation dry-run..." : "Running update...");
    try {
      const res = await runAdminUpdate({
        sessionId,
        filePath: filePath.trim(),
        description: description.trim(),
        autoInstallDeps,
        dryRun,
      });
      if (res?.status === "success") {
        setStatus(`${dryRun ? "Dry-run" : "Update"} success. ${res.message || ""}`.trim());
        if (res?.backup_path) setBackupPath(String(res.backup_path));
      } else {
        setStatus(res?.message || `${dryRun ? "Dry-run" : "Update"} failed.`);
      }
      await refreshHistory();
    } catch (e) {
      setStatus(e?.message || `${dryRun ? "Dry-run" : "Update"} failed.`);
    } finally {
      setBusy(false);
    }
  };

  const rollback = async () => {
    if (!sessionId) return;
    if (!filePath.trim()) {
      setStatus("File path is required for rollback.");
      return;
    }

    setBusy(true);
    setStatus("Rolling back update...");
    try {
      const res = await rollbackAdminUpdate({
        sessionId,
        filePath: filePath.trim(),
        backupPath: backupPath.trim() || null,
      });
      if (res?.status === "success") {
        setStatus("Rollback applied successfully.");
      } else {
        setStatus(res?.message || "Rollback failed.");
      }
      await refreshHistory();
    } catch (e) {
      setStatus(e?.message || "Rollback failed.");
    } finally {
      setBusy(false);
    }
  };

  const continueModuleCycle = async () => {
    if (!sessionId) return;
    if (!moduleInstruction.trim()) {
      setStatus("Module instruction is required.");
      return;
    }

    const title = moduleTitle.trim();
    const text = title
      ? `Continue module task ${title}: ${moduleInstruction.trim()}`
      : `Continue module task: ${moduleInstruction.trim()}`;

    setBusy(true);
    setStatus("Sending module-cycle instruction...");
    try {
      const res = await sendMessage(text, "chat", sessionId, 120000);
      const reply = (res?.text || "").toString().trim();
      setStatus(reply || "Module-cycle instruction sent.");
      try {
        if (typeof onTasksChanged === "function") {
          await onTasksChanged();
        }
      } catch {}
    } catch (e) {
      setStatus(e?.message || "Failed to continue module cycle.");
    } finally {
      setBusy(false);
    }
  };

  const deleteByTitle = async () => {
    if (!sessionId) return;
    const title = deleteTaskTitle.trim();
    if (!title) {
      setStatus("Task title is required for delete.");
      return;
    }

    setBusy(true);
    setStatus("Deleting task by title...");
    try {
      const res = await deleteTaskByTitle(sessionId, title);
      setStatus((res?.message || "Task delete processed.").toString());
      try {
        if (typeof onTasksChanged === "function") {
          await onTasksChanged();
        }
      } catch {}
      await refreshHistory();
    } catch (e) {
      setStatus(e?.message || "Failed to delete task by title.");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (!isOpen || !sessionId) return;
    refreshProgressiveReport();
  }, [isOpen, sessionId, refreshProgressiveReport]);

  if (!isOpen) return null;

  return (
    <div style={{ position: "fixed", top: 70, right: 20, zIndex: 40, width: 540, maxWidth: "95vw", background: "rgba(10,10,12,0.93)", border: "1px solid var(--jarvis-accent)", borderRadius: 12, boxShadow: "0 0 20px var(--jarvis-accent-glow)", padding: 14 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <strong style={{ color: "var(--jarvis-accent)", fontSize: 14 }}>Update Management Console (Admin)</strong>
        <button onClick={onClose} style={{ background: "transparent", border: "none", color: "#ff8080", cursor: "pointer" }}>Close</button>
      </div>

      <div style={{ display: "grid", gap: 8 }}>
        <input value={filePath} onChange={(e) => setFilePath(e.target.value)} placeholder="Target file path" style={{ width: "100%", background: "#111", color: "#e8f7ff", border: "1px solid #2f3a40", borderRadius: 8, padding: 8 }} />
        <textarea value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Update description" rows={3} style={{ width: "100%", resize: "vertical", background: "#111", color: "#e8f7ff", border: "1px solid #2f3a40", borderRadius: 8, padding: 8 }} />
        <input value={backupPath} onChange={(e) => setBackupPath(e.target.value)} placeholder="Optional backup path for rollback" style={{ width: "100%", background: "#111", color: "#e8f7ff", border: "1px solid #2f3a40", borderRadius: 8, padding: 8 }} />

        <input value={moduleTitle} onChange={(e) => setModuleTitle(e.target.value)} placeholder="Module cycle title (e.g., currency converter)" style={{ width: "100%", background: "#111", color: "#e8f7ff", border: "1px solid #2f3a40", borderRadius: 8, padding: 8 }} />
        <textarea value={moduleInstruction} onChange={(e) => setModuleInstruction(e.target.value)} placeholder="Admin instruction to continue module cycle" rows={2} style={{ width: "100%", resize: "vertical", background: "#111", color: "#e8f7ff", border: "1px solid #2f3a40", borderRadius: 8, padding: 8 }} />
        <input value={deleteTaskTitle} onChange={(e) => setDeleteTaskTitle(e.target.value)} placeholder="Delete task by title" style={{ width: "100%", background: "#111", color: "#e8f7ff", border: "1px solid #2f3a40", borderRadius: 8, padding: 8 }} />

        <label style={{ color: "#9ecfe0", fontSize: 12, display: "flex", gap: 8, alignItems: "center" }}>
          <input type="checkbox" checked={autoInstallDeps} onChange={(e) => setAutoInstallDeps(e.target.checked)} />
          Auto-install missing Python dependencies
        </label>

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button disabled={disabled} onClick={() => runUpdate(true)} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid #2f3a40", background: "#1b1f24", color: "#e8f7ff", cursor: disabled ? "not-allowed" : "pointer" }}>Validate (Dry-run)</button>
          <button disabled={disabled} onClick={() => runUpdate(false)} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid var(--jarvis-accent)", background: "rgba(0,234,255,0.12)", color: "var(--jarvis-accent)", cursor: disabled ? "not-allowed" : "pointer" }}>Run Update</button>
          <button disabled={disabled} onClick={rollback} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid #dca86a", background: "rgba(220,168,106,0.12)", color: "#ffd4a0", cursor: disabled ? "not-allowed" : "pointer" }}>Rollback</button>
          <button disabled={disabled} onClick={continueModuleCycle} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid #67c6ff", background: "rgba(103,198,255,0.12)", color: "#c9ecff", cursor: disabled ? "not-allowed" : "pointer" }}>Continue Module Cycle</button>
          <button disabled={disabled} onClick={deleteByTitle} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid #ff8f8f", background: "rgba(255,143,143,0.12)", color: "#ffd2d2", cursor: disabled ? "not-allowed" : "pointer" }}>Delete Task by Title</button>
          <button disabled={disabled} onClick={refreshHistory} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid #2f3a40", background: "#1b1f24", color: "#e8f7ff", cursor: disabled ? "not-allowed" : "pointer" }}>Refresh History</button>
          <button disabled={disabled} onClick={refreshProgressiveReport} style={{ padding: "8px 10px", borderRadius: 8, border: "1px solid #6fdca5", background: "rgba(111,220,165,0.12)", color: "#cdf7e1", cursor: disabled ? "not-allowed" : "pointer" }}>Refresh Progressive Report</button>
        </div>

        <div style={{ fontSize: 12, color: status.includes("failed") || status.includes("error") ? "#ff9f9f" : "#9ecfe0", minHeight: 16 }}>{status}</div>
      </div>

      <div style={{ marginTop: 10, borderTop: "1px solid #2a3138", paddingTop: 8 }}>
        <div style={{ color: "#9ecfe0", fontSize: 12, marginBottom: 6 }}><strong>Daily Progressive LLM Report</strong></div>
        {!progressiveReport ? (
          <div style={{ color: "#8da3ad", fontSize: 12 }}>No report loaded yet. Click "Refresh Progressive Report".</div>
        ) : (
          <div style={{ fontSize: 12, color: "#d6ebf3", border: "1px solid #23303a", borderRadius: 8, padding: 8 }}>
            <div><strong>Status:</strong> {progressiveReport.status || "unknown"}</div>
            <div><strong>Started:</strong> {progressiveReport.started_at || "-"}</div>
            <div><strong>Completed:</strong> {progressiveReport.completed_at || "-"}</div>
            <div><strong>Actor:</strong> {progressiveReport.actor || "-"}</div>
            <div><strong>Dry-run:</strong> {String(!!progressiveReport.dry_run)}</div>
            <div style={{ marginTop: 6 }}><strong>Changed Files:</strong></div>
            {Array.isArray(progressiveReport.changed_files) && progressiveReport.changed_files.length > 0 ? (
              <ul style={{ margin: "4px 0 0 16px", padding: 0 }}>
                {progressiveReport.changed_files.map((f, i) => (
                  <li key={`${f}-${i}`} style={{ color: "#9ab8c4" }}>{String(f)}</li>
                ))}
              </ul>
            ) : (
              <div style={{ color: "#9ab8c4" }}>No changed files recorded.</div>
            )}
          </div>
        )}
      </div>

      <div style={{ marginTop: 10, maxHeight: 260, overflowY: "auto", borderTop: "1px solid #2a3138", paddingTop: 8 }}>
        {historyPreview.length === 0 ? (
          <div style={{ color: "#8da3ad", fontSize: 12 }}>No update events yet.</div>
        ) : (
          historyPreview.map((row, idx) => (
            <div key={`${row.ts || ""}-${idx}`} style={{ marginBottom: 7, fontSize: 12, color: "#d6ebf3", borderBottom: "1px dashed #23303a", paddingBottom: 6 }}>
              <div><strong>{row.action || "update"}</strong> • {row.status || "unknown"} • {(row.actor || "unknown")}</div>
              <div style={{ color: "#9ab8c4" }}>{row.target_file || "-"}</div>
              <div style={{ color: "#7ea0ad" }}>{row.ts || ""}</div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
