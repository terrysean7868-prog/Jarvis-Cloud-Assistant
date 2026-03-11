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
  API_URL,
  getNotificationsWsUrl,
  stopTask,
  getAdminUpdateHistory,
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
    const raw = (typeof process !== "undefined" && process.env && process.env.REACT_APP_WAKE_SESSION_MINUTES)
      ? String(process.env.REACT_APP_WAKE_SESSION_MINUTES)
      : "15";
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
    const raw = (typeof process !== "undefined" && process.env && process.env.REACT_APP_GOOGLE_SPEECH_ENABLED)
      ? String(process.env.REACT_APP_GOOGLE_SPEECH_ENABLED)
      : "false";
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
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [username, setUsername] = useState(null);
  const [assistantName, setAssistantName] = useState("Jarvis");
  const [role, setRole] = useState(null);
  const [, setPermissions] = useState(null);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [permissionPrompt, setPermissionPrompt] = useState(null);
  const [showUpdateConsole, setShowUpdateConsole] = useState(false);
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
            return;
          }

          if (type === "research_failed") {
            const topic = (msg?.topic || "").toString();
            const err = (msg?.error || "").toString();
            const taskId = (msg?.task_id || "").toString();
            if (taskId) latestResearchTaskIdRef.current = taskId;
            addLog("error", `Research failed${topic ? `: ${topic}` : ""}. ${err || ""}`.trim());
            return;
          }

          if (type === "research_cancelled") {
            const topic = (msg?.topic || "").toString();
            addLog("system", `Research cancelled${topic ? `: ${topic}` : ""}.`);
            return;
          }

          if (type === "device_job_result") {
            const deviceId = (msg?.device_id || "").toString();
            const jobId = (msg?.job_id || "").toString();
            const sourceText = (msg?.source_text || "").toString();
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
            return;
          }

          addLog("system", `${type}: ${JSON.stringify(msg)}`);
        };

        ws.onclose = (evt) => {
          if (closedByCleanup) return;

          // 1008 = policy violation (we use this for missing/invalid session_id)
          if (evt && evt.code === 1008) {
            addLog("system", "Realtime notifications disconnected (auth required). Please login again.");
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
  }, [isAuthenticated, sessionId, addLog, addStructuredLog]);

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
      addLog("system", "Checking PC agent…");
      const online = await isPcAgentOnline(sid);
      if (!online) {
        addLog("system", "PC agent is offline. Start JarvisPCAgent.exe (or python pc_agent.py), then try again.");
        return;
      }

      addLog("system", "PC agent online. Establishing connection…");
      const resp = await configureMyPc(sid);
      try {
        const did = (resp?.device_id || "").toString().trim();
        if (did) localStorage.setItem("jarvis_device_id", did);
      } catch {}
      addLog("system", "PC agent connected.");
    } catch {
      addLog("system", "Could not establish connection. Try again.");
    }
  }, [sessionId, addLog, isPcAgentOnline]);

  useEffect(() => {
    if (!isAuthenticated || !sessionId) {
      setSystemInfo(null);
      return;
    }

    let cancelled = false;

    // Fast-path: hydrate from cache immediately.
    try {
      const raw = sessionStorage.getItem(`jarvis_system_info_${sessionId}`);
      const cached = raw ? JSON.parse(raw) : null;
      if (cached && typeof cached === "object") setSystemInfo(cached);
    } catch {}

    const poll = async () => {
      try {
        const info = await getSystemInfo(sessionId, 2500);
        if (!cancelled) {
          setSystemInfo(info);
          try {
            sessionStorage.setItem(`jarvis_system_info_${sessionId}`, JSON.stringify(info));
          } catch {}
        }
      } catch {
        // Local-only endpoint; ignore errors (cloud mode / permissions).
        // Keep the last known values to avoid UI "blanking".
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
    return String(s || "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }, []);

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

    addLog("input", transcript);
    const textLower = transcript.toLowerCase();

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
      const res = await sendMessage(transcript, "voice", sessionId);
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

      setSpeaking(true);
      speak(res.text || "Done.", () => {
        setSpeaking(false);
        setEmotion("calm");
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });

      // run any actions returned
      if (Array.isArray(res.actions) && res.actions.length) {
        const DEVICE_ACTION_TYPES = new Set([
          "device_action",
          "open_app", "close_app", "switch_app",
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
              addLog("system", `PC actions queued (${deviceActions.length}).`);
              if (dispatchRes?.status === "queued") {
                // ok
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

              if (permDetail?.required_capability) {
                const needed = { [permDetail.required_capability]: true };
                setPermissionPrompt({
                  title: "Permission required",
                  message: permDetail.message || "This action needs permission on your PC agent.",
                  details: permDetail.env_var ? `Allow it now? (This updates the running PC agent.)\nRequired: ${permDetail.env_var}` : "Allow it now? (This updates the running PC agent.)",
                  neededPermissions: needed,
                  pendingActions: deviceActions,
                  sourceText: transcript,
                });
                try {
                  speak("Permission required. Please approve the popup.");
                } catch {}
              } else if (permDetail?.message === "Device agent is not connected" || /device agent is not connected/i.test(String(permMsg || raw))) {
                addLog("system", "PC agent is not connected. Start JarvisPCAgent.exe (or python pc_agent.py) and login again (then click Yes).");
                try { speak("PC agent is not connected. Start it and login again."); } catch {}
              } else if (/no device assigned to this user/i.test(String(permMsg || raw))) {
                addLog("system", "PC is not configured. Login again and click Yes when asked about PC agent.");
              } else {
                addLog("system", `PC action dispatch failed: ${permMsg || raw}`);
                addLog("system", "Make sure pc_agent.py is running on your PC and connected.");
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
            const w = window.open(targetUrl, "_blank", "noopener,noreferrer");
            if (!w) {
              addLog("system", `Popup blocked. Open this link: ${targetUrl}`);
              try {
                if (navigator.clipboard?.writeText) navigator.clipboard.writeText(targetUrl);
              } catch {}
            }
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
  }, [sessionId, addLog, addStructuredLog, isMobile, isIOS, voiceLang, endWakeSessionWindow, googleSttEnabled, voiceBiometricsEnabled, voiceBiometricsActive, startWakeSessionWindow]);

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
    let restartTimer = null;
    let watchdogTimer = null;
    let lastEventAt = Date.now();

    const scheduleStart = (ms) => {
      try {
        if (restartTimer) clearTimeout(restartTimer);
      } catch {}
      restartTimer = setTimeout(safeStart, ms);
    };

    const safeStart = () => {
      if (active) return;

      const disabledUntil = Number(wakeDisabledUntilRef.current || 0);
      const now = Date.now();
      if (disabledUntil && now < disabledUntil) {
        const wait = Math.max(250, Math.min(disabledUntil - now, 60_000));
        scheduleStart(wait);
        return;
      }

      try {
        recognizer.start();
        active = true;
        startAttempts = 0;
        lastEventAt = Date.now();
      } catch (err) {
        startAttempts++;
        if (startAttempts < 6) scheduleStart(400 + startAttempts * 200);
      }
    };

    recognizer.onstart = () => {
      active = true;
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

        const nm = normalizeWake(assistantNameRef.current || "Jarvis");
        const wakeByPhrase = transcript.includes("wake up") || transcript.includes("wakeup");
        const wakeByNamePhrase = nm && (
          transcript.includes(`${nm} wake up`) ||
          transcript.includes(`wake up ${nm}`) ||
          transcript.includes(`${nm} wakeup`) ||
          transcript.includes(`wakeup ${nm}`)
        );
        const wakeHit =
          (nm && (transcript.includes(`hey ${nm}`) || transcript.includes(`ok ${nm}`) || transcript.includes(`okay ${nm}`) || transcript === nm)) ||
          wakeByPhrase ||
          wakeByNamePhrase;

        if (wakeHit) {
          // In biometrics mode, defer wake-session start and UI pulse until
          // we successfully capture a verified command.
          if (!voiceBiometricsActive) {
            // Start/refresh the wake session window.
            startWakeSessionWindow();

            // vibrate UI briefly (visual) and pause auto recognition
            setWakePulse(true);
            setTimeout(() => setWakePulse(false), 900);
          }

          // stop recognizer safely so we can do single-shot capture
          try { recognizer.stop(); } catch {}
          active = false;

          setTimeout(async () => await handleVoiceCommand(), 60);
          return;
        }

        // During wake-session: treat ANY speech as a command (no re-wake needed).
        if (inWakeSession && rawTranscript) {
          pendingTranscriptRef.current = rawTranscript;

          // stop recognizer safely so we can process command without overlap
          try { recognizer.stop(); } catch {}
          active = false;

          setTimeout(async () => await handleVoiceCommand(), 60);
          return;
        }
      } catch (err) {
        console.warn("Wake onresult parse err:", err);
      }
    };

    recognizer.onerror = (ev) => {
      const errName = ev?.error || "unknown";
      lastEventAt = Date.now();

      // Some errors are not recoverable without user action (permissions / missing mic).
      // Avoid an infinite restart loop that spams logs in packaged desktop builds.
      const fatal = new Set(["not-allowed", "service-not-allowed", "audio-capture", "language-not-supported"]);
      if (fatal.has(errName)) {
        if (!wakePermissionHintedRef.current) {
          wakePermissionHintedRef.current = true;
          if (errName === "language-not-supported") {
            addLog("system", `Wake listener disabled: language not supported (${voiceLang || "en-US"}).`);
          } else if (errName === "audio-capture") {
            addLog("system", "Wake listener disabled: microphone not available.");
          } else {
            addLog(
              "system",
              "Wake listener disabled: microphone permission denied. Enable microphone access for this app/browser, then reload Jarvis."
            );
          }
        }
        wakeDisabledUntilRef.current = Date.now() + 10 * 60 * 1000;
        active = false;
        return;
      }

      // Reduce disruptive log spam; "no-speech" is common and not actionable.
      if (errName !== "no-speech") {
        addLog("system", `Wake listener error: ${errName}`);
      } else {
        const now = Date.now();
        // "no-speech" is extremely common; keep it very quiet.
        if (now - lastWakeNoSpeechLogRef.current > 10 * 60 * 1000) {
          lastWakeNoSpeechLogRef.current = now;
          addLog("system", "Wake listener: idle (no speech)");
        }
      }
      active = false;
      // restart with backoff
      if (isAuthenticated) scheduleStart(errName === "aborted" ? 700 : 1500);
    };

    recognizer.onend = () => {
      active = false;
      lastEventAt = Date.now();
      if (!isHandlingCommand.current && isAuthenticated) scheduleStart(700);
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
          scheduleStart(1200);
          return;
        }

        // If we are not active, attempt a gentle restart.
        if (!active) {
          scheduleStart(1200);
        }
      } catch {
        // ignore
      }
    }, 12_000);

    // Start after a short delay to avoid slowing initial paint
    const startTimer = setTimeout(safeStart, 600);
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
  }, [addLog, handleVoiceCommand, isAuthenticated, isMobile, normalizeWake, voiceLang, voiceUnlocked, startWakeSessionWindow, voiceBiometricsActive]);

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
              setPermissionPrompt(null);
            }}
            onAllow={async () => {
              const req = permissionPrompt;
              try {
                // For all other permission prompts, close the modal immediately before proceeding.
                setPermissionPrompt(null);
                const grantRes = await grantDevicePermissions(sessionId, req.neededPermissions);
                if (grantRes?.offline) {
                  addLog("system", "Permission saved. Start JarvisPCAgent.exe (or python pc_agent.py) and login again (then click Yes).");
                  return;
                }

                await dispatchDeviceActions(req.pendingActions, sessionId, req.sourceText);
                addLog("system", "Permission granted. PC action queued.");
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
      )}

      {activeDisplay !== "autonomy" && (
        <JarvisDashboard
          isAuthenticated={isAuthenticated}
          logs={logs}
          tasks={tasks}
          emotion={emotion}
          listening={listening}
          speaking={speaking}
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
        />
      )}

      {/* Bottom stack: status above the wake prompt */}
      <div style={{ position: "fixed", bottom: 24, left: 0, right: 0, display: "flex", justifyContent: "center", zIndex: 12, pointerEvents: "none" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "center" }}>
          <div style={{ pointerEvents: "none", background: "rgba(10,10,12,0.28)", color: "var(--jarvis-accent)", padding: "8px 14px", borderRadius: 999, backdropFilter: "blur(6px)", display: "flex", alignItems: "center", gap: 10, minWidth: 220, justifyContent: "center", boxShadow: "inset 0 0 20px rgba(255,255,255,0.02)" }}>
            <div style={{ width: 10, height: 10, borderRadius: 999, background: emotion === "critical" ? "#ff4d4f" : emotion === "analyzing" ? "#ffd24d" : "#00ffc8", boxShadow: emotion === "critical" ? "0 0 10px rgba(255,77,79,0.45)" : emotion === "analyzing" ? "0 0 10px rgba(255,210,77,0.35)" : "0 0 10px rgba(0,255,200,0.45)" }} />
            <div style={{ fontFamily: "Inter, system-ui, sans-serif", fontSize: 13, letterSpacing: 0.3 }}>
              {emotion === "calm" && ((listening || speaking) ? "Listening (session)" : "Idle")}
              {emotion === "analyzing" && "Analyzing"}
              {emotion === "critical" && "Critical"}
            </div>
          </div>

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
              {listening ? "Listening..." : speaking ? "Responding..." : `Say 'Hey ${assistantName || "Jarvis"}' to wake up`}
            </div>

          </div>
        </div>
      </div>
    </div>
  );
}
