// src/App.jsx
import React, { useState, useEffect, useRef, useCallback, Suspense, lazy } from "react";
import { listenOnce, speak, initAudioProcessing } from "./utils/speech";
import { sendMessage } from "./utils/api";
import "./styles/jarvis.css";

// Lazy-load heavy UI pieces
const ArcReactor = lazy(() => import("./components/ArcReactor"));
const HUDLogs = lazy(() => import("./components/HUDLogs"));
const AuthModal = lazy(() => import("./components/AuthModal"));

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
  const [showAuthModal, setShowAuthModal] = useState(false);

  // refs
  const wakeRecognizer = useRef(null);
  const isHandlingCommand = useRef(false);
  const micStreamRef = useRef(null);
  const filamentCanvasRef = useRef(null);
  const filamentWorkerRef = useRef(null);
  const audioWorkerRef = useRef(null);

  // Minimal log writer (memoized to avoid re-creating)
  const addLog = useCallback((type, message) => {
    setLogs(prev => [{ type, message, time: new Date().toLocaleTimeString() }, ...prev.slice(0, 12)]);
  }, []);

  // ---------- Offload visuals to worker (OffscreenCanvas) ----------
  useEffect(() => {
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
    // run once
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ---------- Audio processing (throttled) ----------
  useEffect(() => {
    let rafId = null;
    let audioData = null;
    let lastSend = 0;
    const THROTTLE_MS = 50; // 20fps audio updates to worker (light)

    const init = async () => {
      try {
        audioData = await initAudioProcessing(); // returns { analyser, stream } if implemented
        if (audioData && audioData.analyser) {
          micStreamRef.current = audioData.stream;
          const analyser = audioData.analyser;
          const dataArray = new Float32Array(analyser.fftSize);

          const tick = () => {
            analyser.getFloatTimeDomainData(dataArray);
            // compute light RMS on small slice to keep it cheap
            let sum = 0;
            // sample a subset for speed
            for (let i = 0; i < dataArray.length; i += Math.max(1, Math.floor(dataArray.length / 128))) {
              const v = dataArray[i];
              sum += v * v;
            }
            const rms = Math.sqrt(sum / Math.max(1, Math.floor(dataArray.length / 128)));
            // smooth on main thread (cheap)
            setVolume(v => Math.min(1, v * 0.8 + rms * 0.2));

            // send small update to worker at throttled rate
            const now = performance.now();
            if (filamentWorkerRef.current && now - lastSend > THROTTLE_MS) {
              lastSend = now;
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

    init();

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      try { micStreamRef.current?.getTracks().forEach(t => t.stop()); } catch {}
    };
  }, [addLog]);

  // ---------- Wake-word listener (same logic but keep stable callbacks) ----------
  useEffect(() => {
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
        const transcript = (result[0].transcript || "").trim().toLowerCase();
        if (!transcript) return;
        if (isHandlingCommand.current) return;

        if (transcript.includes("hey jarvis") || transcript.includes("ok jarvis") ||
            transcript.includes("wake up") || transcript.includes("wakeup")) {

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
      setTimeout(safeStart, errName === "aborted" ? 700 : 1500);
    };

    recognizer.onend = () => {
      active = false;
      if (!isHandlingCommand.current) setTimeout(safeStart, 700);
    };

    safeStart();
    wakeRecognizer.current = recognizer;
    addLog("system", "Wake-word listener started.");

    return () => {
      try {
        recognizer.onresult = null;
        recognizer.onerror = null;
        recognizer.onend = null;
        recognizer.stop();
      } catch {}
    };
    // run once
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [addLog]);

  // ----- handleVoiceCommand (stable reference) -----
  const handleVoiceCommand = useCallback(async () => {
    if (isHandlingCommand.current) return;
    isHandlingCommand.current = true;
    setListening(true);
    addLog("system", "Capturing command...");

    // stop wake recognizer to avoid overlap
    try { wakeRecognizer.current?.stop(); } catch {}

    let transcript = null;
    try {
      transcript = await listenOnce({
        timeout: 10000,
        interim: false,
        language: "en-US",
        maxAlternatives: 1
      });
    } catch (err) {
      console.warn("listenOnce failed:", err);
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
      const res = await sendMessage(transcript, "chat", sessionId);
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
        for (const a of res.actions) {
          addLog("action", `${a.type} ${a.value || a.url || a.file_path || ""}`);
          if (a.type === "open_url" && (a.value || a.url)) {
            window.open(a.value || a.url, "_blank");
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
  }, [sessionId, addLog]);

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
  const handleAuthSuccess = useCallback((newSessionId, newUsername) => {
    setSessionId(newSessionId);
    setUsername(newUsername);
    setIsAuthenticated(true);
    setShowAuthModal(false);
    localStorage.setItem("jarvis_session", newSessionId);
    localStorage.setItem("jarvis_username", newUsername);
    addLog("system", `Authenticated as ${newUsername}`);
  }, [addLog]);

  useEffect(() => {
    const storedSession = localStorage.getItem("jarvis_session");
    const storedUsername = localStorage.getItem("jarvis_username");
    if (storedSession && storedUsername) {
      fetch(`${process.env.REACT_APP_API_URL || "http://localhost:8000"}/api/validate-session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: storedSession })
      })
        .then(res => res.json())
        .then(data => {
          if (data.valid) {
            setSessionId(storedSession);
            setUsername(storedUsername);
            setIsAuthenticated(true);
          } else {
            localStorage.removeItem("jarvis_session");
            localStorage.removeItem("jarvis_username");
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

      {isAuthenticated && username && (
        <div style={{ position: "fixed", top: 20, right: 20, zIndex: 15 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{ width: 8, height: 8, borderRadius: 8, background: "#00ffc8", boxShadow: "0 0 10px rgba(0,255,200,0.6)" }} />
            <span style={{ color: "#00ffc8", fontSize: 14 }}>{username}</span>
            <button onClick={() => {
              localStorage.removeItem("jarvis_session");
              localStorage.removeItem("jarvis_username");
              setIsAuthenticated(false);
              setSessionId(null);
              setUsername(null);
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
            {listening ? "Listening..." : speaking ? "Responding..." : "Say 'Hey Jarvis' to begin"}
          </div>
        </div>
      </div>
    </div>
  );
}
