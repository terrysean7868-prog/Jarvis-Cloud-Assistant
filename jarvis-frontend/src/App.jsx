// src/App.jsx
import React, { useState, useEffect, useRef } from "react";
import { listenOnce, speak } from "./utils/speech";
import { sendMessage } from "./utils/api";
import ArcReactor from "./components/ArcReactor";
import HUDLogs from "./components/HUDLogs";
import "./styles/jarvis.css";

/**
 * Phase 11.5: Intelligent Wake Logic + Neural Filament Grid + Emotional Glow
 *
 * - Stable wake-word detection: avoids multiple starts, handles "aborted" errors.
 * - Pauses wake listener while capturing a command (prevents overlap).
 * - Emits spoken ACK: "Yes sir, I'm listening."
 * - Neural filament canvas that reacts to emotion, wakePulse and mic volume.
 * - Emotional glow overlay that changes core color (calm/action/critical).
 *
 * Requirements: utils/speech.js -> listenOnce() & speak(text, onEnd)
 *               utils/api.js -> sendMessage(text) returns { text, actions }
 *
 * Drop into src/App.jsx and run.
 */

export default function App() {
  // UI / state
  const [listening, setListening] = useState(false); // actively recording command
  const [speaking, setSpeaking] = useState(false);
  const [wakePulse, setWakePulse] = useState(false); // short visual pulse on wake
  const [logs, setLogs] = useState([]);
  const [emotion, setEmotion] = useState("calm"); // calm | action | analyzing | critical
  const [volume, setVolume] = useState(0); // 0..1 mic amplitude
  const [transformState, setTransformState] = useState("normal"); // visual transform commands

  // refs
  const wakeRecognizer = useRef(null);
  const isHandlingCommand = useRef(false); // prevents re-entrance
  const micStreamRef = useRef(null);
  const filamentRAF = useRef(null);

  // addLog helper
  const addLog = (type, message) => {
    setLogs((prev) => [
      { type, message, time: new Date().toLocaleTimeString() },
      ...prev.slice(0, 12),
    ]);
  };

  // --- Mic amplitude analyzer for reactive visuals
  useEffect(() => {
    let audioCtx = null;
    let analyser = null;
    let dataArray = null;
    let rafId = null;

    const init = async () => {
      try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        micStreamRef.current = stream;
        const src = audioCtx.createMediaStreamSource(stream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        src.connect(analyser);
        dataArray = new Uint8Array(analyser.frequencyBinCount);

        const tick = () => {
          analyser.getByteFrequencyData(dataArray);
          // compute simple normalized RMS-like amplitude
          let sum = 0;
          for (let i = 0; i < dataArray.length; i++) sum += dataArray[i] * dataArray[i];
          const rms = Math.sqrt(sum / dataArray.length) / 255;
          // smooth slightly
          setVolume((v) => Math.min(1, v * 0.85 + rms * 0.15));
          rafId = requestAnimationFrame(tick);
        };
        tick();
      } catch (err) {
        console.warn("Mic analyzer init failed:", err);
      }
    };

    init();

    return () => {
      if (rafId) cancelAnimationFrame(rafId);
      try {
        micStreamRef.current?.getTracks().forEach((t) => t.stop());
      } catch {}
      try {
        audioCtx?.close();
      } catch {}
    };
  }, []);

  // --- Robust wake-word listener (continuous) ---
  useEffect(() => {
    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
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
        // InvalidStateError occurs if .start() called too fast; ignore and retry later
        startAttempts++;
        if (startAttempts < 5) setTimeout(safeStart, 500 + startAttempts * 200);
      }
    };

    recognizer.onresult = (e) => {
      // Only consider final results (interimResults=false so usually final)
      try {
        const result = e.results[e.resultIndex];
        const transcript = (result[0].transcript || "").trim().toLowerCase();
        if (!transcript) return;

        // If currently handling command, ignore (we'll resume later)
        if (isHandlingCommand.current) return;

        // Wake words
        if (transcript.includes("hey jarvis") || transcript.includes("ok jarvis")) {
          addLog("wake", "Wake word detected.");
          // visual pulse
          setWakePulse(true);
          setTimeout(() => setWakePulse(false), 900);

          // Speak acknowledgement and then process command
          // Pause recognizer to avoid overlap
          try {
            // stop recognizer gracefully and mark inactive
            try {
              recognizer.stop();
            } catch {}
            active = false;
          } catch {}

          // Audible ACK and then capture command (listenOnce handles single-shot)
          speak("Yes sir, I'm listening.", () => {
            // tiny delay so speak finishes and mic readies
            setTimeout(async () => {
              await handleVoiceCommand(); // captures command, resumes wake listener inside
            }, 120);
          });
        }
      } catch (err) {
        console.warn("Wake onresult parsing error:", err);
      }
    };

    recognizer.onerror = (ev) => {
      // handle aborts / no-speech gracefully by restarting after small backoff
      const errName = ev?.error || "unknown";
      addLog("system", `Wake listener error: ${errName}`);
      if (errName === "aborted" || errName === "no-speech" || errName === "network") {
        active = false;
        setTimeout(safeStart, 700);
      } else {
        // generic fallback
        active = false;
        setTimeout(safeStart, 1500);
      }
    };

    recognizer.onend = () => {
      active = false;
      // if we're not in the middle of handling a command, restart after short pause
      if (!isHandlingCommand.current) setTimeout(safeStart, 600);
    };

    // start initially
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
    // we intentionally run this once on mount
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Handle capturing a single command (pauses wake listener) ---
  const handleVoiceCommand = async () => {
    // if another command running, ignore
    if (isHandlingCommand.current) return;
    isHandlingCommand.current = true;
    setListening(true);
    addLog("system", "Capturing command...");

    // stop wake recognizer if active to avoid interference
    try {
      wakeRecognizer.current?.stop();
    } catch {}

    // listenOnce is expected to return a transcript string (or null)
    let transcript = null;
    try {
      transcript = await listenOnce({ timeout: 8000, interim: false });
    } catch (err) {
      console.warn("listenOnce failed:", err);
    }

    setListening(false);

    if (!transcript) {
      addLog("error", "No command received.");
      // restart wake recognizer
      try {
        wakeRecognizer.current?.start();
      } catch {}
      isHandlingCommand.current = false;
      return;
    }

    addLog("input", transcript);

    // Local quick commands controlling visuals
    const text = transcript.toLowerCase();
    if (/twist|vortex|snake/i.test(text)) {
      setTransformState("twist");
      addLog("action", "Reactor twisting.");
      speak("Twisting reactor geometry now.", () => {
        // resume wake listener
        try {
          wakeRecognizer.current?.start();
        } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/expand|bigger|open up/i.test(text)) {
      setTransformState("expand");
      addLog("action", "Reactor expanding.");
      speak("Expanding energy field.", () => {
        try {
          wakeRecognizer.current?.start();
        } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/shrink|contract|small/i.test(text)) {
      setTransformState("contract");
      addLog("action", "Reactor contracting.");
      speak("Contracting core.", () => {
        try {
          wakeRecognizer.current?.start();
        } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }
    if (/reset|normal|stable/i.test(text)) {
      setTransformState("normal");
      addLog("action", "Reactor normalized.");
      speak("Reactor returning to normal state.", () => {
        try {
          wakeRecognizer.current?.start();
        } catch {}
        isHandlingCommand.current = false;
      });
      return;
    }

    // If not a local command, send to backend
    try {
      const res = await sendMessage(transcript);
      addLog("response", res.text || "No text returned.");

      // update emotion based on keywords
      const tLower = (res.text || "").toLowerCase();
      if (/\b(error|fail|cannot|no connection|critical|danger)\b/.test(tLower)) setEmotion("critical");
      else if (/\b(open|launch|execute|run|action)\b/.test(tLower)) setEmotion("action");
      else if (/\b(analyz|thinking|processing|research|search)\b/.test(tLower)) setEmotion("analyzing");
      else setEmotion("calm");

      // speak response
      setSpeaking(true);
      speak(res.text || "Done.", () => {
        setSpeaking(false);
        setEmotion("calm");
        // resume wake listener after response finishes
        try {
          wakeRecognizer.current?.start();
        } catch {}
        isHandlingCommand.current = false;
      });

      // perform actions if present
      if (Array.isArray(res.actions) && res.actions.length) {
        for (const a of res.actions) {
          addLog("action", `${a.type} ${a.value || a.url || ""}`);
          if (a.type === "open_url" && (a.value || a.url)) {
            window.open(a.value || a.url, "_blank");
          }
          // Additional action types can be added here
        }
      }
    } catch (err) {
      addLog("error", err?.message || String(err));
      setEmotion("critical");
      speak("I encountered an error contacting the server.", () => {
        setEmotion("calm");
        try {
          wakeRecognizer.current?.start();
        } catch {}
        isHandlingCommand.current = false;
      });
    }
  };

  // --- Neural Filament Canvas: draws connecting filaments between points and glows based on emotion/volume
  useEffect(() => {
    const canvas = document.getElementById("filamentCanvas");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    let WIDTH = (canvas.width = window.innerWidth);
    let HEIGHT = (canvas.height = window.innerHeight);
    const devicePixelRatio = window.devicePixelRatio || 1;
    canvas.width = Math.floor(WIDTH * devicePixelRatio);
    canvas.height = Math.floor(HEIGHT * devicePixelRatio);
    canvas.style.width = WIDTH + "px";
    canvas.style.height = HEIGHT + "px";
    ctx.scale(devicePixelRatio, devicePixelRatio);

    // particle nodes distributed in a circle around center (reactor)
    const center = { x: WIDTH / 2, y: HEIGHT / 2 };
    const nodeCount = Math.max(8, Math.floor(Math.min(WIDTH, HEIGHT) / 60));
    let nodes = [];

    const initNodes = () => {
      nodes = [];
      const baseRadius = Math.min(WIDTH, HEIGHT) * 0.18;
      for (let i = 0; i < nodeCount; i++) {
        const angle = (i / nodeCount) * Math.PI * 2;
        const r = baseRadius * (0.8 + Math.random() * 0.6);
        nodes.push({
          baseAngle: angle,
          angle,
          r,
          x: center.x + Math.cos(angle) * r,
          y: center.y + Math.sin(angle) * r,
          vx: 0,
          vy: 0,
          twist: Math.random() * 0.9 + 0.1,
          phase: Math.random() * Math.PI * 2
        });
      }
    };

    initNodes();

    let t = 0;
    const colorForEmotion = () => {
      switch (emotion) {
        case "calm":
          return { r: 8, g: 200, b: 220, a: 0.18 };
        case "analyzing":
          return { r: 220, g: 190, b: 40, a: 0.22 };
        case "action":
          return { r: 90, g: 255, b: 130, a: 0.22 };
        case "critical":
          return { r: 255, g: 80, b: 90, a: 0.26 };
        default:
          return { r: 8, g: 200, b: 220, a: 0.18 };
      }
    };

    function drawFilaments() {
      t += 0.016;
      ctx.clearRect(0, 0, WIDTH, HEIGHT);

      // slight background glow based on emotion & wakePulse
      const emo = colorForEmotion();
      const glowAlpha = emo.a + (wakePulse ? 0.08 : 0) + Math.min(0.22, volume * 0.5);
      ctx.fillStyle = `rgba(${emo.r},${emo.g},${emo.b},${Math.max(0.02, glowAlpha * 0.12)})`;
      ctx.fillRect(0, 0, WIDTH, HEIGHT);

      // update nodes positions (twisting and snake-like)
      for (let i = 0; i < nodes.length; i++) {
        const n = nodes[i];
        // breathing motion + twist effect + volume push
        const tw = Math.sin(t * 0.6 + n.phase) * 0.4 * n.twist;
        const volPush = 1 + volume * 0.8;
        n.angle = n.baseAngle + tw * (transformState === "twist" ? 2.2 : 1.0);
        const radius = n.r * (transformState === "expand" ? 1.25 : transformState === "contract" ? 0.7 : 1) * volPush;
        n.x = center.x + Math.cos(n.angle + t * 0.06) * radius;
        n.y = center.y + Math.sin(n.angle + t * 0.06) * radius;
      }

      // draw filaments - curved bezier connections between nodes (snake-like)
      ctx.lineWidth = 1 + Math.min(2.2, 1.2 + volume * 2);
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        const b = nodes[(i + 1) % nodes.length];
        const midx = (a.x + b.x) / 2 + Math.sin(t * 1.2 + i) * 8 * (volume + 0.05);
        const midy = (a.y + b.y) / 2 + Math.cos(t * 1.3 + i) * 8 * (volume + 0.05);
        const grad = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
        grad.addColorStop(0, `rgba(${emo.r},${emo.g},${emo.b},${0.85 * (0.6 + volume)})`);
        grad.addColorStop(1, `rgba(255,255,255,${0.08 + volume * 0.18})`);

        ctx.strokeStyle = grad;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.quadraticCurveTo(midx, midy, b.x, b.y);
        ctx.stroke();
      }

      // central glow halo
      const haloRadius = Math.min(WIDTH, HEIGHT) * (0.08 + 0.02 * volume);
      const haloGrad = ctx.createRadialGradient(center.x, center.y, haloRadius * 0.1, center.x, center.y, haloRadius * 1.6);
      haloGrad.addColorStop(0, `rgba(${emo.r},${emo.g},${emo.b},${0.24 + volume * 0.5})`);
      haloGrad.addColorStop(1, `rgba(0,0,0,0)`);
      ctx.fillStyle = haloGrad;
      ctx.beginPath();
      ctx.arc(center.x, center.y, haloRadius * 1.6, 0, Math.PI * 2);
      ctx.fill();

      filamentRAF.current = requestAnimationFrame(drawFilaments);
    }

    drawFilaments();

    const onResize = () => {
      WIDTH = canvas.width = window.innerWidth;
      HEIGHT = canvas.height = window.innerHeight;
      canvas.style.width = WIDTH + "px";
      canvas.style.height = HEIGHT + "px";
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.scale(devicePixelRatio, devicePixelRatio);
      center.x = WIDTH / 2;
      center.y = HEIGHT / 2;
      initNodes();
    };
    window.addEventListener("resize", onResize);

    return () => {
      cancelAnimationFrame(filamentRAF.current);
      window.removeEventListener("resize", onResize);
    };
    // re-render filaments when emotion, wakePulse, volume, transformState changes
  }, [emotion, wakePulse, volume, transformState]);

  // --- Simple HUD color overlay (emotional glow) ---
  const emotionColorStyle = () => {
    switch (emotion) {
      case "calm":
        return { boxShadow: `0 0 ${24 + volume * 40}px rgba(6,230,230,${0.16 + volume * 0.3})` };
      case "analyzing":
        return { boxShadow: `0 0 ${28 + volume * 40}px rgba(230,200,40,${0.16 + volume * 0.3})` };
      case "action":
        return { boxShadow: `0 0 ${28 + volume * 40}px rgba(100,255,150,${0.16 + volume * 0.3})` };
      case "critical":
        return { boxShadow: `0 0 ${36 + volume * 40}px rgba(255,80,90,${0.22 + volume * 0.35})` };
      default:
        return {};
    }
  };

  // --- Render ---
  return (
    <div className="jarvis-root">
      {/* filament background canvas */}
      <canvas id="filamentCanvas" style={{ position: "fixed", inset: 0, zIndex: 1, pointerEvents: "none" }} />

      {/* emotional overlay for center area / reactor */}
      <div
        className="hud-overlay"
        style={{
          position: "fixed",
          inset: 0,
          zIndex: 5,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          pointerEvents: "none"
        }}
      >
        <div
          className="reactor-shell"
          style={{
            width: "min(36vmin, 420px)",
            height: "min(36vmin, 420px)",
            borderRadius: "50%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            transition: "transform 420ms ease, box-shadow 320ms ease",
            transform: transformState === "expand" ? "scale(1.08)" : transformState === "contract" ? "scale(0.88)" : "scale(1)",
            ...emotionColorStyle(),
            pointerEvents: "none"
          }}
        >
          {/* ArcReactor component receives props to animate itself */}
          <div style={{ pointerEvents: "none", zIndex: 6 }}>
            <ArcReactor
              active={listening || speaking}
              wakePulse={wakePulse}
              emotion={emotion}
              volume={volume}
              transformState={transformState}
            />
          </div>
        </div>
      </div>

      {/* top-left HUD logs */}
      <div style={{ position: "fixed", left: 20, top: 20, zIndex: 10 }}>
        <HUDLogs logs={logs} />
      </div>

      {/* bottom-center status + mic prompt (interaction area) */}
      <div style={{
        position: "fixed",
        bottom: 30,
        left: 0,
        right: 0,
        display: "flex",
        justifyContent: "center",
        zIndex: 12,
        pointerEvents: "none"
      }}>
        <div style={{
          pointerEvents: "auto",
          background: "rgba(10,10,12,0.35)",
          color: "white",
          padding: "10px 18px",
          borderRadius: 999,
          backdropFilter: "blur(6px)",
          display: "flex",
          alignItems: "center",
          gap: 12,
          minWidth: 260,
          justifyContent: "center",
          boxShadow: "inset 0 0 20px rgba(255,255,255,0.02)"
        }}>
          <div style={{
            width: 12, height: 12, borderRadius: 6,
            background: listening ? "#00ffc8" : wakePulse ? "#00d4ff" : speaking ? "#ffb86b" : "#6b7280",
            boxShadow: listening ? "0 0 12px rgba(0,255,200,0.6)" : wakePulse ? "0 0 10px rgba(0,212,255,0.45)" : ""
          }} />
          <div style={{ fontFamily: "Inter, system-ui, sans-serif", fontSize: 14 }}>
            {listening ? "Listening..." : speaking ? "Responding..." : "Say 'Hey Jarvis' to begin"}
          </div>
        </div>
      </div>
    </div>
  );
}
