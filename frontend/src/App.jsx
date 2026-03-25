// src/App.jsx
import React, { useState, useEffect, useRef, useCallback, useMemo, Suspense, lazy } from "react";
import { listenOnce, speak, initAudioProcessing, primeSpeechRecognition, recordPcm16Once, startPcm16Recorder, startWebSpeechHold } from "./utils/speech";
import {
  sendMessage,
  addLearningExample,
  setAssistantName as setAssistantNameApi,
  getUserPreferences,
  setUserPreferences,
  getUserDevice,
  setUserDevice,
  configureMyPc,
  dispatchDeviceActions,
  grantDevicePermissions,
  googleSpeechToText,
  secureVoiceToText,
  getAgentConfig,
  getTasks,
  getSystemInfo,
  getDeviceStatus,
  API_URL,
  getNotificationsWsUrl,
  stopTask,
  getAdminUpdateHistory,
  logRequirementEvent,
} from "./utils/api";
import "./styles/jarvis.css";
import AuthModal from "./components/AuthModal";
import JarvisDashboard from "./components/JarvisDashboard";
import UpdateManagementConsole from "./components/UpdateManagementConsole";
import AutonomyDashboard from "./pages/AutonomyDashboard";

const scheduleIdle = (fn, timeout = 800) => {
  if (typeof window === "undefined") return setTimeout(fn, 0);
  if (typeof window.requestIdleCallback === "function") {
    return window.requestIdleCallback(fn, { timeout });
  }
  return setTimeout(fn, Math.min(250, timeout));
};

// Lazy-load heavy UI pieces
const PermissionModal = lazy(() => import("./components/PermissionModal"));

