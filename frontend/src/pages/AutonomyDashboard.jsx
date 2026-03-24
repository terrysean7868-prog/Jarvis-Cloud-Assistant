import React, { useCallback, useEffect, useMemo, useState } from "react";
import ReactFlow, { Background, Controls, applyEdgeChanges, applyNodeChanges } from "reactflow";
import "reactflow/dist/style.css";
import { Line } from "react-chartjs-2";
import { Chart as ChartJS, CategoryScale, LinearScale, PointElement, LineElement, Tooltip, Legend } from "chart.js";
import {
  cancelAutonomyGoal,
  controlAutonomyRuntime,
  createAutonomyGoal,
  getAutonomyGoals,
  getAutonomyStatus,
  updateAutonomyGoalGraph,
} from "../utils/api";
import TaskManager from "./TaskManager";
import AgentMonitor from "./AgentMonitor";
import ResearchMonitor from "./ResearchMonitor";
import DeviceControl from "./DeviceControl";
import SystemHealth from "./SystemHealth";
import SelfImprovementPanel from "./SelfImprovementPanel";
import AnatomyView from "./AnatomyView";
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
  "Anatomy",
];

function normalizeGraph(goal, accentColor = "#00eaff") {
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
        color: "rgba(235, 247, 255, 0.96)",
        background: "rgba(4, 12, 22, 0.88)",
        boxShadow: `0 0 0 1px ${accentColor}55 inset`,
      },
    });

    (n.dependencies || []).forEach((dep) => {
      edges.push({ id: `${dep}-${n.task_id}`, source: String(dep), target: String(n.task_id), animated: false });
    });
  });

  return { nodes, edges };
}

