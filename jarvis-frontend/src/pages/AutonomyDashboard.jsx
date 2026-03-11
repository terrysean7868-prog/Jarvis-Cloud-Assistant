import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, { Background, Controls } from "reactflow";
import "reactflow/dist/style.css";
import { Line } from "react-chartjs-2";
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend } from "chart.js";
import { createAutonomyGoal, getAutonomyGoals, getAutonomyStatus } from "../utils/api";
import TaskManager from "./TaskManager";
import AgentMonitor from "./AgentMonitor";
import ResearchMonitor from "./ResearchMonitor";
import DeviceControl from "./DeviceControl";
import SystemHealth from "./SystemHealth";
import SelfImprovementPanel from "./SelfImprovementPanel";
import "./autonomyPanel.css";

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend);

const TABS = [
  "Autonomy",
  "Tasks",
  "Agents",
  "Research",
  "Devices",
  "Health",
  "Self-Improvement",
];

function normalizeGraph(goal) {
  const reports = Array.isArray(goal?.reports) ? goal.reports : [];
  const graphReport = reports.find((r) => r && r.graph && Array.isArray(r.graph.nodes));
  const graph = graphReport?.graph || { nodes: [] };
  const nodes = [];
  const edges = [];
  const rowGap = 90;

  (graph.nodes || []).forEach((n, idx) => {
    nodes.push({
      id: String(n.task_id || idx),
      position: { x: 80 + idx * 160, y: 40 + (idx % 2) * rowGap },
      data: { label: `${n.title || n.task_id} (${n.status || "pending"})` },
      style: {
        border: "1px solid rgba(255,255,255,0.2)",
        borderRadius: 8,
        padding: 6,
        color: "#dbf7ff",
        background: "rgba(8, 26, 41, 0.86)",
      },
    });

    (n.dependencies || []).forEach((dep) => {
      edges.push({ id: `${dep}-${n.task_id}`, source: String(dep), target: String(n.task_id), animated: false });
    });
  });

  return { nodes, edges };
}

export default function AutonomyDashboard({ sessionId, logs = [] }) {
  const [tab, setTab] = useState("Autonomy");
  const [goals, setGoals] = useState([]);
  const [status, setStatus] = useState(null);
  const [goalInput, setGoalInput] = useState("");
  const [selectedGoalId, setSelectedGoalId] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [g, s] = await Promise.all([
        getAutonomyGoals({ sessionId, statuses: "pending,running,awaiting_confirmation,failed,completed", limit: 80 }),
        getAutonomyStatus(sessionId),
      ]);
      const rows = Array.isArray(g?.goals) ? g.goals : [];
      setGoals(rows);
      setStatus(s || null);
      if (!selectedGoalId && rows.length) {
        setSelectedGoalId(String(rows[0]?._id || ""));
      }
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  }, [sessionId, selectedGoalId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 7000);
    return () => clearInterval(id);
  }, [refresh]);

  const selectedGoal = useMemo(
    () => goals.find((g) => String(g?._id || "") === String(selectedGoalId || "")) || goals[0] || null,
    [goals, selectedGoalId]
  );

  const chartData = useMemo(() => {
    const labels = ["Running", "Pending", "Completed", "Failed", "Blocked"];
    const running = goals.filter((g) => String(g?.status || "") === "running").length;
    const pending = goals.filter((g) => String(g?.status || "") === "pending").length;
    const completed = goals.filter((g) => String(g?.status || "") === "completed").length;
    const failed = goals.filter((g) => String(g?.status || "") === "failed").length;
    const blocked = goals.filter((g) => ["blocked", "awaiting_confirmation"].includes(String(g?.status || ""))).length;

    return {
      labels,
      datasets: [{
        label: "Goal Status",
        data: [running, pending, completed, failed, blocked],
        borderColor: "#42d3ff",
        backgroundColor: "rgba(66, 211, 255, 0.2)",
        tension: 0.35,
      }],
    };
  }, [goals]);

  const graph = useMemo(() => normalizeGraph(selectedGoal || {}), [selectedGoal]);

  const startGoal = async () => {
    if (!goalInput.trim()) return;
    try {
      await createAutonomyGoal(goalInput.trim(), sessionId, 5);
      setGoalInput("");
      await refresh();
    } catch (e) {
      setError(e?.message || String(e));
    }
  };

  const cancelGoal = () => {
    setError("Cancel endpoint is not available yet in this build; use task cancellation controls.");
  };

  const renderAutonomy = () => (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Autonomous Control Panel</h3>
        <div className="panel-row">
          <input value={goalInput} onChange={(e) => setGoalInput(e.target.value)} placeholder="Start a new autonomous goal" />
          <button className="panel-btn" onClick={startGoal}>Start Goal</button>
          <button className="panel-btn danger" onClick={cancelGoal}>Cancel Goal</button>
        </div>
        <div className="panel-list" style={{ marginTop: 10 }}>
          {goals.map((g, idx) => (
            <div className="panel-item" key={String(g?._id || idx)}>
              <h4>{String(g?.goal || "Goal")}</h4>
              <p>Status: {String(g?.status || "unknown")}</p>
              <p>Owner: {String(g?.owner || "system")}</p>
              <div className="panel-row">
                <button className="panel-btn" onClick={() => setSelectedGoalId(String(g?._id || ""))}>View Task Graph</button>
              </div>
            </div>
          ))}
          {!goals.length && <div className="panel-item"><p>No goals yet.</p></div>}
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Task Graph</h3>
        <div className="graph-wrap">
          <ReactFlow nodes={graph.nodes} edges={graph.edges} fitView>
            <Background />
            <Controls />
          </ReactFlow>
        </div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Runtime + Metrics</h3>
        <Line data={chartData} options={{ responsive: true, plugins: { legend: { display: false } } }} />
        <div className="log-box" style={{ marginTop: 8 }}>{JSON.stringify(status?.health || {}, null, 2)}</div>
        <div className="log-box" style={{ marginTop: 8 }}>{error || "No errors."}</div>
      </div>
      <div className="panel-card">
        <h3 className="panel-title">Background Tasks</h3>
        <div className="log-box">
          {goals
            .filter((g) => ["running", "pending", "awaiting_confirmation"].includes(String(g?.status || "")))
            .map((g) => `- ${g?.goal} [${g?.status}]`)
            .join("\n") || "No background goals running."}
        </div>
      </div>
    </div>
  );

  return (
    <div className="autonomy-shell">
      <div className="autonomy-tabs">
        {TABS.map((t) => (
          <button key={t} className={`autonomy-tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>{t}</button>
        ))}
      </div>
      <div className="autonomy-body">
        {tab === "Autonomy" && renderAutonomy()}
        {tab === "Tasks" && <TaskManager sessionId={sessionId} />}
        {tab === "Agents" && <AgentMonitor sessionId={sessionId} />}
        {tab === "Research" && <ResearchMonitor sessionId={sessionId} />}
        {tab === "Devices" && <DeviceControl sessionId={sessionId} />}
        {tab === "Health" && <SystemHealth sessionId={sessionId} logs={logs} />}
        {tab === "Self-Improvement" && <SelfImprovementPanel sessionId={sessionId} />}
      </div>
    </div>
  );
}
