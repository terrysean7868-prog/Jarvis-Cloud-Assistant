import React, { useCallback, useEffect, useMemo, useState } from "react";
import HUDLogs from "./HUDLogs";
import ArcReactor from "./ArcReactor";
import HexagonColorPicker from "./HexagonColorPicker";
import "../styles/jarvisDashboard.css";

function clamp01(n) {
  if (!Number.isFinite(n)) return 0;
  return Math.max(0, Math.min(1, n));
}

function computeTaskPercent(task) {
  const status = (task?.status || "").toString();
  if (["completed"].includes(status)) return 100;
  if (["failed", "stopped"].includes(status)) return 100;

  const steps = Array.isArray(task?.steps) ? task.steps : [];
  const totalSteps = steps.length;
  const currentStepRaw = Number(task?.current_step);
  const currentStep = Number.isFinite(currentStepRaw) ? currentStepRaw : 0;

  if (totalSteps > 0) {
    const ratio = clamp01(currentStep / totalSteps);
    return Math.round(ratio * 100);
  }

  if (status === "in_progress") return 50;
  return 0;
}

function clampPercent(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return null;
  return Math.max(0, Math.min(100, v));
}

async function copyText(text) {
  const value = (text || "").toString();
  if (!value) return false;

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // fallback below
  }

  try {
    const ta = document.createElement("textarea");
    ta.value = value;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return !!ok;
  } catch {
    return false;
  }
}

