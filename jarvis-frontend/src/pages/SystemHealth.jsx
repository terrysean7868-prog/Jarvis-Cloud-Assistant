import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Bar } from "react-chartjs-2";
import { Chart as ChartJS, BarElement, CategoryScale, LinearScale, Tooltip, Legend } from "chart.js";
import { getAutonomyStatus, getTasks } from "../utils/api";
import "./autonomyPanel.css";

ChartJS.register(BarElement, CategoryScale, LinearScale, Tooltip, Legend);

export default function SystemHealth({ sessionId, logs = [] }) {
  const [status, setStatus] = useState(null);
  const [tasks, setTasks] = useState([]);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [st, tk] = await Promise.all([getAutonomyStatus(sessionId), getTasks(sessionId)]);
      setStatus(st || null);
      setTasks(Array.isArray(tk?.tasks) ? tk.tasks : []);
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

  const chartData = useMemo(() => {
    const running = tasks.filter((t) => ["running", "in_progress", "pending"].includes(String(t?.status || ""))).length;
    const failed = tasks.filter((t) => ["failed", "error", "blocked"].includes(String(t?.status || ""))).length;
    const completed = tasks.filter((t) => ["completed", "success"].includes(String(t?.status || ""))).length;
    const tools = Number(status?.health?.tools || 0);
    const agents = Number(status?.health?.agents || 0);
    const queueSize = running;

    return {
      labels: ["Tasks Running", "Tasks Failed", "Tasks Completed", "Agents Active", "Tool Usage", "Queue Size"],
      datasets: [{
        label: "System Metrics",
        data: [running, failed, completed, agents, tools, queueSize],
        backgroundColor: ["#30c6ff", "#ff6f6f", "#65e3ab", "#89a0ff", "#f0bf68", "#b48dff"],
        borderColor: "#13253d",
        borderWidth: 1,
      }],
    };
  }, [status, tasks]);

  return (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">System Metrics</h3>
        <Bar data={chartData} options={{ responsive: true, plugins: { legend: { display: false } } }} />
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Agent Activity & Errors</h3>
        <div className="log-box">
          {JSON.stringify(status?.health || {}, null, 2)}
          {"\n\nRecent errors:\n"}
          {logs.filter((l) => String(l?.type || "") === "error").slice(0, 12).map((l) => `[${l?.time}] ${l?.message}`).join("\n") || "No recent error logs."}
        </div>
        {!!error && <div className="log-box" style={{ marginTop: 8 }}>{error}</div>}
      </div>
    </div>
  );
}