export default function App() {
  const WAKE_SESSION_MINUTES = useMemo(() => {
    const raw = "15";
    const n = Number(raw);
    return Number.isFinite(n) ? Math.max(1, Math.min(n, 120)) : 15;
  }, []);

  const WAKE_SESSION_MS = useMemo(() => WAKE_SESSION_MINUTES * 60 * 1000, [WAKE_SESSION_MINUTES]);
  const isMobile = useMemo(() => {
    try {
      const ua = (navigator.userAgent || "").toLowerCase();
      return /android|iphone|ipad|ipod/.test(ua);
    } catch {
      return false;
    }
  }, []);

  const isIOS = useMemo(() => {
    try {
      const ua = (navigator.userAgent || "").toLowerCase();
      return /iphone|ipad|ipod/.test(ua);
    } catch {
      return false;
    }
  }, []);

  const [preferredLanguage, setPreferredLanguage] = useState(() => {
    try {
      return localStorage.getItem("jarvis_language") || null;
    } catch {
      return null;
    }
  });

  const voiceLang = useMemo(() => {
    try {
      const preferred = (preferredLanguage || "").toString().trim();
      return preferred || (navigator.language || "en-US").toString();
    } catch {
      return "en-US";
    }
  }, [preferredLanguage]);

  const googleSttEnabled = useMemo(() => {
    const raw = "false";
    return ["1", "true", "yes", "y"].includes(raw.toLowerCase());
  }, []);

  // Auto-enable speaker verification when the logged-in user has enrolled biometrics.
  const [voiceBiometricsEnrolled, setVoiceBiometricsEnrolled] = useState(() => {
    try {
      return localStorage.getItem("jarvis_voice_biometrics_enrolled") === "1";
    } catch {
      return false;
    }
  });

  const voiceBiometricsEnabled = !!voiceBiometricsEnrolled;
  // Biometrics mode requires the secure server audio path (Google STT).
  // If the UI hasn't enabled Google STT, gracefully fall back to WebSpeech
  // instead of becoming unusable.
  const voiceBiometricsActive = useMemo(() => {
    return !!voiceBiometricsEnabled && !!googleSttEnabled;
  }, [voiceBiometricsEnabled, googleSttEnabled]);

  // Light state only — avoid large objects in state
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [wakeListeningOnline, setWakeListeningOnline] = useState(false);
  const [wakePulse, setWakePulse] = useState(false);
  const [logs, setLogs] = useState(() => {
    try {
      const raw = sessionStorage.getItem("jarvis_logs_cache");
      const parsed = raw ? JSON.parse(raw) : null;
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return [];
    }
  });
  const [emotion, setEmotion] = useState("calm"); // calm|action|analyzing|critical
  const [volume, setVolume] = useState(0); // 0..1

  const [themeColor, setThemeColor] = useState(() => {
    try {
      return localStorage.getItem("jarvis_theme_color") || "#00eaff";
    } catch {
      return "#00eaff";
    }
  });

  const [tasks, setTasks] = useState([]);
  const [agentToken, setAgentToken] = useState("");
  const [agentSharedSecret, setAgentSharedSecret] = useState("");
  const [agentServerUrl, setAgentServerUrl] = useState("");
  const [agentWsUrl, setAgentWsUrl] = useState("");
  const [agentCfgLoaded, setAgentCfgLoaded] = useState(false);
  const [agentCfgError, setAgentCfgError] = useState(null);
  const [systemInfo, setSystemInfo] = useState(null);
  const [systemHealth, setSystemHealth] = useState(null);
  const [agentOffline, setAgentOffline] = useState(false);
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [username, setUsername] = useState(null);
  const [assistantName, setAssistantName] = useState("Jarvis");
  const [role, setRole] = useState(null);
  const [, setPermissions] = useState(null);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [permissionPrompt, setPermissionPrompt] = useState(null);
  const [pendingResume, setPendingResume] = useState(null);
  const [showUpdateConsole, setShowUpdateConsole] = useState(false);
  const [autonomyTab, setAutonomyTab] = useState("Autonomy");
  const [activeDisplay, setActiveDisplay] = useState(() => {
    try {
      const saved = (localStorage.getItem("jarvis_active_display") || "").toString().trim().toLowerCase();
      return saved === "autonomy" ? "autonomy" : "dashboard";
    } catch {
      return "dashboard";
    }
  });

  useEffect(() => {
    const nm = (assistantName || "Jarvis").toString().trim() || "Jarvis";
    try {
      document.title = `${nm} - AI Assistant`;
    } catch {}
  }, [assistantName]);

  const googleSttDisabledUntilRef = useRef(0);
  const lastWakeNoSpeechLogRef = useRef(0);
  const wakeDisabledUntilRef = useRef(0);
  const wakePermissionHintedRef = useRef(false);
  const lastWakeDetectedAtRef = useRef(0);
  const [voiceUnlocked, setVoiceUnlocked] = useState(() => !isMobile);
  const [vizAudioUnlocked, setVizAudioUnlocked] = useState(false);

  // refs
  const wakeRecognizer = useRef(null);
  const isHandlingCommand = useRef(false);
  const pendingTranscriptRef = useRef(null);
  const speakingRef = useRef(false);
  const wakeSessionUntilRef = useRef(0);
  const wakeSessionTimerRef = useRef(null);
  const assistantNameRef = useRef("Jarvis");
  const micStreamRef = useRef(null);
  const notificationsWsRef = useRef(null);
  const notificationsReconnectTimerRef = useRef(null);
  const latestResearchTaskIdRef = useRef(null);
  const permissionPromptRef = useRef(null);
  const permissionPromptSpokenKeyRef = useRef("");

  const pttControllerRef = useRef(null);
  const pttInitPromiseRef = useRef(null);
  const pttSeqRef = useRef(0);
  const [pttHolding, setPttHolding] = useState(false);

  useEffect(() => {
    speakingRef.current = !!speaking;
  }, [speaking]);

  useEffect(() => {
    try {
      window.__jarvisPreferredLang = voiceLang;
    } catch {}
  }, [voiceLang]);

  useEffect(() => {
    const hex = (themeColor || "").toString().trim();
    if (!/^#([0-9a-f]{6}|[0-9a-f]{3})$/i.test(hex)) return;

    const normalize = (h) => {
      const v = h.toLowerCase();
      if (v.length === 4) {
        return `#${v[1]}${v[1]}${v[2]}${v[2]}${v[3]}${v[3]}`;
      }
      return v;
    };

    const full = normalize(hex);
    const r = parseInt(full.slice(1, 3), 16);
    const g = parseInt(full.slice(3, 5), 16);
    const b = parseInt(full.slice(5, 7), 16);
    const glow = `rgba(${r}, ${g}, ${b}, 0.45)`;
    const rgb = `${r}, ${g}, ${b}`;

    try {
      document.documentElement.style.setProperty("--jarvis-accent", full);
      document.documentElement.style.setProperty("--jarvis-accent-glow", glow);
      document.documentElement.style.setProperty("--jarvis-accent-rgb", rgb);
    } catch {}

    try {
      localStorage.setItem("jarvis_theme_color", full);
    } catch {}
  }, [themeColor]);

  // Minimal log writer (memoized to avoid re-creating)
  const addLog = useCallback((type, message) => {
    setLogs(prev => [{ type, message, time: new Date().toLocaleTimeString() }, ...prev.slice(0, 12)]);
  }, []);

  const addStructuredLog = useCallback((type, message, messageType = "text", payload = null) => {
    setLogs(prev => [
      { type, message, messageType, payload, time: new Date().toLocaleTimeString() },
      ...prev.slice(0, 24),
    ]);
  }, []);

  const auditRequirementEvent = useCallback(async (event) => {
    try {
      if (!sessionId) return;
      await logRequirementEvent({ sessionId, ...(event || {}) }, 4000);
    } catch {
      // best effort only
    }
  }, [sessionId]);

  const buildRequirementDetails = useCallback((req) => {
    const requirement = req && typeof req === "object" ? req : {};
    const lines = [];
    lines.push(`Requirement Type: ${String(requirement.requirement_type || "missing_requirement").replace(/_/g, " ")}`);
    lines.push(`Target: ${String(requirement.target_application || requirement.target || "system")}`);
    lines.push(`Why: ${String(requirement.why || "This is required to continue the requested action.")}`);
    const requiredBy = String(requirement.required_by || "user").trim().toLowerCase();
    lines.push(`Who should act: ${requiredBy === "admin" ? "Administrator" : "Current user"}`);
    const guidance = Array.isArray(requirement.guidance) ? requirement.guidance : [];
    if (guidance.length) {
      lines.push("How to resolve:");
      guidance.forEach((step, idx) => lines.push(`${idx + 1}. ${String(step)}`));
    }
    lines.push("Resume Behavior: Once resolved, I will resume the original task automatically.");
    return lines.join("\n");
  }, []);

  const promptForRequirement = useCallback((opts) => {
    const requirement = opts?.requirement || {};
    const requirementType = String(requirement.requirement_type || opts?.requirementType || "missing_requirement");
    const target = String(requirement.target || opts?.target || "System");
    const permissionOrScope = String(requirement.permission_or_scope || opts?.permissionOrScope || "").trim() || null;
    const title = opts?.title || `Requirement required: ${requirementType.replace(/_/g, " ")}`;
    const message = opts?.message || String(opts?.rawMessage || "Additional permission or information is required to continue.");
    const details = buildRequirementDetails({ ...requirement, requirement_type: requirementType, target, permission_or_scope: permissionOrScope });
    const prompt = {
      title,
      message,
      details,
      requirementType,
      target,
      permissionOrScope,
      requiredBy: String(requirement.required_by || "user").trim().toLowerCase(),
      pendingActions: Array.isArray(opts?.pendingActions) ? opts.pendingActions : [],
      sourceText: String(opts?.sourceText || ""),
      neededPermissions: opts?.neededPermissions || null,
      actionMode: String(opts?.actionMode || "ack"),
      allowLabel: opts?.allowLabel || (opts?.actionMode === "grant_permission" ? "Grant and Continue" : "I Fixed It"),
      denyLabel: opts?.denyLabel || "Not Now",
    };
    setPermissionPrompt(prompt);

    auditRequirementEvent({
      requestedAction: String(opts?.requestedAction || "unknown"),
      requirementType,
      target,
      permissionOrScope,
      status: "pending",
      details: {
        message,
        action_mode: prompt.actionMode,
      },
    });
  }, [auditRequirementEvent, buildRequirementDetails]);

  useEffect(() => {
    permissionPromptRef.current = permissionPrompt;
  }, [permissionPrompt]);

  useEffect(() => {
    const req = permissionPrompt;
    if (!req) return;

    const key = [
      String(req?.actionMode || "ack"),
      String(req?.title || ""),
      String(req?.message || ""),
    ].join("|");
    if (permissionPromptSpokenKeyRef.current === key) return;
    permissionPromptSpokenKeyRef.current = key;

    try {
      const isContinueConfirm = String(req?.actionMode || "") === "confirm_resume_after_permission";
      const spoken = isContinueConfirm
        ? "Permission granted. Do you want me to continue the task now? Say continue or not now."
        : `${String(req?.title || "Permission required")}. ${String(req?.message || "Additional permission is required.")} Say allow to proceed, or not now to skip.`;
      speak(spoken);
    } catch {}
  }, [permissionPrompt]);

  const resolvePermissionPromptDecision = useCallback(async (req, approved) => {
    if (!req) return;

    if (!approved) {
      if (req?.actionMode === "confirm_resume_after_permission") {
        addLog("system", "Okay, I will not continue the task now. You can resume it manually anytime.");
        try { speak("Okay. I will wait for your manual continue command."); } catch {}
        setPermissionPrompt(null);
        return;
      }

      auditRequirementEvent({
        requestedAction: String(req?.sourceText || "device_action"),
        requirementType: String(req?.requirementType || "missing_requirement"),
        target: String(req?.target || "system"),
        permissionOrScope: req?.permissionOrScope || null,
        status: "denied",
        details: { action_mode: req?.actionMode || "ack" },
      });
      addLog("system", "Requirement not granted yet. I can still help with alternatives that do not need this permission.");
      setPermissionPrompt(null);
      return;
    }

    // Approved path
    setPermissionPrompt(null);

    if (req?.actionMode === "confirm_resume_after_permission") {
      if (Array.isArray(req?.pendingActions) && req.pendingActions.length) {
        await dispatchDeviceActions(req.pendingActions || [], sessionId, req.sourceText || "");
        addLog("system", "Continuing your task now.");
      } else {
        addLog("system", "No pending action to continue.");
      }
      return;
    }

    if (req?.actionMode === "grant_permission" && req?.neededPermissions) {
      const grantRes = await grantDevicePermissions(sessionId, req.neededPermissions);
      if (grantRes?.offline) {
        addLog("system", "Permission saved. Start JarvisPCAgent.exe (or python pc_agent.py) and keep it connected. Continue manually after reconnect.");
        setPendingResume({
          type: "device_connection",
          createdAt: Date.now(),
          pendingActions: req.pendingActions || [],
          sourceText: req.sourceText || "",
        });
        return;
      }
      if (Array.isArray(req?.pendingActions) && req.pendingActions.length) {
        setPermissionPrompt({
          title: "Continue Task?",
          message: "Permission was granted. Do you want me to continue your pending task now?",
          details: [
            "Say Continue to run the queued action now.",
            "Say Not now to keep it pending for manual execution.",
          ],
          allowLabel: "Continue",
          denyLabel: "Not now",
          actionMode: "confirm_resume_after_permission",
          pendingActions: req.pendingActions || [],
          sourceText: req.sourceText || "",
          requirementType: "manual_confirmation",
          target: "Pending Task",
        });
        addLog("system", "Permission granted. Waiting for your confirmation to continue the task.");
      } else {
        addLog("system", "Permission granted.");
      }
    } else if (req?.actionMode === "configure_device") {
      await configureMyPc(sessionId);
      await dispatchDeviceActions(req.pendingActions || [], sessionId, req.sourceText || "");
      addLog("system", "Device configured. Resumed your original task.");
    } else if (req?.actionMode === "wait_for_connection") {
      setPendingResume({
        type: "device_connection",
        createdAt: Date.now(),
        pendingActions: req.pendingActions || [],
        sourceText: req.sourceText || "",
      });
      addLog("system", "Connection requirement noted. You can continue manually after reconnect.");
    } else if (req?.actionMode === "retry_voice") {
      wakeDisabledUntilRef.current = 0;
      wakePermissionHintedRef.current = false;
      try { wakeRecognizer.current?.start(); } catch {}
      addLog("system", "Voice permission updated. Wake listening resumed.");
    } else if (req?.actionMode === "retry_source_text") {
      const src = String(req?.sourceText || "").trim();
      if (src) {
        await sendMessage(src, "voice", sessionId);
        addLog("system", "Requirement resolved. Retried your original request.");
      } else {
        addLog("system", "Requirement acknowledged. Ready to continue once you provide the original request.");
      }
    } else if (Array.isArray(req?.pendingActions) && req.pendingActions.length) {
      await dispatchDeviceActions(req.pendingActions || [], sessionId, req.sourceText || "");
      addLog("system", "Requirement acknowledged. Attempting to resume your original task.");
    }

    auditRequirementEvent({
      requestedAction: String(req?.sourceText || "device_action"),
      requirementType: String(req?.requirementType || "missing_requirement"),
      target: String(req?.target || "system"),
      permissionOrScope: req?.permissionOrScope || null,
      status: "granted",
      details: { action_mode: req?.actionMode || "ack" },
    });
  }, [addLog, auditRequirementEvent, sessionId]);

  const buildDeviceCompletionSpeech = useCallback((results, sourceText) => {
    const rows = Array.isArray(results) ? results : [];
    const successStates = new Set(["success", "opened", "edited", "written", "copied", "moved", "deleted"]);

    const statusOf = (r) => (r?.status || "").toString().trim().toLowerCase();
    const actionOf = (r) => (r?.action_type || r?.action || r?.type || "").toString().trim().toLowerCase();

    const successRows = rows.filter((r) => successStates.has(statusOf(r)));
    const failedRows = rows.filter((r) => {
      const st = statusOf(r);
      return st && !successStates.has(st);
    });

    const labelMap = {
      open_app: "open app",
      close_app: "close app",
      switch_app: "switch app",
      open_url: "open link",
      open_path: "open folder",
      type_text: "type text",
      press_key: "press keys",
      hotkey: "use shortcut",
      set_volume: "set volume",
      set_mute: "set mute",
      set_brightness: "set brightness",
      set_power_plan: "set power plan",
      set_wifi: "set Wi-Fi",
      set_bluetooth: "set Bluetooth",
      set_airplane_mode: "set airplane mode",
      execute_command: "run command",
      list_processes: "list processes",
      kill_process: "stop process",
      self_update: "apply update",
    };

    const humanize = (actionType) => {
      const t = (actionType || "").toString().trim().toLowerCase();
      if (!t) return "complete the task";
      if (labelMap[t]) return labelMap[t];
      return t.replace(/_/g, " ");
    };

    if (successRows.length === 0) {
      if (failedRows.length > 0) {
        return "I couldn't complete that request.";
      }
      return "Your request is completed.";
    }

    const uniqueActions = [];
    const seen = new Set();
    for (const r of successRows) {
      const a = actionOf(r);
      const key = a || "unknown";
      if (seen.has(key)) continue;
      seen.add(key);
      uniqueActions.push(humanize(a));
      if (uniqueActions.length >= 3) break;
    }

    const actionPart = uniqueActions.length
      ? uniqueActions.join(", ")
      : "your task";

    if (failedRows.length > 0) {
      return `I completed ${successRows.length} action${successRows.length > 1 ? "s" : ""}, but ${failedRows.length} need attention.`;
    }

    const src = (sourceText || "").toString().trim();
    if (src) {
      return `Done. I completed ${actionPart}.`;
    }
    return `Done. I completed ${actionPart}.`;
  }, []);

  const speakNotificationCompletion = useCallback((text) => {
    const spoken = (text || "").toString().trim();
    if (!spoken) return;
    try {
      setSpeaking(true);
      speak(spoken, () => {
        setSpeaking(false);
        try { wakeRecognizer.current?.start(); } catch {}
      });
    } catch {}
  }, []);

  const buildResearchCompletionSpeech = useCallback((msg, state = "complete") => {
    const topic = (msg?.topic || "").toString().trim();
    if (state === "failed") {
      return topic
        ? `Research failed for ${topic}. Please check the details.`
        : "Research failed. Please check the details.";
    }
    if (state === "cancelled") {
      return topic ? `Research cancelled for ${topic}.` : "Research cancelled.";
    }
    return topic ? `Research complete for ${topic}.` : "Research complete.";
  }, []);

  const buildWorkflowCompletionSpeech = useCallback((msg) => {
    const type = (msg?.type || "").toString().trim().toLowerCase();
    const status = (msg?.status || "").toString().trim().toLowerCase();
    const summary = (msg?.summary || msg?.message || msg?.result || "").toString().trim();

    const isFailure =
      ["failed", "error", "forbidden", "denied"].includes(status) ||
      /failed|error|denied|forbidden/.test(type);
    const isCancelled = ["cancelled", "canceled", "stopped"].includes(status) || /cancelled|canceled|stopped/.test(type);

    const area = (() => {
      if (/self[_-]?update|update/.test(type)) return "self update";
      if (/improvement|improve/.test(type)) return "improvement";
      if (/learning|learn/.test(type)) return "learning";
      if (/research/.test(type)) return "research";
      return "operation";
    })();

    if (isCancelled) {
      return `The ${area} operation was cancelled.`;
    }
    if (isFailure) {
      return `The ${area} operation needs attention.`;
    }

    const hasCompletionSignal =
      ["success", "completed", "done", "ok", "saved", "applied", "learned"].includes(status) ||
      /complete|completed|done|success|saved|applied|learned/.test(type) ||
      /complete|completed|done|saved|applied|learned/.test(summary.toLowerCase());

    if (!hasCompletionSignal) return "";

    if (summary) {
      return `Done. ${summary.length > 120 ? `${summary.slice(0, 117)}...` : summary}`;
    }
    return `Done. The ${area} operation is completed.`;
  }, []);

  const buildDirectChatCompletionSpeech = useCallback((res) => {
    const response = res && typeof res === "object" ? res : {};
    const text = (response?.text || "").toString().trim();
    const textLower = text.toLowerCase();

    const actions = Array.isArray(response?.actions) ? response.actions : [];
    const actionResults = Array.isArray(response?.action_results) ? response.action_results : [];

    const actionTypeOf = (row) => (row?.type || row?.action_type || row?.action || "").toString().trim().toLowerCase();
    const statusOf = (row) => (row?.status || "").toString().trim().toLowerCase();

    const types = new Set();
    for (const a of actions) {
      const t = actionTypeOf(a);
      if (t) types.add(t);
    }
    for (const r of actionResults) {
      const t = actionTypeOf(r);
      if (t) types.add(t);
    }

    const hasTypeLike = (re) => Array.from(types).some((t) => re.test(t));
    const hasResearchPayload = !!(response?.research_report || (response?.research && (response.research?.summary || response.research?.sources)));
    const isResearch =
      hasResearchPayload ||
      hasTypeLike(/research|web_search|fetch_url/) ||
      /research complete|research finished|research ready/.test(textLower);

    if (isResearch) {
      const topic = (response?.research_report?.topic || response?.research?.topic || "").toString().trim();
      return buildResearchCompletionSpeech({ topic }, "complete");
    }

    const isWorkflow = hasTypeLike(/self[_-]?update|improvement|improve|learning|learn/);
    if (!isWorkflow) return "";

    const succeededStates = new Set(["success", "completed", "done", "ok", "saved", "applied", "learned"]);
    let ok = 0;
    let bad = 0;
    for (const r of actionResults) {
      const st = statusOf(r);
      if (!st) continue;
      if (succeededStates.has(st)) ok += 1;
      else bad += 1;
    }

    const status = bad > 0 ? "failed" : (ok > 0 ? "success" : "");
    const typeHint = hasTypeLike(/self[_-]?update/) ? "self_update" : (hasTypeLike(/improvement|improve/) ? "improvement" : "learning");
    return buildWorkflowCompletionSpeech({
      type: typeHint,
      status,
      summary: text,
      message: text,
    });
  }, [buildResearchCompletionSpeech, buildWorkflowCompletionSpeech]);

  useEffect(() => {
    try {
      // Keep small, fast cache for refresh (session-only).
      sessionStorage.setItem("jarvis_logs_cache", JSON.stringify(Array.isArray(logs) ? logs.slice(0, 20) : []));
    } catch {}
  }, [logs]);

  // Realtime notifications (research completion, failures, etc.)
  useEffect(() => {
    if (!isAuthenticated || !sessionId) return;

    let closedByCleanup = false;
    let retry = 0;

    const cleanup = () => {
      closedByCleanup = true;
      try {
        if (notificationsReconnectTimerRef.current) {
          clearTimeout(notificationsReconnectTimerRef.current);
          notificationsReconnectTimerRef.current = null;
        }
      } catch {}
      try {
        if (notificationsWsRef.current) {
          notificationsWsRef.current.onopen = null;
          notificationsWsRef.current.onmessage = null;
          notificationsWsRef.current.onerror = null;
          notificationsWsRef.current.onclose = null;
          notificationsWsRef.current.close();
        }
      } catch {}
      notificationsWsRef.current = null;
    };

    const connect = () => {
      const wsUrl = getNotificationsWsUrl(sessionId);
      if (!wsUrl) return;

      try {
        const ws = new WebSocket(wsUrl);
        notificationsWsRef.current = ws;

        ws.onopen = () => {
          retry = 0;
          addLog("system", "Realtime notifications connected.");
        };

        ws.onmessage = (evt) => {
          let msg = null;
          try {
            msg = JSON.parse(evt.data);
          } catch {
            return;
          }

          const type = (msg?.type || "").toString();
          if (!type || type === "ping" || type === "ack") return;

          if (type === "research_complete") {
            const topic = (msg?.topic || "").toString();
            const summary = (msg?.summary || "").toString();
            const taskId = (msg?.task_id || "").toString();
            if (taskId) latestResearchTaskIdRef.current = taskId;
            addLog("response", `Research complete${topic ? `: ${topic}` : ""}.\n\n${summary}`);
            addStructuredLog("response", `Research report${topic ? `: ${topic}` : ""}`, "research_report", {
              topic,
              summary,
              sources: msg?.sources || [],
            });
            speakNotificationCompletion(buildResearchCompletionSpeech(msg, "complete"));
            return;
          }

          if (type === "research_failed") {
            const topic = (msg?.topic || "").toString();
            const err = (msg?.error || "").toString();
            const taskId = (msg?.task_id || "").toString();
            if (taskId) latestResearchTaskIdRef.current = taskId;
            addLog("error", `Research failed${topic ? `: ${topic}` : ""}. ${err || ""}`.trim());
            speakNotificationCompletion(buildResearchCompletionSpeech(msg, "failed"));
            return;
          }

          if (type === "research_cancelled") {
            const topic = (msg?.topic || "").toString();
            addLog("system", `Research cancelled${topic ? `: ${topic}` : ""}.`);
            speakNotificationCompletion(buildResearchCompletionSpeech(msg, "cancelled"));
            return;
          }

          if (type === "device_job_result") {
            const deviceId = (msg?.device_id || "").toString();
            const jobId = (msg?.job_id || "").toString();
            const sourceText = (msg?.source_text || "").toString();
            if (sourceText === "system_info") {
              return;
            }
            const results = Array.isArray(msg?.results) ? msg.results : [];

            let ok = 0;
            let err = 0;
            let forbidden = 0;
            for (const r of results) {
              const st = (r?.status || "").toString().toLowerCase();
              if (st === "success" || st === "opened" || st === "edited" || st === "written" || st === "copied" || st === "moved" || st === "deleted") ok += 1;
              else if (st === "forbidden") forbidden += 1;
              else if (st) err += 1;
            }

            const header = `PC action completed${deviceId ? ` (${deviceId})` : ""}${jobId ? ` [${jobId}]` : ""}.`;
            const summary = `Results: ${ok} ok, ${err} error, ${forbidden} forbidden.`;
            const details = sourceText ? `Source: ${sourceText}` : "";
            addLog("system", [header, summary, details].filter(Boolean).join("\n"));

            // If actions failed because of permissions/access, prompt user with concrete guidance.
            try {
              const failed = results.filter((r) => {
                const st = String(r?.status || "").toLowerCase();
                return st === "forbidden" || st === "error";
              });
              if (failed.length) {
                const first = failed[0] || {};
                const actionType = String(first?.action_type || first?.action || "device_action").toLowerCase();
                const msg = String(first?.message || first?.error || "Permission or access requirement not satisfied.");
                const lower = msg.toLowerCase();

                if (/screen|record|capture|accessibility|automation/.test(lower) || /capture_screen|screen_navigation/.test(actionType)) {
                  promptForRequirement({
                    requestedAction: sourceText || actionType,
                    requirementType: /accessibility|automation/.test(lower)
                      ? "third_party_app_permission"
                      : "operating_system_permission",
                    target: /accessibility|automation/.test(lower)
                      ? "Desktop Automation / Accessibility Access"
                      : "OS Screen Recording Permission",
                    message: msg,
                    requirement: {
                      requirement_type: /accessibility|automation/.test(lower)
                        ? "third_party_app_permission"
                        : "operating_system_permission",
                      target: /accessibility|automation/.test(lower)
                        ? "Desktop Automation / Accessibility Access"
                        : "OS Screen Recording Permission",
                      target_application: /accessibility|automation/.test(lower)
                        ? "Jarvis PC Agent / target application"
                        : "Operating System Privacy Settings",
                      required_by: "user",
                      why: "This action needs OS/app-level permission that is currently blocked.",
                      guidance: /accessibility|automation/.test(lower)
                        ? [
                            "Open OS accessibility/automation permissions.",
                            "Allow Jarvis PC Agent (and target app if required).",
                            "Restart the app/agent and return here.",
                          ]
                        : [
                            "Open OS Privacy/Security settings.",
                            "Allow screen recording/screen capture for Jarvis desktop app or PC Agent.",
                            "Restart app/agent if required.",
                          ],
                      resume_automatically: true,
                    },
                    actionMode: "retry_source_text",
                    allowLabel: "I Enabled It",
                    pendingActions: [],
                    sourceText: sourceText || actionType,
                  });
                }
              }
            } catch {}

            // Voice UX: confirm completion when all actions succeeded.
            try {
              const allGood = ok > 0 && err === 0 && forbidden === 0;
              if (allGood) {
                const spoken = buildDeviceCompletionSpeech(results, sourceText);

                setSpeaking(true);
                speak(spoken, () => {
                  setSpeaking(false);
                  try { wakeRecognizer.current?.start(); } catch {}
                });
              }
            } catch {}
            return;
          }

          if (/self[_-]?update|improvement|learn|learning/.test(type)) {
            try {
              const spoken = buildWorkflowCompletionSpeech(msg);
              if (spoken) speakNotificationCompletion(spoken);
            } catch {}
          }

          addLog("system", `${type}: ${JSON.stringify(msg)}`);
        };

        ws.onclose = (evt) => {
          if (closedByCleanup) return;

          // 1008 = policy violation (we use this for missing/invalid session_id)
          if (evt && evt.code === 1008) {
            addLog("system", "Realtime notifications disconnected (auth required). Please login again.");
            localStorage.removeItem("jarvis_session");
            localStorage.removeItem("jarvis_username");
            localStorage.removeItem("jarvis_role");
            localStorage.removeItem("jarvis_permissions");
            setIsAuthenticated(false);
            setSessionId(null);
            setUsername(null);
            setRole(null);
            setPermissions(null);
            setShowAuthModal(true);
            return;
          }

          retry += 1;
          const delay = Math.min(30000, 500 * Math.pow(2, Math.min(retry, 6)));
          try {
            if (notificationsReconnectTimerRef.current) clearTimeout(notificationsReconnectTimerRef.current);
          } catch {}
          notificationsReconnectTimerRef.current = setTimeout(connect, delay);
        };
      } catch {
        // ignore
      }
    };

    connect();
    return cleanup;
  }, [
    isAuthenticated,
    sessionId,
    addLog,
    addStructuredLog,
    buildDeviceCompletionSpeech,
    buildResearchCompletionSpeech,
    buildWorkflowCompletionSpeech,
    speakNotificationCompletion,
    promptForRequirement,
  ]);

  useEffect(() => {
    if (!isAuthenticated || !sessionId) return;
    let cancelled = false;

    (async () => {
      try {
        // Fast-path: hydrate from cache immediately.
        try {
          const raw = sessionStorage.getItem(`jarvis_agent_cfg_${sessionId}`);
          const cached = raw ? JSON.parse(raw) : null;
          if (cached && typeof cached === "object") {
            setAgentToken((cached.agent_token || "").toString());
            setAgentSharedSecret((cached.agent_shared_secret || "").toString());
            setAgentServerUrl((cached.server_url || "").toString());
            setAgentWsUrl((cached.ws_url || "").toString());
          }
        } catch {}

        setAgentCfgError(null);
        setAgentCfgLoaded(false);

        // Passing device_id avoids an extra server-side DB lookup (owner -> device).
        let preferredDeviceId = null;
        try {
          preferredDeviceId = localStorage.getItem("jarvis_device_id") || null;
        } catch {}

        const isAbort = (err) => {
          const name = (err?.name || "").toString();
          const msg = (err?.message || "").toString().toLowerCase();
          return name === "AbortError" || msg.includes("aborted") || msg.includes("abort");
        };

        let cfg = null;
        try {
          cfg = await getAgentConfig(sessionId, preferredDeviceId, 6000);
        } catch (e) {
          // If cached device_id is stale/invalid, clear it and retry once without it.
          if (e?.status === 400 && preferredDeviceId) {
            try { localStorage.removeItem("jarvis_device_id"); } catch {}
            cfg = await getAgentConfig(sessionId, null, 6000);
          } else if (e?.status === 409) {
            // Common first-run case: no device assigned yet.
            // For bootstrap, retry against the default device id (matches backend default).
            cfg = await getAgentConfig(sessionId, "primary", 6000);
          } else if (isAbort(e)) {
            // Slow startup / cold backend. Retry once with a longer timeout.
            cfg = await getAgentConfig(sessionId, preferredDeviceId, 15000);
          } else {
            throw e;
          }
        }
        if (cancelled) return;

        const nextToken = (cfg?.agent_token || "").toString();
        const nextSecret = (cfg?.agent_shared_secret || "").toString();
        const nextServerUrl = (cfg?.server_url || "").toString();
        const nextWsUrl = (cfg?.ws_url || "").toString();
        setAgentToken(nextToken);
        setAgentSharedSecret(nextSecret);
        setAgentServerUrl(nextServerUrl);
        setAgentWsUrl(nextWsUrl);
        setAgentCfgLoaded(true);

        try {
          const did = (cfg?.device_id || "").toString().trim();
          if (did) localStorage.setItem("jarvis_device_id", did);
        } catch {}
        try {
          sessionStorage.setItem(
            `jarvis_agent_cfg_${sessionId}`,
            JSON.stringify({
              agent_token: nextToken,
              agent_shared_secret: nextSecret,
              server_url: nextServerUrl,
              ws_url: nextWsUrl,
            })
          );
        } catch {}
      } catch (e) {
        if (cancelled) return;
        // Keep last known values to avoid UI blanking on slow networks.
        const detailMsg = (typeof e?.detail === "string")
          ? e.detail
          : (e?.detail?.message || null);
        const msg = detailMsg || e?.message || "Failed to load PC agent config";
        setAgentCfgError(msg);

        // Keep last known URLs if any.

        // One-time, helpful log for common misconfig.
        if (e?.status === 409) {
          addLog("system", "PC agent config not ready. Click Connect PC Agent (and make sure JarvisPCAgent.exe (or python pc_agent.py) is running).");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, sessionId, addLog]);

  const refreshTasksNow = useCallback(async () => {
    if (!isAuthenticated) return;
    try {
      const res = await getTasks(sessionId);
      setTasks(Array.isArray(res?.tasks) ? res.tasks : []);
    } catch {
      setTasks([]);
    }
  }, [isAuthenticated, sessionId]);

  useEffect(() => {
    try {
      localStorage.setItem("jarvis_active_display", activeDisplay);
    } catch {}
  }, [activeDisplay]);

  useEffect(() => {
    if (!isAuthenticated) return;
    let cancelled = false;

    const load = async () => {
      try {
        const res = await getTasks(sessionId);
        if (cancelled) return;
        setTasks(Array.isArray(res?.tasks) ? res.tasks : []);
      } catch {
        if (cancelled) return;
        setTasks([]);
      }
    };

    load();
    const id = setInterval(load, 4500);
    return () => {
      cancelled = true;
      try { clearInterval(id); } catch {}
    };
  }, [isAuthenticated, sessionId, refreshTasksNow]);

  useEffect(() => {
    if (!(isAuthenticated && role === "admin" && sessionId && showUpdateConsole)) return;
    let cancelled = false;
    (async () => {
      try {
        await getAdminUpdateHistory(sessionId, 1, 12000);
      } catch (e) {
        if (cancelled) return;
        const status = Number(e?.status || 0);
        if (status !== 401 && status !== 403) {
          addLog("system", "Admin update console API is currently unavailable.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, role, sessionId, showUpdateConsole, addLog]);

  const startWakeSessionWindow = useCallback(() => {
    const until = Date.now() + WAKE_SESSION_MS;
    wakeSessionUntilRef.current = until;

    try {
      if (wakeSessionTimerRef.current) clearTimeout(wakeSessionTimerRef.current);
    } catch {}

    wakeSessionTimerRef.current = setTimeout(() => {
      wakeSessionUntilRef.current = 0;
      addLog("system", "Wake session expired. Say the wake word again.");
    }, WAKE_SESSION_MS + 250);
  }, [WAKE_SESSION_MS, addLog]);

  const endWakeSessionWindow = useCallback(() => {
    wakeSessionUntilRef.current = 0;
    try {
      if (wakeSessionTimerRef.current) clearTimeout(wakeSessionTimerRef.current);
    } catch {}
    wakeSessionTimerRef.current = null;
  }, []);

  // No "Enable Voice" button: voice is always on.
  // On mobile we unlock mic/STT on first user gesture.

  useEffect(() => {
    assistantNameRef.current = assistantName || "Jarvis";
  }, [assistantName]);

  const isPcAgentOnline = useCallback(async (sid) => {
    if (!sid) return false;
    try {
      const r = await fetch(`${API_URL}/api/device/status?session_id=${encodeURIComponent(sid)}`);
      const data = await r.json().catch(() => null);
      const agents = Array.isArray(data?.agents) ? data.agents : [];
      return agents.length > 0;
    } catch {
      return false;
    }
  }, []);

  const connectPcAgent = useCallback(async () => {
    const sid = sessionId;
    if (!sid) {
      addLog("system", "Login required to connect PC agent.");
      return;
    }

    try {
      addLog("system", "Configuring PC agent route…");
      let preferredDeviceId = null;
      try {
        preferredDeviceId = localStorage.getItem("jarvis_device_id") || null;
      } catch {}

      let resp = null;
      try {
        resp = await configureMyPc(sid, preferredDeviceId);
      } catch (cfgErr) {
        const cfgStatus = Number(cfgErr?.status || 0);
        const detailText = String(cfgErr?.detail?.message || cfgErr?.detail || cfgErr?.message || "").toLowerCase();
        const staleOwnedDevice =
          !!preferredDeviceId
          && cfgStatus === 403
          && (
            detailText.includes("this device_id is assigned to another user")
            || detailText.includes("device_id is assigned to another user")
            || detailText.includes("already assigned to another user")
          );

        if (!staleOwnedDevice) {
          throw cfgErr;
        }

        addLog("system", "Saved PC route is no longer yours. Rebinding to your available PC…");
        try {
          localStorage.removeItem("jarvis_device_id");
        } catch {}
        resp = await configureMyPc(sid, null);
      }
      const did = (resp?.device_id || "").toString().trim();
      try {
        if (did) localStorage.setItem("jarvis_device_id", did);
      } catch {}

      // Refresh agent config immediately so user sees latest token/urls.
      try {
        const cfg = await getAgentConfig(sid, did || null, 12000);
        const nextToken = (cfg?.agent_token || "").toString();
        const nextSecret = (cfg?.agent_shared_secret || "").toString();
        const nextServerUrl = (cfg?.server_url || "").toString();
        const nextWsUrl = (cfg?.ws_url || "").toString();
        setAgentToken(nextToken);
        setAgentSharedSecret(nextSecret);
        setAgentServerUrl(nextServerUrl);
        setAgentWsUrl(nextWsUrl);
        setAgentCfgLoaded(true);
        setAgentCfgError(null);
      } catch {
        // Non-fatal for connection flow; keep existing values.
      }

      addLog("system", "Route configured. Checking agent presence…");
      let online = false;
      for (let i = 0; i < 8; i += 1) {
        // eslint-disable-next-line no-await-in-loop
        online = await isPcAgentOnline(sid);
        if (online) break;
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => setTimeout(resolve, 900));
      }

      if (online) {
        addLog("system", "PC agent connected.");
      } else {
        addLog("system", "PC route is configured, but agent is still offline. Start JarvisPCAgent.exe (or python pc_agent.py). It will auto-connect.");
      }
    } catch (e) {
      const status = Number(e?.status || 0);
      const detailText = String(e?.detail?.message || e?.detail || e?.message || "").toLowerCase();
      if (status === 401) {
        addLog("system", "Session expired while connecting PC agent. Please login again.");
        localStorage.removeItem("jarvis_session");
        localStorage.removeItem("jarvis_username");
        localStorage.removeItem("jarvis_role");
        localStorage.removeItem("jarvis_permissions");
        setIsAuthenticated(false);
        setSessionId(null);
        setUsername(null);
        setRole(null);
        setPermissions(null);
        setShowAuthModal(true);
      } else if (status === 403) {
        if (detailText.includes("all connected pcs are already assigned to other users")) {
          addLog("system", "Cannot connect this account: all currently connected PCs are assigned to other users. Re-login with the PC owner account or ask an admin to reassign the device.");
        } else if (
          detailText.includes("this device_id is assigned to another user")
          || detailText.includes("device_id is assigned to another user")
          || detailText.includes("already assigned to another user")
        ) {
          addLog("system", "This saved PC route belongs to another user. Clear/reconfigure device ownership (or use the correct owner account) and try Connect PC Agent again.");
        } else {
          addLog("system", `Permission denied while connecting PC agent. ${String(e?.detail?.message || e?.detail || "")}`.trim());
        }
      } else if (status === 409) {
        if (detailText.includes("no pc agent is connected")) {
          addLog("system", "No PC agent is currently connected. Start JarvisPCAgent.exe (or python pc_agent.py) on your PC first, then click Connect PC Agent.");
        } else {
          addLog("system", `PC route conflict: ${String(e?.detail?.message || e?.detail || "Please pick the correct device and retry.")}`);
        }
      } else {
        addLog("system", "Could not establish connection. Try again.");
      }
    }
  }, [sessionId, addLog, isPcAgentOnline]);

  useEffect(() => {
    if (!isAuthenticated || !sessionId) {
      setSystemInfo(null);
      return;
    }

    let cancelled = false;

    const applySystemInfo = (info) => {
      if (cancelled) return;
      if (info && typeof info === "object") {
        setSystemInfo(info);
        try {
          sessionStorage.setItem(`jarvis_system_info_${sessionId}`, JSON.stringify(info));
        } catch {}
      }
    };

    const tryDeviceStatus = async () => {
      try {
        const status = await getDeviceStatus(sessionId, 2500);
        const fullHealth = status?.system_health;
        if (fullHealth && typeof fullHealth === "object") {
          setSystemHealth(fullHealth);
        }
        setAgentOffline(!!status?.agent_offline);
        const agents = Array.isArray(status?.agents) ? status.agents : [];
        const sys = agents[0]?.capabilities?.system_info || null;
        if (sys && typeof sys === "object") {
          applySystemInfo({ status: "success", ...sys });
          return true;
        }
        return false;
      } catch {
        // ignore fallback errors
        return false;
      }
    };

    // Fast-path: hydrate from cache immediately.
    try {
      const raw = sessionStorage.getItem(`jarvis_system_info_${sessionId}`);
      const cached = raw ? JSON.parse(raw) : null;
      if (cached && typeof cached === "object") setSystemInfo(cached);
    } catch {}

    const poll = async () => {
      try {
        // Prefer agent capability snapshot first (avoids delegated system_info jobs in cloud).
        const fromDevice = await tryDeviceStatus();
        if (fromDevice) return;

        // Fallback path for local mode when no agent snapshot is available.
        const info = await getSystemInfo(sessionId, 2500);
        if (info && info.status === "success") {
          applySystemInfo(info);
        }
      } catch {
        // Preserve last known values and try device fallback.
        // Cloud mode may return delegated/queued lifecycle states while waiting for PC-agent results.
        await tryDeviceStatus();
      }
    };

    poll();
    const t = setInterval(poll, 4000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [isAuthenticated, sessionId]);

  const normalizeWake = useCallback((s) => {
    const raw = String(s || "")
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .replace(/\s+/g, " ")
      .trim();
    if (!raw) return "";

    // Trim common ASR filler/noise so wake matching is deterministic.
    return raw
      .replace(/^(um+|uh+|hmm+|erm+|ah+)\s+/g, "")
      .replace(/\s+(please|pls|okay|ok)+$/g, "")
      .trim();
  }, []);

  const getWakeCommandRemainder = useCallback((rawTranscript, assistantName) => {
    const t = normalizeWake(rawTranscript);
    if (!t) return null;

    const nm = normalizeWake(assistantName || "jarvis");
    if (!nm) return null;

    const wakePrefixes = [
      nm,
      `hey ${nm}`,
      `hi ${nm}`,
      `ok ${nm}`,
      `okay ${nm}`,
    ];
    for (const prefix of wakePrefixes) {
      if (t === prefix) return "";
      if (t.startsWith(`${prefix} `)) return t.slice(prefix.length).trim();
    }
    return null;
  }, [normalizeWake]);

  const isWakePhrase = useCallback((rawTranscript, assistantName) => {
    return getWakeCommandRemainder(rawTranscript, assistantName) !== null;
  }, [getWakeCommandRemainder]);

  useEffect(() => {
    if (!isAuthenticated || !sessionId) return;
    let cancelled = false;

    const syncAssistantName = async () => {
      try {
        const r = await fetch(`${API_URL}/api/validate-session`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId })
        });
        const data = await r.json().catch(() => null);
        if (cancelled || !data?.valid) return;
        const nameFromApi = (data?.user?.assistant_name || "").toString().trim();
        if (nameFromApi && nameFromApi !== assistantNameRef.current) {
          setAssistantName(nameFromApi);
          try { localStorage.setItem("jarvis_assistant_name", nameFromApi); } catch {}
          addLog("system", `Wake name synced: ${nameFromApi}`);
        }
      } catch {
        // keep last known assistant name
      }
    };

    syncAssistantName();
    const id = setInterval(syncAssistantName, 60000);
    return () => {
      cancelled = true;
      try { clearInterval(id); } catch {}
    };
  }, [isAuthenticated, sessionId, addLog]);

  // Unlock voice on mobile with a first user gesture (no explicit button).
  useEffect(() => {
    if (!isAuthenticated) return;
    if (!isMobile) {
      if (!voiceUnlocked) setVoiceUnlocked(true);
      return;
    }
    if (voiceUnlocked) return;

    let done = false;
    const onFirstTap = async () => {
      if (done) return;
      done = true;
      try {
        // User gesture: request mic permission + warm up STT.
        await initAudioProcessing();
        await primeSpeechRecognition(voiceLang);
        setVoiceUnlocked(true);
        setVizAudioUnlocked(true);
        addLog("system", "Voice ready.");
      } catch {
        done = false;
        addLog("system", "Microphone permission is required for voice.");
        try { speak("Please allow microphone permission."); } catch {}
      }
    };

    addLog("system", "Tap anywhere once to enable voice.");
    window.addEventListener("pointerdown", onFirstTap, { once: true });
    return () => {
      try { window.removeEventListener("pointerdown", onFirstTap); } catch {}
    };
  }, [addLog, isAuthenticated, isMobile, voiceLang, voiceUnlocked]);

  // Desktop: delay mic/analyser init until a real user gesture to reduce post-login jank
  // and avoid conflicts with SpeechRecognition startup.
  useEffect(() => {
    if (!isAuthenticated) return;
    if (isMobile) return;
    if (vizAudioUnlocked) return;

    let done = false;
    const onFirstGesture = () => {
      if (done) return;
      done = true;
      setVizAudioUnlocked(true);
    };

    window.addEventListener("pointerdown", onFirstGesture, { once: true });
    return () => {
      try { window.removeEventListener("pointerdown", onFirstGesture); } catch {}
    };
  }, [isAuthenticated, isMobile, vizAudioUnlocked]);

  // NOTE: The PC agent is started manually by the user (python pc_agent.py).
  // The web UI intentionally does not attempt to launch local processes.

  // ---------- Audio processing (throttled) ----------
  useEffect(() => {
    if (!isAuthenticated) return;
    if (!vizAudioUnlocked) return;
    let rafId = null;
    let audioData = null;
    let lastUiUpdate = 0;
    let idleHandle = null;
    let cancelled = false;
    const THROTTLE_MS = 50; // 20fps updates (keeps UI smooth)

    const init = async () => {
      try {
        audioData = await initAudioProcessing(); // returns { analyser, stream } if implemented
        if (audioData && audioData.analyser) {
          micStreamRef.current = audioData.stream;
          const analyser = audioData.analyser;
          const dataArray = new Float32Array(analyser.fftSize);

          const tick = () => {
            if (cancelled) return;
            analyser.getFloatTimeDomainData(dataArray);
            // compute light RMS on small slice to keep it cheap
            let sum = 0;
            // sample a subset for speed
            for (let i = 0; i < dataArray.length; i += Math.max(1, Math.floor(dataArray.length / 128))) {
              const v = dataArray[i];
              sum += v * v;
            }
            const rms = Math.sqrt(sum / Math.max(1, Math.floor(dataArray.length / 128)));
            const now = performance.now();

            // Throttle React state updates to avoid 60fps rerenders.
            if (now - lastUiUpdate > THROTTLE_MS) {
              lastUiUpdate = now;
              setVolume(prev => {
                const next = Math.min(1, prev * 0.8 + rms * 0.2);
                return Math.abs(next - prev) < 0.01 ? prev : next;
              });
            }

            rafId = requestAnimationFrame(tick);
          };
          tick();
        } else {
          addLog("system", "Audio processing init returned no analyser.");
        }
      } catch (err) {
        console.warn("Audio init failed (falling back):", err);
      }
    };

    // Defer audio permission + analyzer setup until idle.
    // This reduces post-auth jank and keeps initial interactions snappy.
    idleHandle = scheduleIdle(() => {
      if (!cancelled && document.visibilityState === "visible") init();
    }, 450);

    return () => {
      cancelled = true;
      if (rafId) cancelAnimationFrame(rafId);
      try { micStreamRef.current?.getTracks().forEach(t => t.stop()); } catch {}
      try { setVolume(0); } catch {}
      try {
        if (idleHandle && typeof window.cancelIdleCallback === "function") window.cancelIdleCallback(idleHandle);
        else if (idleHandle) clearTimeout(idleHandle);
      } catch {}
    };
  }, [addLog, isAuthenticated, isMobile, voiceUnlocked, vizAudioUnlocked]);

  // Prime SpeechRecognition after auth so first command starts faster.
  useEffect(() => {
    if (!isAuthenticated) return;
    if (isMobile && !voiceUnlocked) return;
    let cancelled = false;
    const h = scheduleIdle(async () => {
      if (cancelled) return;
      try {
        await primeSpeechRecognition(voiceLang);
      } catch {}
    }, 650);

    return () => {
      cancelled = true;
      try {
        if (h && typeof window.cancelIdleCallback === "function") window.cancelIdleCallback(h);
        else if (h) clearTimeout(h);
      } catch {}
    };
  }, [isAuthenticated, isMobile, voiceLang, voiceUnlocked]);

  // ----- handleVoiceCommand (stable reference) -----
  const handleVoiceCommand = useCallback(async () => {
    if (isHandlingCommand.current) return;
    isHandlingCommand.current = true;
    setListening(true);
    addLog("system", voiceBiometricsActive ? "Verifying voice..." : "Capturing command...");

    // stop wake recognizer to avoid overlap
    try { wakeRecognizer.current?.stop(); } catch {}

    const webSpeechSupported =
      typeof window !== "undefined" &&
      ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

    const tryGoogleFallback = async () => {
      try {
        const now = Date.now();
        if (googleSttDisabledUntilRef.current && now < googleSttDisabledUntilRef.current) {
          return null;
        }
        const { audio_b64, sample_rate_hz } = await recordPcm16Once({
          sampleRateHz: 16000,
          maxMs: isIOS ? 7500 : 6000,
          silenceStopMs: isIOS ? 1100 : 900,
          startRms: 0.012,
          silenceRms: 0.009,
        });
        if (!audio_b64) return null;

        // When voice biometrics is enabled, require server-side verify+STT.
        if (voiceBiometricsActive) {
          const resp = await secureVoiceToText(sessionId, audio_b64, voiceLang, sample_rate_hz);
          return (resp?.text || "").toString().trim() || null;
        }

        const resp = await googleSpeechToText(sessionId, audio_b64, voiceLang, sample_rate_hz);
        return (resp?.text || "").toString().trim() || null;
      } catch (e) {
        const msg = (e?.message || String(e) || "").toLowerCase();
        // If backend returns 501 (not configured/unsupported), disable Google STT for a while.
        if (msg.includes("status: 501") || msg.includes("http 501") || msg.includes("not implemented")) {
          googleSttDisabledUntilRef.current = Date.now() + 5 * 60 * 1000;
        } else if (msg.includes("status: 502") || msg.includes("http 502")) {
          // Temporary upstream issue; cool down briefly to avoid repeated slow fallbacks.
          googleSttDisabledUntilRef.current = Date.now() + 60 * 1000;
        }
        return null;
      }
    };

    const inWakeSession = Date.now() < (wakeSessionUntilRef.current || 0);
    let transcript = pendingTranscriptRef.current;
    pendingTranscriptRef.current = null;
    if (typeof transcript === "string") transcript = transcript.trim();
    if (!transcript) transcript = null;

    if (webSpeechSupported && !transcript) {
      try {
        // In biometrics mode, we require the secure server audio path so that speaker identity is verified.
        // WebSpeech doesn't provide raw audio to the backend, so we skip it.
        if (!voiceBiometricsActive) {
          // During wake-session, allow more time for the user to speak naturally.
          transcript = await listenOnce({
            timeout: inWakeSession ? (isMobile ? 20000 : 16000) : (isMobile ? 12000 : 9000),
            silenceTimeoutMs: isIOS ? 1500 : 1100,
            interim: false,
            language: voiceLang,
            maxAlternatives: 1
          });
        }
      } catch (err) {
        console.warn("listenOnce failed:", err);
      }
    }

    if (!transcript && googleSttEnabled) {
      transcript = await tryGoogleFallback();
    }

    setListening(false);

    if (!transcript) {
      addLog("error", "No command received.");
      if (voiceBiometricsEnabled && !voiceBiometricsActive) {
        addLog("system", "Voice biometrics is enrolled, but secure voice transcription is disabled in the UI config.");
        addLog("system", "Enable it by setting REACT_APP_GOOGLE_SPEECH_ENABLED=true (and configure Google STT on the server). Or disable biometrics enrollment.");
      } else if (!webSpeechSupported && !googleSttEnabled) {
        addLog("system", "Speech-to-text is unavailable on this device.");
      }
      try { wakeRecognizer.current?.start(); } catch {}
      isHandlingCommand.current = false;
      return;
    }

    // Only start/extend wake-session after we successfully captured a verified command.
    // This prevents a non-owner speaker from "waking" the UI.
    if (voiceBiometricsActive) {
      startWakeSessionWindow();
    }

    const wakeRemainder = getWakeCommandRemainder(transcript, assistantNameRef.current || "Jarvis");
    if (wakeRemainder !== null) {
      startWakeSessionWindow();
      setWakePulse(true);
      setTimeout(() => setWakePulse(false), 900);
      if (!wakeRemainder) {
        addLog("system", "Wake word detected. Listening.");
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
        return;
      }
      transcript = wakeRemainder;
    }

    addLog("input", transcript);
    const textLower = transcript.toLowerCase();

    const pendingPrompt = permissionPromptRef.current;
    if (pendingPrompt) {
      const isApprove = /\b(yes|yeah|yep|continue|proceed|allow|grant|approve|ok|okay|do it)\b/i.test(textLower);
      const isDeny = /\b(no|nope|not now|later|cancel|stop|deny|don't continue|do not continue)\b/i.test(textLower);

      if (isApprove || isDeny) {
        try {
          await resolvePermissionPromptDecision(pendingPrompt, isApprove && !isDeny);
        } catch (err) {
          addLog("system", `Permission decision failed: ${err?.message || err}`);
        }
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
        return;
      }
    }

    // Cancel latest research task
    if (/\b(cancel|stop)\s+(research|search)\b/i.test(textLower)) {
      const taskId = latestResearchTaskIdRef.current;
      if (!taskId) {
        addLog("system", "No recent research task to cancel.");
        try { speak("I don't have a recent research task to cancel."); } catch {}
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
        return;
      }

      try {
        await stopTask(sessionId, taskId);
        addLog("system", `Cancel requested for research task ${taskId}.`);
        try { speak("Okay. Cancelling the research."); } catch {}
      } catch (e) {
        addLog("error", e?.message || String(e));
        try { speak("I couldn't cancel that task."); } catch {}
      }

      try { wakeRecognizer.current?.start(); } catch {}
      isHandlingCommand.current = false;
      return;
    }

    // End the wake-session early.
    if (/^(sleep|go to sleep|stop listening|stop|cancel|goodbye|bye|thanks\s+jarvis\s+stop)$/i.test(transcript.trim())) {
      endWakeSessionWindow();
      addLog("system", "Going idle. Say the wake word to continue.");
      speak("Okay. Say the wake word when you need me.", () => {
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }

    // Voice command: rename assistant
    // Examples:
    // - "rename assistant to Friday"
    // - "set assistant name to Jarvis"
    // - "call you Nova"
    const renamePatterns = [
      /^rename assistant to (.+)$/i,
      /^set assistant name to (.+)$/i,
      /^change assistant name to (.+)$/i,
      /^call you (.+)$/i,
    ];
    let renameTo = null;
    for (const p of renamePatterns) {
      const m = transcript.match(p);
      if (m?.[1]) {
        renameTo = m[1].trim();
        break;
      }
    }
    if (renameTo) {
      if (!sessionId) {
        addLog("error", "Renaming requires login.");
        speak("Please login first so I can update my name.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }

      try {
        const resp = await setAssistantNameApi(renameTo, sessionId);
        const updated = (resp?.user?.assistant_name || renameTo || "Jarvis").toString().trim();
        setAssistantName(updated || "Jarvis");
        localStorage.setItem("jarvis_assistant_name", updated || "Jarvis");
        addLog("system", `Assistant name updated to: ${updated || "Jarvis"}`);
        speak(`Okay. Call me ${updated || "Jarvis"}.`, () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        addLog("error", err?.message || String(err));
        speak("Sorry, I couldn't update my name.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    // Preferences/habits (per-user)
    // Set examples:
    // - "set preference language to English"
    // - "remember my habit wake_time is 6 am"
    // Read examples:
    // - "show my preferences"
    // - "what are my habits"
    const showPrefs = /^(show|read|list)\s+(my\s+)?(preferences|preference|habits?|settings)$/i.test(transcript.trim()) ||
      /^(what are|what's|what is)\s+(my\s+)?(preferences|habits?)\??$/i.test(transcript.trim());

    if (showPrefs) {
      if (!sessionId) {
        addLog("error", "Preferences require login.");
        speak("Please login first so I can access your preferences.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }
      try {
        const resp = await getUserPreferences(sessionId);
        const prefs = resp?.preferences || {};
        const keys = Object.keys(prefs || {});
        addLog("system", `Preferences loaded (${keys.length})`);
        if (!keys.length) {
          speak("You have no saved preferences yet.", () => {
            try { wakeRecognizer.current?.start(); } catch {}
            isHandlingCommand.current = false;
          });
          return;
        }
        // Speak a short summary to avoid very long TTS
        const preview = keys.slice(0, 3).map(k => `${k} ${String(prefs[k])}`).join(", ");
        speak(`You have ${keys.length} preferences. For example: ${preview}.`, () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        addLog("error", err?.message || String(err));
        speak("Sorry, I couldn't read your preferences.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    const setPrefMatch = transcript.match(/^(?:set|remember|save)\s+(?:my\s+)?(?:preference|preferences|habit|setting)s?\s*(?:for\s+)?(.+?)\s+(?:to|is|as)\s+(.+)$/i);
    if (setPrefMatch) {
      if (!sessionId) {
        addLog("error", "Preferences require login.");
        speak("Please login first so I can save your preferences.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }
      const rawKey = (setPrefMatch[1] || "").trim();
      const rawValue = (setPrefMatch[2] || "").trim();
      const key = rawKey.toLowerCase().replace(/\s+/g, "_").replace(/[^a-z0-9_]/g, "").slice(0, 64);
      const value = rawValue.slice(0, 500);
      if (!key || !value) {
        addLog("error", "Invalid preference format.");
        speak("Say: set preference X to Y.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }
      try {
        await setUserPreferences({ [key]: value }, sessionId, "merge");
        addLog("system", `Preference saved: ${key} = ${value}`);
        if (key === "language" || key === "language_code") {
          setPreferredLanguage(value);
          localStorage.setItem("jarvis_language", value);
        }
        speak(`Saved. Your ${rawKey} is set.`, () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        addLog("error", err?.message || String(err));
        speak("Sorry, I couldn't save that preference.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    // Device binding (per-user PC)
    // Examples:
    // - "set my device id to primary"
    // - "use device laptop"
    // - "what is my device id"
    const showDevice = /^(what is|show|get)\s+(my\s+)?device(\s+id)?\??$/i.test(transcript.trim());
    if (showDevice) {
      if (!sessionId) {
        addLog("error", "Device lookup requires login.");
        speak("Please login first.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }
      try {
        const resp = await getUserDevice(sessionId);
        const did = resp?.device_id;
        addLog("system", `Device id: ${did || "(not set)"}`);
        speak(did ? `Your device id is ${did}.` : "You have no device assigned yet.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        addLog("error", err?.message || String(err));
        speak("Sorry, I couldn't read your device id.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    const setDeviceMatch = transcript.match(/^(?:set\s+my\s+device\s+id\s+to|set\s+device\s+id\s+to|use\s+device)\s+([a-z0-9_-]{3,32})$/i);
    if (setDeviceMatch) {
      if (!sessionId) {
        addLog("error", "Device assignment requires login.");
        speak("Please login first.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }
      const did = (setDeviceMatch[1] || "").trim();
      try {
        await setUserDevice(did, sessionId);
        addLog("system", `Device assigned: ${did}`);
        speak(`Okay. I'll use device ${did}.`, () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        addLog("error", err?.message || String(err));
        speak("Sorry, I couldn't assign that device.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    // Single-command PC setup
    // Examples:
    // - "configure my pc"
    // - "configure my pc primary"
    const configureMatch = transcript.match(/^configure\s+(?:my\s+)?pc(?:\s+([a-z0-9_-]{3,32}))?$/i);
    if (configureMatch) {
      if (!sessionId) {
        addLog("error", "PC configuration requires login.");
        speak("Please login first so I can configure your PC.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }

      const did = (configureMatch[1] || "").trim() || null;
      try {
        const resp = await configureMyPc(sessionId, did);
        const msg = resp?.message || (resp?.device_id ? `Configured your PC as device ${resp.device_id}.` : "Configured your PC.");
        addLog("system", msg);
        speak(msg, () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        const rawMsg = err?.message || String(err);
        let spoken = "Sorry, I couldn't configure your PC.";
        try {
          const parts = rawMsg.split(" - ");
          const maybeJson = parts.length >= 2 ? parts.slice(1).join(" - ") : null;
          const parsed = maybeJson ? JSON.parse(maybeJson) : null;
          const detail = parsed?.detail;
          if (typeof detail === "string") {
            spoken = detail;
          } else if (detail?.available_device_ids?.length) {
            spoken = `Multiple PCs are connected. Say: configure my PC ${detail.available_device_ids[0]}.`;
            addLog("system", `Available devices: ${detail.available_device_ids.join(", ")}`);
          } else if (detail?.message) {
            spoken = detail.message;
          }
        } catch {}
        addLog("error", rawMsg);
        speak(spoken, () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    // Voice learning shortcut:
    // "learn: when I say X, respond with Y"
    // Stores a prompt/completion example for later retrieval (RAG-lite), scoped to your account.
    const learnMatch = transcript.match(/\blearn\b\s*:?[\s\n]*when\s+i\s+say\s+(.+?)\s*,?\s*(?:respond\s+with|reply\s+with|say)\s+(.+)$/i);
    if (learnMatch) {
      const learnedPrompt = (learnMatch[1] || "").trim();
      const learnedCompletion = (learnMatch[2] || "").trim();

      if (!sessionId) {
        addLog("error", "Learning requires login.");
        speak("Please login first so I can save what you teach me.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }

      if (!learnedPrompt || !learnedCompletion) {
        addLog("error", "Invalid learning format.");
        speak("Say: learn, when I say X, respond with Y.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
        return;
      }

      addLog("system", `Learning saved: "${learnedPrompt}" → "${learnedCompletion}"`);
      try {
        await addLearningExample(learnedPrompt, learnedCompletion, sessionId, ["voice", "user_learn"]);
        setSpeaking(true);
        speak("Learned. I will use that as guidance next time.", () => {
          setSpeaking(false);
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      } catch (err) {
        addLog("error", err?.message || String(err));
        speak("I could not save that learning example.", () => {
          try { wakeRecognizer.current?.start(); } catch {}
          isHandlingCommand.current = false;
        });
      }
      return;
    }

    // quick local commands (purely visual)
    if (/twist|vortex|snake/i.test(textLower)) {
      // no background transform state (React/CSS atomic background)
      addLog("action", "Reactor twisting.");
      speak("Twisting reactor geometry.", () => {
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/expand|bigger|open up/i.test(textLower)) {
      // no background transform state (React/CSS atomic background)
      addLog("action", "Reactor expanding.");
      speak("Expanding energy field.", () => {
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/reset|normal|stable/i.test(textLower)) {
      // no background transform state (React/CSS atomic background)
      addLog("action", "Reactor normalized.");
      speak("Reactor returning to normal state.", () => {
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }

    // send to backend
    try {
      setEmotion("analyzing");
      const res = await sendMessage(transcript, "voice", sessionId);

      const resultText = String(res?.text || res?.message || "");
      if (String(res?.status || "").toLowerCase() === "error") {
        const lowerErr = resultText.toLowerCase();
        if (/(api[_\s-]?key|oauth|token|scope|webhook|integration|not configured|missing configuration)/i.test(lowerErr)) {
          promptForRequirement({
            requestedAction: transcript,
            requirementType: "service_account_permission",
            target: "Service Integration Configuration",
            title: "Requirement required: Service integration access",
            message: resultText || "A required service credential or integration scope is missing.",
            requirement: {
              requirement_type: "service_account_permission",
              target: "Service Integration Configuration",
              target_application: "Jarvis Integrations / Environment Configuration",
              required_by: "admin",
              why: "This task requires credentials, OAuth consent, or integration scopes that are not currently configured.",
              guidance: [
                "Open Management Console -> Integrations or Server Configuration.",
                "Connect the required account or set the required API key/token.",
                "Grant required scopes for the target service.",
                "Retry and Jarvis will continue automatically.",
              ],
              resume_automatically: true,
            },
            actionMode: "retry_source_text",
            sourceText: transcript,
          });
        }
      }
      addStructuredLog("response", res.text || "No text returned.", "text", null);

      try {
        if (Array.isArray(res?.plan?.steps) && res.plan.steps.length) {
          addStructuredLog("response", "Autonomous plan generated", "plan", res.plan);
        }
        if (res?.task_graph && Array.isArray(res.task_graph?.nodes)) {
          addStructuredLog("response", "Task graph generated", "task_graph", res.task_graph);
        }
        if (res?.research_report || (res?.research && (res.research?.summary || res.research?.sources))) {
          addStructuredLog("response", "Research report ready", "research_report", res.research_report || res.research);
        }
        if (res?.code || res?.generated_code || res?.code_block) {
          addStructuredLog(
            "response",
            "Code generated by CodingAgent",
            "code_block",
            { code: String(res?.code || res?.generated_code || res?.code_block || "") }
          );
        }
      } catch {}

      // Track latest research task id for cancellation UX
      try {
        if (res?.task_id) {
          latestResearchTaskIdRef.current = String(res.task_id);
        } else if (Array.isArray(res?.task_ids) && res.task_ids.length) {
          latestResearchTaskIdRef.current = String(res.task_ids[res.task_ids.length - 1]);
        } else if (Array.isArray(res?.action_results)) {
          const lastWithTask = [...res.action_results].reverse().find(r => r && r.task_id);
          if (lastWithTask?.task_id) latestResearchTaskIdRef.current = String(lastWithTask.task_id);
        }
        const t = (res.text || "").toString();
        const m = t.match(/\(Task id:\s*(task_\d+)\)/i);
        if (m && m[1]) latestResearchTaskIdRef.current = m[1];
      } catch {}

      const tLower = (res.text || "").toLowerCase();
      if (res?.emotion) {
        setEmotion(String(res.emotion));
      } else if (/\b(error|fail|cannot|no connection|critical|danger)\b/.test(tLower)) setEmotion("critical");
      else if (/\b(open|launch|execute|run|action)\b/.test(tLower)) setEmotion("action");
      else if (/\b(analyz|thinking|processing|research|search)\b/.test(tLower)) setEmotion("analyzing");
      else setEmotion("calm");

      if (res?.language) {
        const lang = String(res.language).trim();
        if (lang) {
          setPreferredLanguage(lang);
          localStorage.setItem("jarvis_language", lang);
        }
      }

      const spokenResponse = buildDirectChatCompletionSpeech(res) || res.text || "Done.";
      setSpeaking(true);
      speak(spokenResponse, () => {
        setSpeaking(false);
        setEmotion("calm");
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });

      // run any actions returned
      if (Array.isArray(res.actions) && res.actions.length) {
        const openUrlInUi = (targetUrl) => {
          const w = window.open(targetUrl, "_blank", "noopener,noreferrer");
          if (!w) {
            addLog("system", `Popup blocked. Open this link: ${targetUrl}`);
            promptForRequirement({
              requestedAction: "open_url",
              requirementType: "browser_site_permission",
              target: "Browser Popup Permission",
              title: "Permission required: Browser popup access",
              message: "Your browser blocked opening a new tab for this action.",
              requirement: {
                requirement_type: "browser_site_permission",
                target: "Browser Popup Permission",
                target_application: "Current browser site settings",
                required_by: "user",
                why: "Jarvis needs popup permission for this site to open requested links.",
                guidance: [
                  "Open browser site settings for this page.",
                  "Allow popups and redirects for this site.",
                  "Try the action again.",
                ],
                resume_automatically: false,
              },
            });
            try {
              if (navigator.clipboard?.writeText) navigator.clipboard.writeText(targetUrl);
            } catch {}
          }
        };

        const DEVICE_ACTION_TYPES = new Set([
          "device_action",
          "open_app", "close_app", "switch_app",
          "open_url",
          "execute_command",
          "set_brightness", "adjust_brightness",
          "set_power_plan", "set_energy_saver",
          "set_volume", "adjust_volume",
          "set_mute", "toggle_mute",
          "capture_screen", "screen_navigation",
          "type_text", "press_key", "hotkey",
          "read", "list", "mkdir",
          "write", "edit", "delete", "move", "copy", "cleanup",
          "self_update", "self_add",
        ]);

        const SERVER_ACTION_TYPES = new Set(["n8n_webhook"]);

        const deviceActions = res.actions.filter(a => DEVICE_ACTION_TYPES.has((a || {}).type));
        const uiActions = res.actions.filter(a => !DEVICE_ACTION_TYPES.has((a || {}).type) && !SERVER_ACTION_TYPES.has((a || {}).type));

        // If the backend returned device actions (e.g. open_app) and we're running hosted,
        // dispatch them to the connected PC agent.
        if (deviceActions.length) {
          if (!sessionId) {
            addLog("system", "Login required to run PC actions.");
          } else {
            try {
              const dispatchRes = await dispatchDeviceActions(deviceActions, sessionId, transcript);
              const flowStatus = String(dispatchRes?.status || "").toLowerCase();
              if (flowStatus === "delegated") {
                addLog("system", `PC actions delegated (${deviceActions.length}).`);
              } else if (flowStatus === "queued_for_agent") {
                addLog("system", "PC agent offline. Task queued and will auto-resume on reconnect.");
                setPendingResume({
                  type: "device_connection",
                  createdAt: Date.now(),
                  pendingActions: deviceActions,
                  sourceText: transcript,
                });
              } else if (flowStatus === "awaiting_agent") {
                addLog("system", "No device assigned yet. Action is waiting for device setup.");
                promptForRequirement({
                  requestedAction: "device_action",
                  requirementType: "missing_user_information",
                  target: "Device Selection",
                  message: dispatchRes?.message || "Assign a device to continue.",
                  pendingActions: deviceActions,
                  sourceText: transcript,
                  actionMode: "configure_device",
                  allowLabel: "Configure and Continue",
                });
              } else if (flowStatus === "pending_permission") {
                addLog("system", dispatchRes?.message || "Permission is required before this action can execute.");
                const cap = String(dispatchRes?.required_capability || "").trim();
                const needed = cap ? { [cap]: true } : null;
                promptForRequirement({
                  requestedAction: String(dispatchRes?.action_type || "device_action"),
                  requirementType: "assistant_permission",
                  target: "PC Agent Runtime Permission",
                  message: dispatchRes?.message || "Permission is required before this action can execute.",
                  neededPermissions: needed,
                  permissionOrScope: cap || null,
                  pendingActions: deviceActions,
                  sourceText: transcript,
                  actionMode: cap ? "grant_permission" : "ack",
                  allowLabel: cap ? "Grant and Continue" : "I Fixed It",
                });
              } else {
                addLog("system", `PC action status: ${flowStatus || "unknown"}`);
              }
            } catch (e) {
              const raw = e?.message || String(e);

              // Prefer structured error detail if api.js attached it.
              const structuredDetail = e?.detail;

              // Fallback: parse FastAPI JSON error response embedded in the message.
              // api.js throws: "HTTP error! status: 403 - {\"detail\":{...}}" or "... - {\"detail\":\"...\"}"
              let parsed = null;
              if (!structuredDetail) {
                try {
                  const parts = raw.split(" - ");
                  const maybeJson = parts.length >= 2 ? parts.slice(1).join(" - ") : null;
                  parsed = maybeJson ? JSON.parse(maybeJson) : null;
                } catch {}
              }

              const detail = structuredDetail ?? parsed?.detail;
              const permDetail = (detail && typeof detail === "object") ? detail : null;
              const permMsg = (typeof detail === "string") ? detail : (permDetail?.message || null);
              const reqInfo = (permDetail && typeof permDetail.requirement === "object") ? permDetail.requirement : null;

              if (permDetail?.required_capability) {
                const needed = { [permDetail.required_capability]: true };
                promptForRequirement({
                  requestedAction: permDetail?.action_type || "device_action",
                  requirementType: "assistant_permission",
                  target: "PC Agent Runtime Permission",
                  message: permDetail.message || "This action needs permission on your PC agent.",
                  requirement: reqInfo || {
                    requirement_type: "assistant_permission",
                    target: "PC Agent Runtime Permission",
                    target_application: "Jarvis Management Console / PC Agent",
                    permission_or_scope: permDetail.required_capability,
                    required_by: "user",
                    why: "The requested device action is blocked by current PC agent permission policy.",
                    guidance: [
                      "Open Management Console -> Device Permissions.",
                      `Enable '${permDetail.required_capability}'.`,
                      "Save and apply.",
                    ],
                    resume_automatically: true,
                  },
                  permissionOrScope: permDetail.required_capability,
                  pendingActions: deviceActions,
                  sourceText: transcript,
                  neededPermissions: needed,
                  actionMode: "grant_permission",
                  allowLabel: "Grant and Continue",
                });
                try {
                  speak("Permission required. Please approve the popup.");
                } catch {}
              } else if (permDetail?.message === "Device agent is not connected" || /device agent is not connected/i.test(String(permMsg || raw))) {
                addLog("system", "PC agent is not connected. Start JarvisPCAgent.exe (or python pc_agent.py) and login again (then click Yes).");
                setPendingResume({
                  type: "device_connection",
                  createdAt: Date.now(),
                  pendingActions: deviceActions,
                  sourceText: transcript,
                });
                promptForRequirement({
                  requestedAction: "device_action",
                  requirementType: "third_party_app_permission",
                  target: "PC Agent",
                  message: permMsg || "The PC agent is currently offline.",
                  requirement: reqInfo || {
                    requirement_type: "third_party_app_permission",
                    target: "PC Agent",
                    target_application: "JarvisPCAgent",
                    required_by: "user",
                    why: "The PC agent must be connected before device actions can run.",
                    guidance: [
                      "Start JarvisPCAgent on your PC.",
                      "Confirm it is connected to your server.",
                      "Keep it running; Jarvis will auto-resume this task.",
                    ],
                    resume_automatically: true,
                  },
                  pendingActions: deviceActions,
                  sourceText: transcript,
                  actionMode: "wait_for_connection",
                  allowLabel: "I Started The Agent",
                });
                try { speak("PC agent is not connected. Start it and login again."); } catch {}

                // Fallback: if the only action is open_url, attempt UI open.
                if (deviceActions.every(a => (a || {}).type === "open_url")) {
                  deviceActions.forEach((a) => {
                    const targetUrl = a?.value || a?.url;
                    if (targetUrl) openUrlInUi(targetUrl);
                  });
                }
              } else if (/no device assigned to this user/i.test(String(permMsg || raw))) {
                addLog("system", "PC is not configured. Login again and click Yes when asked about PC agent.");
                promptForRequirement({
                  requestedAction: "device_action",
                  requirementType: "missing_user_information",
                  target: "Device Selection",
                  message: permMsg || "A target device has not been assigned yet.",
                  requirement: reqInfo || {
                    requirement_type: "missing_user_information",
                    target: "Device Selection",
                    target_application: "Jarvis Management Console",
                    required_by: "user",
                    why: "Jarvis needs a selected device before it can run PC actions.",
                    guidance: [
                      "Open Management Console -> Device Setup.",
                      "Click Configure My PC.",
                      "Approve the selected device.",
                    ],
                    resume_automatically: true,
                  },
                  pendingActions: deviceActions,
                  sourceText: transcript,
                  actionMode: "configure_device",
                  allowLabel: "Configure and Continue",
                });

                if (deviceActions.every(a => (a || {}).type === "open_url")) {
                  deviceActions.forEach((a) => {
                    const targetUrl = a?.value || a?.url;
                    if (targetUrl) openUrlInUi(targetUrl);
                  });
                }
              } else {
                addLog("system", `PC action dispatch failed: ${permMsg || raw}`);
                addLog("system", "Make sure pc_agent.py is running on your PC and connected.");
                promptForRequirement({
                  requestedAction: "device_action",
                  requirementType: "missing_requirement",
                  target: "PC Agent / Integration",
                  message: permMsg || raw,
                  requirement: {
                    requirement_type: "missing_requirement",
                    target: "PC Agent / Integration",
                    target_application: "Jarvis + external integrations",
                    required_by: "user",
                    why: "One or more requirements for this action are still missing.",
                    guidance: [
                      "Check Device Status and Permissions in the Management Console.",
                      "Verify the external app/service is logged in and authorized.",
                      "Retry the task after fixing the requirement.",
                    ],
                    resume_automatically: true,
                  },
                  pendingActions: deviceActions,
                  sourceText: transcript,
                });
              }
            }
          }
        }

        // Handle UI-safe actions (like open_url) locally.
        for (const a of uiActions) {
          addLog("action", `${a.type} ${a.value || a.url || a.file_path || a.app_name || ""}`);
          if (a.type === "open_url" && (a.value || a.url)) {
            const targetUrl = a.value || a.url;
            // Always prefer a new tab. Never navigate away from the assistant UI.
            openUrlInUi(targetUrl);
          }
        }
      }
    } catch (err) {
      addLog("error", err?.message || String(err));
      setEmotion("critical");
      speak("I encountered an error contacting the server.", () => {
        setEmotion("calm");
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
    }
  }, [sessionId, addLog, addStructuredLog, buildDirectChatCompletionSpeech, isMobile, isIOS, voiceLang, endWakeSessionWindow, googleSttEnabled, voiceBiometricsEnabled, voiceBiometricsActive, startWakeSessionWindow, getWakeCommandRemainder, promptForRequirement, resolvePermissionPromptDecision]);

  const startHoldToTalk = useCallback(async () => {
    if (!isAuthenticated) {
      setShowAuthModal(true);
      return;
    }
    if (pttHolding) return;
    if (isHandlingCommand.current) return;
    if (speakingRef.current) return;

    const seq = (pttSeqRef.current || 0) + 1;
    pttSeqRef.current = seq;

    setPttHolding(true);
    addLog("system", "Voice capture started.");

    // Stop wake recognizer to avoid overlap.
    try { wakeRecognizer.current?.stop(); } catch {}

    const startWebSpeechFallback = () => {
      try {
        const ws = startWebSpeechHold({ language: voiceLang, maxMs: isMobile ? 20000 : 25000 });
        pttControllerRef.current = { kind: "webspeech", ctrl: ws, seq };
        if (!ws) {
          addLog("system", "SpeechRecognition not available for push-to-talk.");
        }
      } catch {
        pttControllerRef.current = null;
        addLog("system", "Could not start SpeechRecognition.");
      }
    };

    // Prefer the server audio path when available (best accuracy + required for biometrics).
    if (voiceBiometricsActive || googleSttEnabled) {
      try {
        const initPromise = startPcm16Recorder({
          sampleRateHz: 16000,
          maxMs: isMobile ? 20000 : 25000,
        });

        pttInitPromiseRef.current = initPromise;
        // Store a placeholder so stopHoldToTalk can await init if the user releases quickly.
        pttControllerRef.current = { kind: "pcm", ctrl: null, seq };

        const ctrl = await initPromise;
        // If the user released (or restarted) while init was in flight, stop/cancel immediately.
        if (pttSeqRef.current !== seq) {
          try { ctrl?.cancel?.(); } catch {}
          return;
        }

        pttInitPromiseRef.current = null;
        pttControllerRef.current = { kind: "pcm", ctrl, seq };
        if (!ctrl) {
          addLog("system", "Microphone is not available for push-to-talk. Falling back to browser recognition.");
          startWebSpeechFallback();
        }
        return;
      } catch {
        addLog("system", "Could not start push-to-talk recording. Falling back to browser recognition.");
        pttInitPromiseRef.current = null;
        startWebSpeechFallback();
        return;
      }
    }

    // Fallback: WebSpeech hold session.
    startWebSpeechFallback();
  }, [addLog, googleSttEnabled, isAuthenticated, isMobile, pttHolding, voiceBiometricsActive, voiceLang]);

  const stopHoldToTalk = useCallback(async () => {
    if (!pttHolding) return;
    setPttHolding(false);

    // Invalidate any in-flight recorder init.
    const stopSeq = (pttSeqRef.current || 0) + 1;
    pttSeqRef.current = stopSeq;

    const holder = pttControllerRef.current;
    pttControllerRef.current = null;
    const initPromise = pttInitPromiseRef.current;
    pttInitPromiseRef.current = null;

    try {
      if (!holder) {
        try { wakeRecognizer.current?.start(); } catch {}
        return;
      }

      // PCM path (server transcription)
      if (holder.kind === "pcm") {
        let ctrl = holder.ctrl;
        // If stop occurs before init completes, await init and stop the resulting controller.
        if (!ctrl && initPromise && typeof initPromise.then === "function") {
          try {
            ctrl = await initPromise;
          } catch {
            ctrl = null;
          }
        }
        if (!ctrl || !ctrl.stop) {
          try { wakeRecognizer.current?.start(); } catch {}
          return;
        }

        const { audio_b64, sample_rate_hz } = await ctrl.stop();
        if (!audio_b64) {
          addLog("error", "No speech captured.");
          try { wakeRecognizer.current?.start(); } catch {}
          return;
        }

        let text = null;
        try {
          if (voiceBiometricsActive) {
            const resp = await secureVoiceToText(sessionId, audio_b64, voiceLang, sample_rate_hz);
            text = (resp?.text || "").toString().trim() || null;
          } else {
            const resp = await googleSpeechToText(sessionId, audio_b64, voiceLang, sample_rate_hz);
            text = (resp?.text || "").toString().trim() || null;
          }
        } catch (e) {
          if (voiceBiometricsActive) {
            addLog("error", e?.message || String(e));
            try { wakeRecognizer.current?.start(); } catch {}
            return;
          }

          addLog("system", "Server speech recognition unavailable. Using browser recognition fallback.");
          try {
            text = await listenOnce({
              timeout: isMobile ? 12000 : 9000,
              silenceTimeoutMs: isIOS ? 1500 : 1100,
              interim: false,
              language: voiceLang,
              maxAlternatives: 1,
            });
            text = (text || "").toString().trim() || null;
          } catch {
            text = null;
          }
        }

        if (!text) {
          addLog("error", "No command received.");
          try { wakeRecognizer.current?.start(); } catch {}
          return;
        }

        // Match wake-word UX: pulse + open wake-session after a successful command.
        // In biometrics mode, we only open the wake-session after verification (handled in handleVoiceCommand).
        if (!voiceBiometricsActive) {
          startWakeSessionWindow();
          setWakePulse(true);
          setTimeout(() => setWakePulse(false), 900);
        }

        pendingTranscriptRef.current = text;
        await handleVoiceCommand();
        return;
      }

      // WebSpeech hold path
      if (holder.kind === "webspeech") {
        try { holder.ctrl.stop(); } catch {}
        const text = await holder.ctrl.promise;
        if (!text) {
          addLog("error", "No command received.");
          try { wakeRecognizer.current?.start(); } catch {}
          return;
        }

        if (!voiceBiometricsActive) {
          startWakeSessionWindow();
          setWakePulse(true);
          setTimeout(() => setWakePulse(false), 900);
        }

        pendingTranscriptRef.current = String(text).trim();
        await handleVoiceCommand();
        return;
      }
    } catch (e) {
      addLog("error", e?.message || String(e));
      try { wakeRecognizer.current?.start(); } catch {}
    }
  }, [addLog, handleVoiceCommand, isIOS, isMobile, pttHolding, sessionId, voiceBiometricsActive, voiceLang, startWakeSessionWindow]);

  // ---------- Wake-word listener (same logic but keep stable callbacks) ----------
  useEffect(() => {
    if (!isAuthenticated) return;
    if (isMobile && !voiceUnlocked) return;
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      addLog("system", "SpeechRecognition not available in this browser.");
      return;
    }

    const recognizer = new SpeechRecognition();
    recognizer.continuous = true;
    recognizer.interimResults = false;
    recognizer.lang = voiceLang || "en-US";

    let active = false;
    let startAttempts = 0;
    let restartBurstCount = 0;
    let restartBurstWindowStart = Date.now();
    let lastRestartLogAt = 0;
    let restartTimer = null;
    let watchdogTimer = null;
    let lastEventAt = Date.now();

    const scheduleStart = (ms, reason = "retry") => {
      try {
        if (restartTimer) clearTimeout(restartTimer);
      } catch {}
      restartTimer = setTimeout(() => safeStart(reason), ms);
    };

    const safeStart = (reason = "manual") => {
      if (active) return;

      const now = Date.now();
      if ((now - restartBurstWindowStart) > 60_000) {
        restartBurstWindowStart = now;
        restartBurstCount = 0;
      }
      restartBurstCount += 1;
      if (restartBurstCount > 20) {
        wakeDisabledUntilRef.current = now + 30_000;
        try { setWakeListeningOnline(false); } catch {}
        if ((now - lastRestartLogAt) > 8_000) {
          lastRestartLogAt = now;
          addLog("system", "listener restarted too frequently; pausing for recovery.");
        }
        scheduleStart(31_000, "cooldown");
        return;
      }

      const disabledUntil = Number(wakeDisabledUntilRef.current || 0);
      if (disabledUntil && now < disabledUntil) {
        const wait = Math.max(250, Math.min(disabledUntil - now, 60_000));
        scheduleStart(wait, "disabled_wait");
        return;
      }

      try {
        recognizer.start();
        active = true;
        startAttempts = 0;
        lastEventAt = Date.now();
        if ((Date.now() - lastRestartLogAt) > 5_000 && reason !== "manual") {
          lastRestartLogAt = Date.now();
          addLog("system", "listener restarted");
        }
      } catch (err) {
        startAttempts++;
        if (startAttempts < 6) {
          scheduleStart(400 + startAttempts * 200, "start_failed");
        } else {
          wakeDisabledUntilRef.current = Date.now() + 10_000;
          scheduleStart(10_500, "start_backoff");
        }
      }
    };

    recognizer.onstart = () => {
      active = true;
      try { setWakeListeningOnline(true); } catch {}
      lastEventAt = Date.now();
    };

    recognizer.onresult = (e) => {
      try {
        lastEventAt = Date.now();
        const result = e.results[e.resultIndex];
        const rawTranscript = (result[0].transcript || "").toString().trim();
        const transcript = normalizeWake(rawTranscript);
        if (!transcript) return;
        if (isHandlingCommand.current) return;
        if (speakingRef.current) return;

        const inWakeSession = Date.now() < (wakeSessionUntilRef.current || 0);

        const nm = assistantNameRef.current || "Jarvis";
        const wakeHit = isWakePhrase(rawTranscript, nm);

        if (wakeHit) {
          const now = Date.now();
          if (now - Number(lastWakeDetectedAtRef.current || 0) < 1200) {
            return;
          }
          lastWakeDetectedAtRef.current = now;
          addLog("system", "wake detected");

          // If the user said wake-word + command in one utterance,
          // execute immediately instead of waiting for another capture step.
          const trailingCommand = getWakeCommandRemainder(rawTranscript, nm);

          // In biometrics mode, defer wake-session start and UI pulse until
          // we successfully capture a verified command.
          if (!voiceBiometricsActive) {
            // Start/refresh the wake session window.
            startWakeSessionWindow();

            // vibrate UI briefly (visual) and pause auto recognition
            setWakePulse(true);
            setTimeout(() => setWakePulse(false), 900);
          }

          if (!trailingCommand) {
            addLog("system", "Wake word detected. Listening.");
            return;
          }

          pendingTranscriptRef.current = trailingCommand;

          // stop recognizer safely so we can do single-shot capture
          try { recognizer.stop(); } catch {}
          active = false;

          setTimeout(async () => await handleVoiceCommand(), 20);
          return;
        }

        // During wake-session: treat ANY speech as a command (no re-wake needed).
        if (inWakeSession && rawTranscript) {
          pendingTranscriptRef.current = rawTranscript;

          // stop recognizer safely so we can process command without overlap
          try { recognizer.stop(); } catch {}
          active = false;

          setTimeout(async () => await handleVoiceCommand(), 20);
          return;
        }
      } catch (err) {
        console.warn("Wake onresult parse err:", err);
      }
    };

    recognizer.onerror = async (ev) => {
      const errName = ev?.error || "unknown";
      lastEventAt = Date.now();
      addLog("system", `mic error: ${errName}`);

      // Some errors are not recoverable without user action (permissions / missing mic).
      // Avoid an infinite restart loop that spams logs in packaged desktop builds.
      const fatal = new Set(["not-allowed", "service-not-allowed", "audio-capture", "language-not-supported"]);
      if (fatal.has(errName)) {
        try { setWakeListeningOnline(false); } catch {}
        if (!wakePermissionHintedRef.current) {
          wakePermissionHintedRef.current = true;
          if (errName === "language-not-supported") {
            addLog("system", `Wake listener disabled: language not supported (${voiceLang || "en-US"}).`);
          } else if (errName === "audio-capture") {
            let hasAudioInput = false;
            try {
              const devices = await navigator.mediaDevices?.enumerateDevices?.();
              hasAudioInput = Array.isArray(devices) && devices.some((d) => d?.kind === "audioinput");
            } catch {
              hasAudioInput = false;
            }

            const target = "Microphone Input Device";
            const targetApp = "Operating System Sound Input Settings";
            const guidance = hasAudioInput
              ? [
                  "Close other apps that may be using the microphone (Zoom/Teams/Recorder/browser tabs).",
                  "Open OS Sound Input settings and confirm the correct microphone is selected.",
                  "Restart Jarvis after releasing the microphone device.",
                ]
              : [
                  "Connect/enable a microphone device in OS Sound settings.",
                  "Set the microphone as an active input device.",
                  "Restart Jarvis after the microphone appears in input devices.",
                ];

            addLog("system", hasAudioInput
              ? "Wake listener disabled: microphone is busy/unavailable for this app."
              : "Wake listener disabled: no microphone input device detected.");
            promptForRequirement({
              requestedAction: "voice_input",
              requirementType: "operating_system_audio_device",
              target,
              title: "Microphone input required",
              message: hasAudioInput
                ? "A microphone exists but is currently unavailable to Jarvis."
                : "No active microphone input device is available to Jarvis.",
              requirement: {
                requirement_type: "operating_system_audio_device",
                target,
                target_application: targetApp,
                required_by: "user",
                why: "Voice commands require an available microphone input device.",
                guidance,
                resume_automatically: true,
              },
              actionMode: "retry_voice",
            });
          } else {
            addLog("system", "permission missing: microphone permission denied");
            addLog(
              "system",
              "Wake listener disabled: microphone permission denied. Enable microphone access for this app/browser, then reload Jarvis."
            );
            promptForRequirement({
              requestedAction: "voice_input",
              requirementType: "browser_site_permission",
              target: "Browser Microphone Permission",
              title: "Permission required: Browser microphone access",
              message: "Microphone permission is blocked for this site.",
              requirement: {
                requirement_type: "browser_site_permission",
                target: "Browser Microphone Permission",
                target_application: "Current browser site settings",
                required_by: "user",
                why: "Voice listening cannot start unless the browser allows microphone access for this site.",
                guidance: [
                  "Click the lock icon in the address bar.",
                  "Set Microphone for this site to Allow.",
                  "Refresh the page if needed.",
                ],
                resume_automatically: true,
              },
              actionMode: "retry_voice",
            });
          }
        }
        wakeDisabledUntilRef.current = Date.now() + 10 * 60 * 1000;
        active = false;
        return;
      }

      // Reduce disruptive log spam; "no-speech"/"aborted" are common and not actionable.
      if (errName !== "no-speech" && errName !== "aborted") {
        addLog("system", `Wake listener error: ${errName}`);
      } else {
        const now = Date.now();
        // Common idle/transition events; keep them very quiet.
        if (now - lastWakeNoSpeechLogRef.current > 10 * 60 * 1000) {
          lastWakeNoSpeechLogRef.current = now;
          addLog("system", "Wake listener: idle (no speech)");
        }
      }
      active = false;
      try { setWakeListeningOnline(false); } catch {}
      // restart with backoff
      if (isAuthenticated) scheduleStart(errName === "aborted" ? 250 : 900, `error_${errName}`);
    };

    recognizer.onend = () => {
      active = false;
      try { setWakeListeningOnline(false); } catch {}
      lastEventAt = Date.now();
      if (!isHandlingCommand.current && isAuthenticated) scheduleStart(250, "onend");
    };

    // Desktop/Electron/WebView builds sometimes end up with a "stuck" recognizer
    // that stops emitting events but also doesn't reliably fire onend. Add a small
    // watchdog so the user doesn't need to refresh the UI.
    watchdogTimer = setInterval(() => {
      try {
        if (!isAuthenticated) return;
        if (isHandlingCommand.current) return;
        if (speakingRef.current) return;

        const now = Date.now();
        const quietMs = now - (lastEventAt || now);

        // If nothing has happened for a while, restart recognition.
        if (quietMs > 45_000) {
          try { recognizer.stop(); } catch {}
          active = false;
          lastEventAt = now;
          scheduleStart(1200, "watchdog_quiet");
          return;
        }

        // If we are not active, attempt a gentle restart.
        if (!active) {
          scheduleStart(1200, "watchdog_inactive");
        }
      } catch {
        // ignore
      }
    }, 12_000);

    // Start after a short delay to avoid slowing initial paint
    const startTimer = setTimeout(() => safeStart("boot"), 120);
    restartTimer = startTimer;
    wakeRecognizer.current = recognizer;
    addLog("system", "Wake-word listener started.");

    return () => {
      clearTimeout(startTimer);
      try {
        if (restartTimer) clearTimeout(restartTimer);
      } catch {}
      try {
        if (watchdogTimer) clearInterval(watchdogTimer);
      } catch {}
      try {
        setWakeListeningOnline(false);
        recognizer.onresult = null;
        recognizer.onerror = null;
        recognizer.onstart = null;
        recognizer.onend = null;
        recognizer.stop();
      } catch {}

      try {
        if (wakeSessionTimerRef.current) clearTimeout(wakeSessionTimerRef.current);
      } catch {}
    };
  }, [addLog, handleVoiceCommand, isAuthenticated, isMobile, normalizeWake, voiceLang, voiceUnlocked, startWakeSessionWindow, voiceBiometricsActive, isWakePhrase, getWakeCommandRemainder, promptForRequirement]);

  useEffect(() => {
    if (!pendingResume || !isAuthenticated || !sessionId) return;
    if (String(pendingResume?.type || "") !== "device_connection") return;

    let cancelled = false;
    const startedAt = Number(pendingResume?.createdAt || Date.now());

    const tryResume = async () => {
      if (cancelled) return;
      if ((Date.now() - startedAt) > (10 * 60 * 1000)) {
        addLog("system", "Pending task resume timed out. You can retry the original command.");
        setPendingResume(null);
        return;
      }
      try {
        const status = await getDeviceStatus(sessionId, 2500);
        const agents = Array.isArray(status?.agents) ? status.agents : [];
        if (!agents.length) return;

        if (pendingResume?.manual_notified) return;
        addLog("system", "Requirement satisfied. Pending task is ready for manual resume.");
        try { speak("Connection restored. Resume your task manually."); } catch {}
        setPendingResume((prev) => {
          if (!prev || prev.type !== "device_connection") return prev;
          return { ...prev, manual_notified: true };
        });
      } catch {
        // Keep polling silently.
      }
    };

    tryResume();
    const id = setInterval(tryResume, 5000);
    return () => {
      cancelled = true;
      try { clearInterval(id); } catch {}
    };
  }, [pendingResume, isAuthenticated, sessionId, addLog, auditRequirementEvent]);

  // Authentication helpers (unchanged, stable)
  const handleAuthSuccess = useCallback((newSessionId, newUsername, newRole, newPermissions) => {
    setSessionId(newSessionId);
    setUsername(newUsername);
    setRole(newRole || null);
    setPermissions(newPermissions || null);
    setIsAuthenticated(true);
    setShowAuthModal(false);
    localStorage.setItem("jarvis_session", newSessionId);
    localStorage.setItem("jarvis_username", newUsername);
    if (newRole) localStorage.setItem("jarvis_role", newRole);
    if (newPermissions) localStorage.setItem("jarvis_permissions", JSON.stringify(newPermissions));
    addLog("system", `Authenticated as ${newUsername}${newRole ? ` (${newRole})` : ""}`);

    // Pull latest profile (assistant_name) after auth
    fetch(`${API_URL}/api/validate-session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: newSessionId })
    })
      .then(res => res.json())
      .then(data => {
        const nameFromApi = data?.user?.assistant_name;
        if (nameFromApi && String(nameFromApi).trim()) {
          const nextName = String(nameFromApi).trim();
          setAssistantName(nextName);
          localStorage.setItem("jarvis_assistant_name", nextName);
        }

        const enrolled = !!data?.user?.voice_biometrics_enrolled;
        setVoiceBiometricsEnrolled(enrolled);
        try {
          localStorage.setItem("jarvis_voice_biometrics_enrolled", enrolled ? "1" : "0");
        } catch {}
      })
      .catch(() => {});

    // Pull preferences (language, etc.) after auth
    (async () => {
      try {
        const resp = await getUserPreferences(newSessionId);
        const prefs = resp?.preferences || {};
        const lang = (prefs.language_code || prefs.language || "").toString().trim();
        if (lang) {
          setPreferredLanguage(lang);
          localStorage.setItem("jarvis_language", lang);
        }
      } catch {}
    })();
  }, [addLog]);

  // PC agent is started manually (python pc_agent.py).

  useEffect(() => {
    const storedSession = localStorage.getItem("jarvis_session");
    const storedUsername = localStorage.getItem("jarvis_username");
    const storedRole = localStorage.getItem("jarvis_role");
    const storedPermissionsRaw = localStorage.getItem("jarvis_permissions");
    const storedAssistantName = localStorage.getItem("jarvis_assistant_name");

    // Restore cached identifiers, but do NOT mark as authenticated until the server validates the session.
    // This prevents wake-word listening and WS communication from starting for an expired/invalid session.
    if (storedSession && storedUsername) {
      setSessionId(storedSession);
      setUsername(storedUsername);
      setRole(storedRole || null);
      const nextName = (storedAssistantName || "Jarvis").toString().trim();
      setAssistantName(nextName || "Jarvis");
      try {
        setPermissions(storedPermissionsRaw ? JSON.parse(storedPermissionsRaw) : null);
      } catch {
        setPermissions(null);
      }
      // Keep the auth modal hidden while we validate the session in the background.
      setIsAuthenticated(false);
      setShowAuthModal(false);
    }

    (async () => {
      if (storedSession && storedUsername) {
        try {
          const r = await fetch(`${API_URL}/api/validate-session`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ session_id: storedSession })
          });
          const data = await r.json().catch(() => null);
          if (data?.valid) {
            setRole(data.role || storedRole || null);
            try {
              setPermissions(data.permissions || (storedPermissionsRaw ? JSON.parse(storedPermissionsRaw) : null));
            } catch {
              setPermissions(data.permissions || null);
            }
            const nameFromApi = data?.user?.assistant_name;
            const nextName = (nameFromApi || storedAssistantName || "Jarvis").toString().trim();
            setAssistantName(nextName || "Jarvis");
            localStorage.setItem("jarvis_assistant_name", nextName || "Jarvis");

            const enrolled = !!data?.user?.voice_biometrics_enrolled;
            setVoiceBiometricsEnrolled(enrolled);
            try {
              localStorage.setItem("jarvis_voice_biometrics_enrolled", enrolled ? "1" : "0");
            } catch {}
            setIsAuthenticated(true);
            setShowAuthModal(false);
            return;
          }
        } catch {
          // fallthrough
        }
      }

      // Default: show auth modal
      setIsAuthenticated(false);
      setSessionId(null);
      setUsername(null);
      setVoiceBiometricsEnrolled(false);
      try {
        localStorage.setItem("jarvis_voice_biometrics_enrolled", "0");
      } catch {}
      setRole(null);
      setPermissions(null);
      setShowAuthModal(true);
    })();
  }, []);

  useEffect(() => {
    if (!isAuthenticated || !sessionId) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await getUserPreferences(sessionId);
        if (cancelled) return;
        const prefs = resp?.preferences || {};
        const lang = (prefs.language_code || prefs.language || "").toString().trim();
        if (lang) {
          setPreferredLanguage(lang);
          localStorage.setItem("jarvis_language", lang);
        }
      } catch {}
    })();
    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, sessionId]);

  // ----------------- RENDER -----------------
  return (
    <div className={`jarvis-root${wakePulse ? " wake-pulse" : ""}`}>
      {showAuthModal && (
        <AuthModal
          onAuthSuccess={handleAuthSuccess}
          onClose={() => {
            if (!isAuthenticated) {
              speak("Authentication is required to use Jarvis.");
            } else {
              setShowAuthModal(false);
            }
          }}
        />
      )}

      <Suspense fallback={<div />}>
        {permissionPrompt && (
          <PermissionModal
            title={permissionPrompt.title}
            message={permissionPrompt.message}
            details={permissionPrompt.details}
            copyFields={permissionPrompt.copyFields}
            allowLabel={permissionPrompt.allowLabel || "Allow"}
            denyLabel={permissionPrompt.denyLabel || "Deny"}
            onDeny={() => {
              const req = permissionPrompt;
              if (!req) return;
              resolvePermissionPromptDecision(req, false).catch((err) => {
                addLog("system", `Permission decision failed: ${err?.message || err}`);
              });
            }}
            onAllow={async () => {
              const req = permissionPrompt;
              if (!req) return;
              try {
                await resolvePermissionPromptDecision(req, true);
              } catch (err) {
                addLog("system", `Permission grant failed: ${err?.message || err}`);
              }
            }}
          />
        )}
      </Suspense>

      {isAuthenticated && role === "admin" && (
        <>
          <div style={{ position: "fixed", bottom: 20, left: 20, zIndex: 16 }}>
            <button
              onClick={() => setShowUpdateConsole((prev) => !prev)}
              style={{
                background: "rgba(0,234,255,0.12)",
                border: "1px solid var(--jarvis-accent)",
                color: "var(--jarvis-accent)",
                borderRadius: 10,
                padding: "8px 12px",
                cursor: "pointer",
              }}
            >
              {showUpdateConsole ? "Hide" : "Open"} Update Console
            </button>
          </div>

          <UpdateManagementConsole
            sessionId={sessionId}
            isOpen={showUpdateConsole}
            onClose={() => setShowUpdateConsole(false)}
            onTasksChanged={refreshTasksNow}
          />
        </>
      )}


      {isAuthenticated && username && (
        <div style={{ position: "fixed", top: 20, right: 20, zIndex: 15 }}>
          <div style={{ display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 8 }}>
            {(String(systemHealth?.status || "ok").toLowerCase() !== "ok") && (
              <div style={{
                background: "rgba(255, 193, 7, 0.18)",
                border: "1px solid rgba(255, 193, 7, 0.65)",
                color: "#ffd86a",
                borderRadius: 10,
                padding: "6px 10px",
                fontSize: 12,
                fontWeight: 600,
              }}>
                System degraded
              </div>
            )}
            {agentOffline && (
              <div style={{
                background: "rgba(255, 77, 79, 0.18)",
                border: "1px solid rgba(255, 77, 79, 0.65)",
                color: "#ff9a9b",
                borderRadius: 10,
                padding: "6px 10px",
                fontSize: 12,
                fontWeight: 600,
              }}>
                Agent offline
              </div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginRight: 8 }}>
              {activeDisplay === "autonomy" ? (
                <button
                  onClick={() => setActiveDisplay("dashboard")}
                  style={{
                    background: "rgba(0,234,255,0.16)",
                    border: "1px solid var(--jarvis-accent)",
                    color: "var(--jarvis-accent)",
                    borderRadius: 999,
                    padding: "6px 10px",
                    fontSize: 12,
                    cursor: "pointer",
                  }}
                >
                  Main View
                </button>
              ) : (
                <button
                  onClick={() => setActiveDisplay("autonomy")}
                  style={{
                    background: "rgba(0,234,255,0.16)",
                    border: "1px solid var(--jarvis-accent)",
                    color: "var(--jarvis-accent)",
                    borderRadius: 999,
                    padding: "6px 10px",
                    fontSize: 12,
                    cursor: "pointer",
                  }}
                >
                  Autonomy View
                </button>
              )}
            </div>
            <div style={{ width: 8, height: 8, borderRadius: 8, background: "var(--jarvis-accent)", boxShadow: "0 0 10px var(--jarvis-accent-glow)" }} />
            <span style={{ color: "var(--jarvis-accent)", fontSize: 14 }}>
              {username}{role ? ` (${role})` : ""}
            </span>
            <span style={{ color: "var(--jarvis-accent)", fontSize: 14, opacity: 0.9 }}>
              Assistant: {assistantName || "Jarvis"}
            </span>

            <button onClick={async () => {
              const sid = sessionId;

              // Ask server to logout AND request agent stop (server-side).
              try {
                if (sid) {
                  await fetch(`${API_URL}/api/logout`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ session_id: sid })
                  });
                }
              } catch {}

              localStorage.removeItem("jarvis_session");
              localStorage.removeItem("jarvis_username");
              localStorage.removeItem("jarvis_role");
              localStorage.removeItem("jarvis_permissions");
              localStorage.removeItem("jarvis_assistant_name");
              localStorage.removeItem("jarvis_voice_biometrics_enrolled");
              setIsAuthenticated(false);
              setSessionId(null);
              setUsername(null);
              setAssistantName("Jarvis");
              setVoiceBiometricsEnrolled(false);
              setRole(null);
              setPermissions(null);
              setPermissionPrompt(null);
              try {
                if (isMobile) setVoiceUnlocked(false);
              } catch {}
              setShowAuthModal(true);
              speak("Logged out successfully.");
            }} style={{ background: "transparent", border: "none", color: "#ff5050", cursor: "pointer" }}>Logout</button>
            </div>
          </div>
        </div>
      )}

      {activeDisplay !== "autonomy" && (
        <JarvisDashboard
          isAuthenticated={isAuthenticated}
          logs={logs}
          tasks={tasks}
          emotion={emotion}
          listening={listening}
          speaking={speaking}
          wakeListeningOnline={wakeListeningOnline}
          volume={volume}
          agentToken={agentToken}
          agentSharedSecret={agentSharedSecret}
          agentServerUrl={agentServerUrl}
          agentWsUrl={agentWsUrl}
          agentCfgLoaded={agentCfgLoaded}
          agentCfgError={agentCfgError}
          onConnectPcAgent={connectPcAgent}
          systemInfo={systemInfo}
          themeColor={themeColor}
          onThemeColorChange={setThemeColor}
        />
      )}

      {isAuthenticated && activeDisplay === "autonomy" && (
        <AutonomyDashboard
          sessionId={sessionId}
          logs={logs}
          onTabChange={setAutonomyTab}
        />
      )}

      {/* Bottom stack: status above the wake prompt */}
      <div style={{ position: "fixed", bottom: 24, left: 0, right: 0, display: "flex", justifyContent: "center", zIndex: 12, pointerEvents: "none" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "center" }}>
          <div style={{ pointerEvents: "none", background: "rgba(10,10,12,0.28)", color: "var(--jarvis-accent)", padding: "8px 14px", borderRadius: 999, backdropFilter: "blur(6px)", display: "flex", alignItems: "center", gap: 10, minWidth: 220, justifyContent: "center", boxShadow: "inset 0 0 20px rgba(255,255,255,0.02)" }}>
            <div style={{ width: 10, height: 10, borderRadius: 999, background: emotion === "critical" ? "#ff4d4f" : emotion === "analyzing" ? "#ffd24d" : "#00ffc8", boxShadow: emotion === "critical" ? "0 0 10px rgba(255,77,79,0.45)" : emotion === "analyzing" ? "0 0 10px rgba(255,210,77,0.35)" : "0 0 10px rgba(0,255,200,0.45)" }} />
            <div style={{ fontFamily: "Inter, system-ui, sans-serif", fontSize: 13, letterSpacing: 0.3 }}>
              {emotion === "calm" && (speaking ? "Speaking" : (listening ? "Capturing" : (wakeListeningOnline ? "Wake listening" : "Idle")))}
              {emotion === "analyzing" && "Analyzing"}
              {emotion === "critical" && "Critical"}
            </div>
          </div>
          {!(activeDisplay === "autonomy" && String(autonomyTab || "").toLowerCase() === "anatomy") && (
            <div
              onPointerDown={(e) => {
                if (!isAuthenticated || (isMobile && !voiceUnlocked)) return;
                try { e.preventDefault(); } catch {}
                try {
                  if (typeof e.pointerId === "number" && e.currentTarget?.setPointerCapture) {
                    e.currentTarget.setPointerCapture(e.pointerId);
                  }
                } catch {}
                startHoldToTalk();
              }}
              onPointerUp={(e) => {
                if (!isAuthenticated || (isMobile && !voiceUnlocked)) return;
                try { e.preventDefault(); } catch {}
                try {
                  if (typeof e.pointerId === "number" && e.currentTarget?.releasePointerCapture) {
                    e.currentTarget.releasePointerCapture(e.pointerId);
                  }
                } catch {}
                stopHoldToTalk();
              }}
              onPointerCancel={(e) => {
                if (!isAuthenticated || (isMobile && !voiceUnlocked)) return;
                try {
                  if (typeof e.pointerId === "number" && e.currentTarget?.releasePointerCapture) {
                    e.currentTarget.releasePointerCapture(e.pointerId);
                  }
                } catch {}
                stopHoldToTalk();
              }}
              onKeyDown={(e) => {
                if (!isAuthenticated || (isMobile && !voiceUnlocked)) return;
                if (e.key === " " || e.key === "Enter") {
                  e.preventDefault();
                  startHoldToTalk();
                }
              }}
              onKeyUp={(e) => {
                if (!isAuthenticated || (isMobile && !voiceUnlocked)) return;
                if (e.key === " " || e.key === "Enter") {
                  e.preventDefault();
                  stopHoldToTalk();
                }
              }}
              role={isAuthenticated ? "button" : undefined}
              tabIndex={isAuthenticated ? 0 : -1}
              aria-label="Voice wake area"
              style={{ pointerEvents: "auto", background: "rgba(10,10,12,0.35)", color: "var(--jarvis-accent)", padding: "10px 18px", borderRadius: 999, backdropFilter: "blur(6px)", display: "flex", alignItems: "center", gap: 12, minWidth: 260, justifyContent: "center", boxShadow: "inset 0 0 20px rgba(255,255,255,0.02), 0 0 18px var(--jarvis-accent-glow)", border: "1px solid var(--jarvis-accent)", cursor: isAuthenticated ? "pointer" : "default" }}
            >
              <div style={{ fontFamily: "Inter, system-ui, sans-serif", fontSize: 14 }}>
                {listening ? "Listening..." : speaking ? "Responding..." : `Say 'Hey ${assistantName || "Jarvis"}'`}
              </div>

            </div>
          )}
        </div>
      </div>
    </div>
  );
}