export default function JarvisDashboard({
  isAuthenticated = false,
  isDeviceConnected = false,
  logs = [],
  tasks = [],
  emotion = "calm",
  listening = false,
  speaking = false,
  volume = 0,
  agentToken = "",
  agentSharedSecret = "",
  agentServerUrl = "",
  agentWsUrl = "",
  agentCfgLoaded = false,
  agentCfgError = null,
  onConnectPcAgent,
  showConnectPcAgentButton = true,
  systemInfo,
  themeColor = "#00eaff",
  onThemeColorChange,
}) {
  const [copyStatus, setCopyStatus] = useState(null);
  const [graphicsName, setGraphicsName] = useState("—");
  const showStatusBadges = !isDeviceConnected;

  useEffect(() => {
    // Best-effort GPU renderer string (browser-side).
    try {
      const canvas = document.createElement("canvas");
      const gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
      if (!gl) {
        setGraphicsName("—");
        return;
      }
      const dbg = gl.getExtension("WEBGL_debug_renderer_info");
      if (!dbg) {
        setGraphicsName("—");
        return;
      }
      const r = gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL);
      const s = (r || "").toString().trim();
      setGraphicsName(s || "—");
    } catch {
      setGraphicsName("—");
    }
  }, []);

  const pendingTasks = useMemo(() => {
    const list = Array.isArray(tasks) ? tasks : [];
    return list
      .filter(t => t && typeof t === "object")
      .filter(t => {
        const s = (t.status || "").toString();
        return ["pending", "in_progress", "paused"].includes(s);
      })
      .sort((a, b) => {
        const ap = Number(a?.priority ?? 999);
        const bp = Number(b?.priority ?? 999);
        if (ap !== bp) return ap - bp;
        return String(b?.created_at || "").localeCompare(String(a?.created_at || ""));
      });
  }, [tasks]);

  const handleCopy = useCallback(async (label, value) => {
    const ok = await copyText(value);
    setCopyStatus(ok ? `${label} copied` : `Could not copy ${label}`);
    setTimeout(() => setCopyStatus(null), 1500);
  }, []);

  const cpuPct = clampPercent(systemInfo?.cpu_percent);
  const memPct = clampPercent(systemInfo?.memory_percent);
  const diskPct = clampPercent(systemInfo?.disk_percent);
  const procCount = Number.isFinite(Number(systemInfo?.process_count)) ? Number(systemInfo.process_count) : null;

  const tokenDisplay = useMemo(() => {
    if (!isAuthenticated) return "(login to view)";
    if (agentToken) return agentToken;
    if (!agentCfgLoaded && !agentCfgError) return "(loading…)";
    if (agentCfgError) return "(configure PC agent)";
    return "(not available)";
  }, [agentCfgError, agentCfgLoaded, agentToken, isAuthenticated]);

  const secretDisplay = useMemo(() => {
    if (!isAuthenticated) return "(login to view)";
    if (agentSharedSecret) return agentSharedSecret;
    if (!agentCfgLoaded && !agentCfgError) return "(loading…)";
    // Shared secret may be intentionally not returned (cloud mode) or not set.
    if (agentCfgError) return "(not available)";
    return "(not set / not exposed)";
  }, [agentCfgError, agentCfgLoaded, agentSharedSecret, isAuthenticated]);

  const serverUrlDisplay = useMemo(() => {
    if (!isAuthenticated) return "(login to view)";
    if (agentServerUrl) return agentServerUrl;
    if (!agentCfgLoaded && !agentCfgError) return "(loading…)";
    if (agentCfgError) return "(not available)";
    return "(not set)";
  }, [agentCfgError, agentCfgLoaded, agentServerUrl, isAuthenticated]);

  const wsUrlDisplay = useMemo(() => {
    if (!isAuthenticated) return "(login to view)";
    if (agentWsUrl) return agentWsUrl;
    if (!agentCfgLoaded && !agentCfgError) return "(loading…)";
    if (agentCfgError) return "(not available)";
    return "(not set)";
  }, [agentCfgError, agentCfgLoaded, agentWsUrl, isAuthenticated]);

  return (
    <div className="jarvis-dashboard" aria-label="Jarvis dashboard">
      <div className="jd-col jd-left">
        <section className="jd-panel" aria-label="Conversion history">
          <div className="jd-panelBody jd-scroll">
            {(Array.isArray(logs) && logs.length > 0) ? (
              <HUDLogs logs={logs} />
            ) : (
              <div className="jd-empty jd-emptyCentered">No history yet.</div>
            )}
          </div>
        </section>

        <section className="jd-panel" aria-label="Pending tasks">
          <div className="jd-panelBody jd-scroll">
            {!pendingTasks.length ? (
              <div className="jd-empty jd-emptyCentered">No pending tasks.</div>
            ) : (
              <div className="jd-taskList">
                {pendingTasks.map((t) => {
                  const id = (t?.id || "").toString() || Math.random().toString(16).slice(2);
                  const desc = (t?.description || "(no description)").toString();
                  const status = (t?.status || "").toString();
                  const steps = Array.isArray(t?.steps) ? t.steps : [];
                  const totalSteps = steps.length;
                  const currentStep = Number.isFinite(Number(t?.current_step)) ? Number(t.current_step) : 0;
                  const percent = computeTaskPercent(t);

                  return (
                    <div key={id} className="jd-task">
                      <div className="jd-taskTop">
                        <div className="jd-taskTitle">{desc}</div>
                        <div className={`jd-taskStatus jd-status-${status}`}>{status || "unknown"}</div>
                      </div>
                      <div className="jd-taskMeta">
                        <span>{percent}%</span>
                        <span>{totalSteps ? `${Math.min(currentStep, totalSteps)}/${totalSteps} steps` : ""}</span>
                      </div>
                      <div className="jd-progressTrack" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
                        <div className="jd-progressFill" style={{ width: `${percent}%` }} />
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </section>
      </div>

      <div className="jd-col jd-middle">
        <section className="jd-panel jd-character" aria-label="Character">
          <div className="jd-panelBody jd-characterBody">
            <ArcReactor
              active={!!(listening || speaking)}
              listening={!!listening}
              speaking={!!speaking}
              emotion={emotion}
              volume={volume}
              size={360}
              showCaption={false}
              themeColor={themeColor}
            />
          </div>
        </section>
      </div>

      <div className="jd-col jd-right">
        <section className="jd-panel" aria-label="PC agent code">
          <div className="jd-panelBody">
            <div className="jd-copyRow">
              <div className="jd-copyLabel">Server URL</div>
              <div className="jd-copyValue">{serverUrlDisplay}</div>
              <button className="jd-btn" onClick={() => handleCopy("Server URL", agentServerUrl)} disabled={!agentServerUrl}>Copy</button>
            </div>
            <div className="jd-copyRow">
              <div className="jd-copyLabel">WS URL</div>
              <div className="jd-copyValue">{wsUrlDisplay}</div>
              <button className="jd-btn" onClick={() => handleCopy("WS URL", agentWsUrl)} disabled={!agentWsUrl}>Copy</button>
            </div>
            <div className="jd-copyRow">
              <div className="jd-copyLabel">Agent token</div>
              <div className="jd-copyValue">{tokenDisplay}</div>
              <button className="jd-btn" onClick={() => handleCopy("Agent token", agentToken)} disabled={!agentToken}>Copy</button>
            </div>
            <div className="jd-copyRow">
              <div className="jd-copyLabel">Shared secret</div>
              <div className="jd-copyValue">{secretDisplay}</div>
              <button className="jd-btn" onClick={() => handleCopy("Shared secret", agentSharedSecret)} disabled={!agentSharedSecret}>Copy</button>
            </div>

            {!!agentCfgError && (
              <div className="jd-empty" style={{ marginTop: 10 }}>
                {String(agentCfgError)}
              </div>
            )}

            <div className="jd-connectRow">
              {showStatusBadges && (
                <div className="jd-statusBadges" aria-label="Agent status">
                  <div className="jd-statusBadge jd-statusBadgeWarning">System degraded</div>
                  <div className="jd-statusBadge jd-statusBadgeDanger">Agent offline</div>
                </div>
              )}

              {showConnectPcAgentButton && (
                <button
                  className="jd-btn jd-connectBtn"
                  onClick={onConnectPcAgent}
                  disabled={typeof onConnectPcAgent !== "function"}
                  style={{ flex: isDeviceConnected ? "1 1 auto" : "1 1 260px" }}
                >
                  Connect PC Agent
                </button>
              )}
            </div>

            <div className="jd-metricsGrid" aria-label="System metrics">
              <div className="jd-metric">
                <div className="jd-metricLabel">CPU</div>
                <div className="jd-metricValue jd-metricValueAccent">{cpuPct == null ? "—" : `${Math.round(cpuPct)}%`}</div>
                {cpuPct != null && (
                  <div className="jd-metricBarTrack" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(cpuPct)}>
                    <div className="jd-metricBarFill" style={{ width: `${cpuPct}%` }} />
                  </div>
                )}
              </div>
              <div className="jd-metric">
                <div className="jd-metricLabel">RAM</div>
                <div className="jd-metricValue jd-metricValueAccent">{memPct == null ? "—" : `${Math.round(memPct)}%`}</div>
                {memPct != null && (
                  <div className="jd-metricBarTrack" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(memPct)}>
                    <div className="jd-metricBarFill" style={{ width: `${memPct}%` }} />
                  </div>
                )}
              </div>
              <div className="jd-metric">
                <div className="jd-metricLabel">Disk</div>
                <div className="jd-metricValue jd-metricValueAccent">{diskPct == null ? "—" : `${Math.round(diskPct)}%`}</div>
                {diskPct != null && (
                  <div className="jd-metricBarTrack" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(diskPct)}>
                    <div className="jd-metricBarFill" style={{ width: `${diskPct}%` }} />
                  </div>
                )}
              </div>
              <div className="jd-metric">
                <div className="jd-metricLabel">Procs</div>
                <div className="jd-metricValue">{procCount == null ? "—" : `${procCount}`}</div>
              </div>
              <div className="jd-metric jd-metricWide">
                <div className="jd-metricLabel">Graphics</div>
                <div className="jd-metricValue jd-metricValueAccent jd-metricTrunc" title={graphicsName}>{graphicsName}</div>
              </div>
            </div>

            {copyStatus && <div className="jd-toast">{copyStatus}</div>}
          </div>
        </section>

        <section className="jd-panel" aria-label="Color picker">
          <div className="jd-panelBody jd-colorPicker">
            <div className="jd-hexPickerWrap">
              <HexagonColorPicker value={themeColor} size={190} onChange={onThemeColorChange} />
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
