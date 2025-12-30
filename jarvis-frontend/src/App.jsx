// src/App.jsx
import React, { useState, useEffect, useRef, useCallback, useMemo, Suspense, lazy } from "react";
import { listenOnce, speak, initAudioProcessing, primeSpeechRecognition, recordPcm16Once } from "./utils/speech";
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
  API_URL
} from "./utils/api";
import "./styles/jarvis.css";

const scheduleIdle = (fn, timeout = 800) => {
  if (typeof window === "undefined") return setTimeout(fn, 0);
  if (typeof window.requestIdleCallback === "function") {
    return window.requestIdleCallback(fn, { timeout });
  }
  return setTimeout(fn, Math.min(250, timeout));
};

// Lazy-load heavy UI pieces
const ArcReactor = lazy(() => import("./components/ArcReactor"));
const HUDLogs = lazy(() => import("./components/HUDLogs"));
const AuthModal = lazy(() => import("./components/AuthModal"));
const PermissionModal = lazy(() => import("./components/PermissionModal"));

// Constants
const FILAMENT_WORKER_PATH = "/filamentWorker.js"; // put this file in public/
const AUDIO_WORKER_PATH = "/audioWorker.js"; // optional, put in public/

export default function App() {
  // Light state only — avoid large objects in state
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [wakePulse, setWakePulse] = useState(false);
  const [logs, setLogs] = useState([]);
  const [emotion, setEmotion] = useState("calm"); // calm|action|analyzing|critical
  const [volume, setVolume] = useState(0); // 0..1
  const [transformState, setTransformState] = useState("normal");
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [username, setUsername] = useState(null);
  const [assistantName, setAssistantName] = useState("Jarvis");
  const [role, setRole] = useState(null);
  const [permissions, setPermissions] = useState(null);
  const [showAuthModal, setShowAuthModal] = useState(false);
  const [permissionPrompt, setPermissionPrompt] = useState(null);
  const [voiceEnabled, setVoiceEnabled] = useState(true);

  // refs
  const wakeRecognizer = useRef(null);
  const isHandlingCommand = useRef(false);
  const assistantNameRef = useRef("Jarvis");
  const micStreamRef = useRef(null);
  const filamentCanvasRef = useRef(null);
  const filamentWorkerRef = useRef(null);
  const audioWorkerRef = useRef(null);

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

  const voiceLang = useMemo(() => {
    try {
      return (navigator.language || "en-US").toString();
    } catch {
      return "en-US";
    }
  }, []);

  useEffect(() => {
    // Mobile Chrome often requires an explicit user gesture before SpeechRecognition works reliably.
    // Default: enabled on desktop, disabled on mobile until the user taps.
    try {
      if (!isMobile) {
        setVoiceEnabled(true);
        return;
      }
      const stored = localStorage.getItem("jarvis_voice_enabled");
      setVoiceEnabled(stored === "1");
    } catch {
      if (isMobile) setVoiceEnabled(false);
    }
  }, [isMobile]);

  useEffect(() => {
    assistantNameRef.current = assistantName || "Jarvis";
  }, [assistantName]);

  const normalizeWake = useCallback((s) => {
    return String(s || "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
  }, []);

  // Minimal log writer (memoized to avoid re-creating)
  const addLog = useCallback((type, message) => {
    setLogs(prev => [{ type, message, time: new Date().toLocaleTimeString() }, ...prev.slice(0, 12)]);
  }, []);

  // ---------- Offload visuals to worker (OffscreenCanvas) ----------
  useEffect(() => {
    if (!isAuthenticated) return;

    let raf1 = 0;
    let raf2 = 0;
    let idleHandle = null;
    let cancelled = false;

    const initWorker = () => {
      if (cancelled) return;

      const canvas = document.getElementById("filamentCanvas");
      if (!canvas) {
        console.warn("filamentCanvas not found");
        return;
      }

      // Only start worker once
      if (!("OffscreenCanvas" in window)) {
        // Fallback: keep main-thread drawing but throttle it (not ideal)
        addLog("system", "OffscreenCanvas not supported; falling back to main-thread rendering (may be slower).");
        return;
      }

      try {
        const offscreen = canvas.transferControlToOffscreen();
        const worker = new Worker(FILAMENT_WORKER_PATH);
        filamentWorkerRef.current = worker;

        // Initialize worker with canvas and some config
        worker.postMessage({
          type: "init",
          canvas: offscreen,
          devicePixelRatio: window.devicePixelRatio || 1,
          width: window.innerWidth,
          height: window.innerHeight
        }, [offscreen]);

        // Worker can send logs or events back
        worker.onmessage = (ev) => {
          const data = ev.data || {};
          if (data.type === "log") addLog("system", `[filamentWorker] ${data.message}`);
        };

        // Resize handler: let worker handle resizing
        const onResize = () => {
          if (worker) {
            worker.postMessage({ type: "resize", width: window.innerWidth, height: window.innerHeight });
          }
        };
        window.addEventListener("resize", onResize);

        // cleanup
        return () => {
          window.removeEventListener("resize", onResize);
          try {
            worker.postMessage({ type: "dispose" });
            worker.terminate();
          } catch {}
        };
      } catch (err) {
        console.error("Filament worker init failed:", err);
        addLog("system", "Filament worker failed to initialize.");
      }
    };

    // Defer heavy worker init until after first paint + idle time
    // so auth/UI becomes responsive instantly.
    raf1 = window.requestAnimationFrame(() => {
      raf2 = window.requestAnimationFrame(() => {
        idleHandle = scheduleIdle(() => {
          const cleanup = initWorker();
          // if initWorker returned a cleanup, attach it to effect cleanup
          // (we store it on the ref to keep closure simple)
          filamentWorkerRef.current && (filamentWorkerRef.current.__cleanup = cleanup);
        }, 1200);
      });
    });

    return () => {
      cancelled = true;
      try { if (raf1) cancelAnimationFrame(raf1); } catch {}
      try { if (raf2) cancelAnimationFrame(raf2); } catch {}
      try {
        if (idleHandle && typeof window.cancelIdleCallback === "function") window.cancelIdleCallback(idleHandle);
        else if (idleHandle) clearTimeout(idleHandle);
      } catch {}

      // if we initialized a worker, run any stored cleanup
      try {
        const cleanup = filamentWorkerRef.current?.__cleanup;
        if (typeof cleanup === "function") cleanup();
      } catch {}
    };
    // run once
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // ---------- Audio processing (throttled) ----------
  useEffect(() => {
    if (!isAuthenticated) return;
    let rafId = null;
    let audioData = null;
    let lastWorkerSend = 0;
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

            // send small update to worker at throttled rate
            if (filamentWorkerRef.current && now - lastWorkerSend > THROTTLE_MS) {
              lastWorkerSend = now;
              filamentWorkerRef.current.postMessage({
                type: "audio",
                volume: Math.min(1, rms * 3) // scaled for visuals
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
    }, 1500);

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
  }, [addLog, isAuthenticated, voiceEnabled]);

  // ---------- Wake-word listener (same logic but keep stable callbacks) ----------
  useEffect(() => {
    if (!isAuthenticated) return;
    if (!voiceEnabled) return;
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      addLog("system", "SpeechRecognition not available in this browser.");
      return;
    }

    const recognizer = new SpeechRecognition();
    recognizer.continuous = true;
    recognizer.interimResults = false;
    recognizer.lang = "en-US";

    let active = false;
    let startAttempts = 0;

    const safeStart = () => {
      if (active) return;
      try {
        recognizer.start();
        active = true;
        startAttempts = 0;
      } catch (err) {
        startAttempts++;
        if (startAttempts < 6) setTimeout(safeStart, 400 + startAttempts * 200);
      }
    };

    recognizer.onresult = (e) => {
      try {
        const result = e.results[e.resultIndex];
        const transcript = normalizeWake(result[0].transcript || "");
        if (!transcript) return;
        if (isHandlingCommand.current) return;

        const nm = normalizeWake(assistantNameRef.current || "Jarvis");
        const wakeHit =
          (nm && (transcript.includes(`hey ${nm}`) || transcript.includes(`ok ${nm}`) || transcript.includes(`okay ${nm}`) || transcript === nm)) ||
          transcript.includes("wake up") ||
          transcript.includes("wakeup");

        if (wakeHit) {

          // vibrate UI briefly (visual) and pause auto recognition
          setWakePulse(true);
          setTimeout(() => setWakePulse(false), 900);

          // stop recognizer safely so we can do single-shot capture
          try { recognizer.stop(); } catch {}
          active = false;

          speak("Yes sir, I'm listening.", () => {
            setTimeout(async () => await handleVoiceCommand(), 150);
          });
        }
      } catch (err) {
        console.warn("Wake onresult parse err:", err);
      }
    };

    recognizer.onerror = (ev) => {
      const errName = ev?.error || "unknown";
      addLog("system", `Wake listener error: ${errName}`);
      active = false;
      // restart with backoff
      if (isAuthenticated) setTimeout(safeStart, errName === "aborted" ? 700 : 1500);
    };

    recognizer.onend = () => {
      active = false;
      if (!isHandlingCommand.current && isAuthenticated) setTimeout(safeStart, 700);
    };

    // Start after a short delay to avoid slowing initial paint
    const startTimer = setTimeout(safeStart, 600);
    wakeRecognizer.current = recognizer;
    addLog("system", "Wake-word listener started.");

    return () => {
      clearTimeout(startTimer);
      try {
        recognizer.onresult = null;
        recognizer.onerror = null;
        recognizer.onend = null;
        recognizer.stop();
      } catch {}
    };
    // run once
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addLog, isAuthenticated]);

  // ----- handleVoiceCommand (stable reference) -----
  const handleVoiceCommand = useCallback(async () => {
    if (isHandlingCommand.current) return;
    isHandlingCommand.current = true;
    setListening(true);
    addLog("system", "Capturing command...");

    // stop wake recognizer to avoid overlap
    try { wakeRecognizer.current?.stop(); } catch {}

    const webSpeechSupported =
      typeof window !== "undefined" &&
      ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

    const tryGoogleFallback = async () => {
      try {
        addLog("system", "Using Google Speech-to-Text...");
        const { audio_b64, sample_rate_hz } = await recordPcm16Once({
          sampleRateHz: 16000,
          maxMs: isIOS ? 9000 : 7500,
          silenceStopMs: isIOS ? 1400 : 1100,
        });
        if (!audio_b64) return null;
        const resp = await googleSpeechToText(sessionId, audio_b64, voiceLang, sample_rate_hz);
        return (resp?.text || "").toString().trim() || null;
      } catch (e) {
        console.warn("Google STT fallback failed:", e);
        return null;
      }
    };

    let transcript = null;
    if (webSpeechSupported) {
      try {
        transcript = await listenOnce({
          timeout: isMobile ? 16000 : 10000,
          silenceTimeoutMs: isIOS ? 1500 : 1100,
          interim: false,
          language: voiceLang,
          maxAlternatives: 1
        });
      } catch (err) {
        console.warn("listenOnce failed:", err);
      }
    }

    if (!transcript) {
      transcript = await tryGoogleFallback();
    }

    setListening(false);

    if (!transcript) {
      addLog("error", "No command received.");
      try { wakeRecognizer.current?.start(); } catch {}
      isHandlingCommand.current = false;
      return;
    }

    addLog("input", transcript);
    const textLower = transcript.toLowerCase();

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
      setTransformState("twist");
      addLog("action", "Reactor twisting.");
      speak("Twisting reactor geometry.", () => {
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/expand|bigger|open up/i.test(textLower)) {
      setTransformState("expand");
      addLog("action", "Reactor expanding.");
      speak("Expanding energy field.", () => {
        try { wakeRecognizer.current?.start(); } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/reset|normal|stable/i.test(textLower)) {
      setTransformState("normal");
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
      addLog("response", res.text || "No text returned.");

      const tLower = (res.text || "").toLowerCase();
      if (/\b(error|fail|cannot|no connection|critical|danger)\b/.test(tLower)) setEmotion("critical");
      else if (/\b(open|launch|execute|run|action)\b/.test(tLower)) setEmotion("action");
      else if (/\b(analyz|thinking|processing|research|search)\b/.test(tLower)) setEmotion("analyzing");
      else setEmotion("calm");

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
          "open_app", "close_app", "switch_app",
          "execute_command",
          "capture_screen", "screen_navigation",
          "read", "list", "mkdir",
          "write", "edit", "delete", "move", "copy", "cleanup",
          "self_update", "self_add",
        ]);

        const deviceActions = res.actions.filter(a => DEVICE_ACTION_TYPES.has((a || {}).type));
        const uiActions = res.actions.filter(a => !DEVICE_ACTION_TYPES.has((a || {}).type));

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

              // Try to parse FastAPI JSON error response embedded in the message.
              // api.js throws: "HTTP error! status: 403 - {\"detail\":{...}}" or "... - {\"detail\":\"...\"}"
              let parsed = null;
              try {
                const parts = raw.split(" - ");
                const maybeJson = parts.length >= 2 ? parts.slice(1).join(" - ") : null;
                parsed = maybeJson ? JSON.parse(maybeJson) : null;
              } catch {}

              const detail = parsed?.detail;
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
                const deviceId = permDetail?.device_id;
                setPermissionPrompt({
                  title: "PC agent offline",
                  message: deviceId ? `Your PC agent (${deviceId}) is offline. Start it now?` : "Your PC agent is offline. Start it now?",
                  details: "This will try to launch the agent on this Windows PC using a local protocol handler.",
                  kind: "start_agent",
                  deviceId,
                });
                try { speak("Your PC agent is offline. Please approve the popup to start it."); } catch {}
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
  }, [sessionId, addLog, isMobile, isIOS, voiceLang]);

  // If filament worker exists, update it when relevant props change
  useEffect(() => {
    if (!filamentWorkerRef.current) return;
    filamentWorkerRef.current.postMessage({
      type: "state",
      emotion,
      wakePulse,
      transformState,
      volume
    });
  }, [emotion, wakePulse, transformState, volume]);

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
      })
      .catch(() => {});
  }, [addLog]);

  // On every login (including auto-session restore), if no PC agent is connected, ask the user to start it.
  useEffect(() => {
    if (!isAuthenticated || !sessionId) return;

    let cancelled = false;
    fetch(`${API_URL}/api/device/status?session_id=${encodeURIComponent(sessionId)}`)
      .then(r => r.json())
      .then(data => {
        if (cancelled) return;
        const agents = Array.isArray(data?.agents) ? data.agents : [];
        if (agents.length) return;

        // Avoid overwriting an existing permission prompt (e.g., a missing-capability prompt).
        setPermissionPrompt(prev => {
          if (prev) return prev;
          try { speak("PC agent is not running. Please approve the popup to start it."); } catch {}
          return {
            kind: "start_agent",
            title: "PC agent not running",
            message: "To run PC tasks (like opening Notepad), Jarvis needs a small agent running on your Windows PC.",
            details: "Start it now? (Requires a one-time protocol install on this PC.)",
          };
        });
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, [isAuthenticated, sessionId]);

  useEffect(() => {
    const storedSession = localStorage.getItem("jarvis_session");
    const storedUsername = localStorage.getItem("jarvis_username");
    const storedRole = localStorage.getItem("jarvis_role");
    const storedPermissionsRaw = localStorage.getItem("jarvis_permissions");
    const storedAssistantName = localStorage.getItem("jarvis_assistant_name");
    if (storedSession && storedUsername) {
      fetch(`${API_URL}/api/validate-session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: storedSession })
      })
        .then(res => res.json())
        .then(data => {
          if (data.valid) {
            setSessionId(storedSession);
            setUsername(storedUsername);
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
            setIsAuthenticated(true);
          } else {
            localStorage.removeItem("jarvis_session");
            localStorage.removeItem("jarvis_username");
            localStorage.removeItem("jarvis_role");
            localStorage.removeItem("jarvis_permissions");
            localStorage.removeItem("jarvis_assistant_name");
            setShowAuthModal(true);
          }
        })
        .catch(() => setShowAuthModal(true));
    } else {
      setShowAuthModal(true);
    }
  }, []);

  // ----------------- RENDER -----------------
  return (
    <div className="jarvis-root">
      <Suspense fallback={<div />}>
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
      </Suspense>

      <Suspense fallback={<div />}>
        {permissionPrompt && (
          <PermissionModal
            title={permissionPrompt.title}
            message={permissionPrompt.message}
            details={permissionPrompt.details}
            allowLabel="Allow"
            denyLabel="Deny"
            onDeny={() => setPermissionPrompt(null)}
            onAllow={async () => {
              const req = permissionPrompt;
              setPermissionPrompt(null);
              try {
                if (req.kind === "start_agent") {
                  window.location.href = "jarvisagent://start";
                  addLog("system", "Requested agent start (jarvisagent://start). If nothing happens, install scripts/install_jarvisagent_protocol.ps1 on this PC.");
                  return;
                }

                await grantDevicePermissions(sessionId, req.neededPermissions);
                await dispatchDeviceActions(req.pendingActions, sessionId, req.sourceText);
                addLog("system", "Permission granted. PC action queued.");
              } catch (err) {
                addLog("system", `Permission grant failed: ${err?.message || err}`);
              }
            }}
          />
        )}
      </Suspense>

      {isAuthenticated && username && (
        <div style={{ position: "fixed", top: 20, right: 20, zIndex: 15 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 8, height: 8, borderRadius: 8, background: "#00ffc8", boxShadow: "0 0 10px rgba(0,255,200,0.6)" }} />
            <span style={{ color: "#00ffc8", fontSize: 14 }}>
              {username}{role ? ` (${role})` : ""}
            </span>
            <span style={{ color: "#00ffc8", fontSize: 14, opacity: 0.9 }}>
              Assistant: {assistantName || "Jarvis"}
            </span>

            {isMobile && (
              <button
                onClick={async () => {
                  if (voiceEnabled) return;
                  try {
                    addLog("system", "Enabling voice…");
                    // Prompt mic permission (user gesture) and warm up recognition.
                    await initAudioProcessing();
                    await primeSpeechRecognition(voiceLang);
                    setVoiceEnabled(true);
                    try { localStorage.setItem("jarvis_voice_enabled", "1"); } catch {}
                    speak("Voice enabled.");
                  } catch {
                    addLog("system", "Could not enable voice. Please allow microphone permission.");
                    try { speak("Please allow microphone permission to enable voice."); } catch {}
                  }
                }}
                style={{
                  background: voiceEnabled ? "rgba(0,255,200,0.15)" : "rgba(255,210,77,0.15)",
                  border: "1px solid rgba(0,255,200,0.35)",
                  color: "#00ffc8",
                  cursor: voiceEnabled ? "default" : "pointer",
                  borderRadius: 10,
                  padding: "6px 10px",
                  fontSize: 12,
                  opacity: voiceEnabled ? 0.75 : 1,
                }}
              >
                {voiceEnabled ? "Voice On" : "Enable Voice"}
              </button>
            )}

            <button onClick={() => {
              // Best-effort: stop the local PC agent when the user logs out.
              // This requires the jarvisagent:// protocol handler to be installed on this PC.
              try {
                const iframe = document.createElement("iframe");
                iframe.style.display = "none";
                iframe.src = "jarvisagent://stop";
                document.body.appendChild(iframe);
                setTimeout(() => {
                  try { document.body.removeChild(iframe); } catch {}
                }, 1500);
              } catch {}

              localStorage.removeItem("jarvis_session");
              localStorage.removeItem("jarvis_username");
              localStorage.removeItem("jarvis_role");
              localStorage.removeItem("jarvis_permissions");
              localStorage.removeItem("jarvis_assistant_name");
              setIsAuthenticated(false);
              setSessionId(null);
              setUsername(null);
              setAssistantName("Jarvis");
              setRole(null);
              setPermissions(null);
              setPermissionPrompt(null);
              try {
                if (isMobile) {
                  localStorage.removeItem("jarvis_voice_enabled");
                  setVoiceEnabled(false);
                }
              } catch {}
              setShowAuthModal(true);
              speak("Logged out successfully.");
            }} style={{ background: "transparent", border: "none", color: "#ff5050", cursor: "pointer" }}>Logout</button>
          </div>
        </div>
      )}

      {/* OffscreenCanvas target */}
      <canvas id="filamentCanvas" style={{ position: "fixed", inset: 0, zIndex: 1, pointerEvents: "none", width: "100vw", height: "100vh" }} />

      <div className="hud-overlay" style={{ position: "fixed", inset: 0, zIndex: 5, display: "flex", alignItems: "center", justifyContent: "center", pointerEvents: "none" }}>
        <div className="reactor-shell" style={{
          width: "min(36vmin, 420px)",
          height: "min(36vmin, 420px)",
          borderRadius: "50%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          transition: "transform 420ms ease, box-shadow 320ms ease",
          transform: transformState === "expand" ? "scale(1.08)" : transformState === "contract" ? "scale(0.88)" : "scale(1)",
          pointerEvents: "none"
        }}>
          <div style={{ pointerEvents: "none", zIndex: 6 }}>
            <Suspense fallback={<div />}>
              <ArcReactor active={listening || speaking} wakePulse={wakePulse} emotion={emotion} volume={volume} transformState={transformState} />
            </Suspense>
          </div>
        </div>
      </div>

      <div style={{ position: "fixed", left: 20, top: 20, zIndex: 10 }}>
        <Suspense fallback={<div />}>
          <HUDLogs logs={logs} />
        </Suspense>
      </div>

      <div style={{ position: "fixed", bottom: 30, left: 0, right: 0, display: "flex", justifyContent: "center", zIndex: 12, pointerEvents: "none" }}>
        <div style={{ pointerEvents: "auto", background: "rgba(10,10,12,0.35)", color: "white", padding: "10px 18px", borderRadius: 999, backdropFilter: "blur(6px)", display: "flex", alignItems: "center", gap: 12, minWidth: 260, justifyContent: "center", boxShadow: "inset 0 0 20px rgba(255,255,255,0.02)" }}>
          <div style={{ width: 12, height: 12, borderRadius: 6, background: listening ? "#00ffc8" : wakePulse ? "#00d4ff" : speaking ? "#ffb86b" : "#6b7280", boxShadow: listening ? "0 0 12px rgba(0,255,200,0.6)" : wakePulse ? "0 0 10px rgba(0,212,255,0.45)" : "" }} />
          <div style={{ fontFamily: "Inter, system-ui, sans-serif", fontSize: 14 }}>
            {listening ? "Listening..." : speaking ? "Responding..." : `Say 'Hey ${assistantName || "Jarvis"}' to begin`}
          </div>
        </div>
      </div>
    </div>
  );
}