export default function AutonomyDashboard({ sessionId, logs = [], onTabChange = null }) {
  const [tab, setTab] = useState("Autonomy");
  const [goals, setGoals] = useState([]);
  const [status, setStatus] = useState(null);
  const [goalInput, setGoalInput] = useState("");
  const [selectedGoalId, setSelectedGoalId] = useState("");
  const [selectedNodeId, setSelectedNodeId] = useState("");
  const [editTitle, setEditTitle] = useState("");
  const [editDeps, setEditDeps] = useState("");
  const [error, setError] = useState("");
  const [graphNodes, setGraphNodes] = useState([]);
  const [graphEdges, setGraphEdges] = useState([]);

  const accentColor = useMemo(() => {
    try {
      return getComputedStyle(document.documentElement).getPropertyValue("--jarvis-accent").trim() || "#00eaff";
    } catch {
      return "#00eaff";
    }
  }, []);

  const refresh = useCallback(async () => {
    if (!sessionId) {
      setGoals([]);
      setStatus(null);
      setError("Login required to load autonomy dashboard data.");
      return;
    }
    try {
      const [gRes, sRes] = await Promise.allSettled([
        getAutonomyGoals({ sessionId, statuses: "pending,running,awaiting_confirmation,failed,completed,cancelled", limit: 80 }),
        getAutonomyStatus(sessionId),
      ]);

      const g = gRes.status === "fulfilled" ? gRes.value : null;
      const s = sRes.status === "fulfilled" ? sRes.value : null;
      const rows = Array.isArray(g?.goals) ? g.goals : [];
      setGoals(rows);
      setStatus(s || null);

      if (gRes.status === "rejected") {
        const msg = gRes.reason?.message || String(gRes.reason || "Failed to load goals");
        setError(msg);
      } else if (sRes.status === "rejected") {
        const msg = sRes.reason?.message || String(sRes.reason || "Failed to load runtime status");
        setError(msg);
      } else {
        setError("");
      }

      if (!selectedGoalId && rows.length) {
        setSelectedGoalId(String(rows[0]?._id || ""));
      }
    } catch (e) {
      setError(e?.message || String(e));
    }
  }, [sessionId, selectedGoalId]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 7000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (typeof onTabChange === "function") {
      onTabChange(tab);
    }
  }, [tab, onTabChange]);

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
        borderColor: accentColor,
        backgroundColor: "rgba(0, 234, 255, 0.2)",
        tension: 0.35,
      }],
    };
  }, [goals, accentColor]);

  const graph = useMemo(() => normalizeGraph(selectedGoal || {}, accentColor), [selectedGoal, accentColor]);

  useEffect(() => {
    setGraphNodes(graph.nodes || []);
    setGraphEdges(graph.edges || []);
  }, [graph]);

  const onNodesChange = useCallback((changes) => {
    setGraphNodes((nds) => applyNodeChanges(changes, nds));
  }, []);

  const onEdgesChange = useCallback((changes) => {
    setGraphEdges((eds) => applyEdgeChanges(changes, eds));
  }, []);
  const selectedGraphNode = useMemo(() => {
    const reports = Array.isArray(selectedGoal?.reports) ? selectedGoal.reports : [];
    const graphReport = [...reports].reverse().find((r) => r && r.graph && Array.isArray(r.graph.nodes));
    const nodes = Array.isArray(graphReport?.graph?.nodes) ? graphReport.graph.nodes : [];
    return nodes.find((n) => String(n?.task_id || "") === String(selectedNodeId || "")) || null;
  }, [selectedGoal, selectedNodeId]);

  useEffect(() => {
    if (!selectedGraphNode) {
      setEditTitle("");
      setEditDeps("");
      return;
    }
    setEditTitle(String(selectedGraphNode?.title || ""));
    setEditDeps(Array.isArray(selectedGraphNode?.dependencies) ? selectedGraphNode.dependencies.join(",") : "");
  }, [selectedGraphNode]);

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

  const cancelGoal = async () => {
    const gid = String(selectedGoalId || selectedGoal?._id || "").trim();
    if (!gid) {
      setError("Select a goal to cancel.");
      return;
    }
    try {
      await cancelAutonomyGoal(gid, sessionId, "user_requested");
      await refresh();
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  };

  const runtimeControl = async (action) => {
    try {
      await controlAutonomyRuntime(action, sessionId);
      await refresh();
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  };

  const saveNodeEdit = async () => {
    if (!selectedGoalId || !selectedNodeId) return;
    const deps = String(editDeps || "")
      .split(",")
      .map((d) => d.trim())
      .filter(Boolean);
    try {
      await updateAutonomyGoalGraph(selectedGoalId, {
        session_id: sessionId,
        nodes: [{ task_id: selectedNodeId, title: editTitle.trim(), dependencies: deps }],
      });
      await refresh();
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  };

  const moveNode = async (dir) => {
    const reports = Array.isArray(selectedGoal?.reports) ? selectedGoal.reports : [];
    const graphReport = [...reports].reverse().find((r) => r && r.graph && Array.isArray(r.graph.nodes));
    const nodes = Array.isArray(graphReport?.graph?.nodes) ? graphReport.graph.nodes : [];
    const idx = nodes.findIndex((n) => String(n?.task_id || "") === String(selectedNodeId || ""));
    if (idx < 0) return;
    const to = dir === "up" ? idx - 1 : idx + 1;
    if (to < 0 || to >= nodes.length) return;
    try {
      await updateAutonomyGoalGraph(selectedGoalId, {
        session_id: sessionId,
        move: { task_id: selectedNodeId, to_index: to },
      });
      await refresh();
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  };

  const rerunFailed = async () => {
    if (!selectedGoalId) return;
    try {
      await updateAutonomyGoalGraph(selectedGoalId, {
        session_id: sessionId,
        rerun_failed: true,
      });
      await refresh();
      setError("");
    } catch (e) {
      setError(e?.message || String(e));
    }
  };

  const renderAutonomy = () => (
    <div className="panel-grid">
      <div className="panel-card">
        <h3 className="panel-title">Autonomous Control Panel</h3>
        <div className="panel-row">
          <input value={goalInput} onChange={(e) => setGoalInput(e.target.value)} placeholder="Start a new autonomous goal" />
          <button className="panel-btn" onClick={startGoal}>Start Goal</button>
          <button className="panel-btn danger" onClick={cancelGoal}>Cancel Goal</button>
          <button className="panel-btn" onClick={() => runtimeControl("pause")}>Pause</button>
          <button className="panel-btn" onClick={() => runtimeControl("resume")}>Resume</button>
          <button className="panel-btn" onClick={() => runtimeControl("tick")}>Tick</button>
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
          <ReactFlow
            nodes={graphNodes.map((n) => ({
              ...n,
              selected: String(n.id) === String(selectedNodeId || ""),
              style: {
                ...(n.style || {}),
                border: String(n.id) === String(selectedNodeId || "")
                  ? `1px solid ${accentColor}`
                  : (n.style?.border || "1px solid rgba(255,255,255,0.2)"),
              },
            }))}
            edges={graphEdges}
            fitView
            onNodeClick={(_, node) => setSelectedNodeId(String(node?.id || ""))}
            onNodesChange={onNodesChange}
            onEdgesChange={onEdgesChange}
          >
            <Background color={accentColor} gap={18} size={1} />
            <Controls />
          </ReactFlow>
        </div>
        <div className="panel-row" style={{ marginTop: 10 }}>
          <input value={editTitle} onChange={(e) => setEditTitle(e.target.value)} placeholder="Edit node title" />
          <input value={editDeps} onChange={(e) => setEditDeps(e.target.value)} placeholder="Dependencies (comma-separated task_ids)" />
        </div>
        <div className="panel-row" style={{ marginTop: 8 }}>
          <button className="panel-btn" onClick={saveNodeEdit}>Save Node</button>
          <button className="panel-btn" onClick={() => moveNode("up")}>Move Up</button>
          <button className="panel-btn" onClick={() => moveNode("down")}>Move Down</button>
          <button className="panel-btn warn" onClick={rerunFailed}>Re-run Failed</button>
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
        {tab === "Anatomy" && <AnatomyView sessionId={sessionId} />}
      </div>
    </div>
  );
}
