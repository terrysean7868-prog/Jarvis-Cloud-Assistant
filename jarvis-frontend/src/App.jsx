// src/App.jsx
import React, { useState, useEffect, useRef } from "react";
import ArcReactor from "./components/ArcReactor";
import HUDLogs from "./components/HUDLogs";
import { listenOnce, speak } from "./utils/speech";
import { sendMessage } from "./utils/api";
import "./styles/jarvis.css";

export default function App() {
  // === STATES ===
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const [wakePulse, setWakePulse] = useState(false);
  const [logs, setLogs] = useState([]);
  const [emotion, setEmotion] = useState("calm");
  const [volume, setVolume] = useState(0);
  const [transformState, setTransformState] = useState("normal");
  const wakeRecognizer = useRef(null);

  // === LOGGING ===
  const addLog = (type, message) => {
    setLogs((prev) => [
      { type, message, time: new Date().toLocaleTimeString() },
      ...prev.slice(0, 8),
    ]);
  };

  // === MICROPHONE ANALYZER (AMPLITUDE) ===
  useEffect(() => {
    let audioCtx, analyser, dataArray, micStream;
    const initMic = async () => {
      try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const source = audioCtx.createMediaStreamSource(micStream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        source.connect(analyser);
        dataArray = new Uint8Array(analyser.frequencyBinCount);

        const update = () => {
          analyser.getByteFrequencyData(dataArray);
          const avg = dataArray.reduce((a, b) => a + b) / dataArray.length / 255;
          setVolume(avg);
          requestAnimationFrame(update);
        };
        update();
      } catch (err) {
        console.warn("Mic analyzer unavailable", err);
      }
    };
    initMic();
    return () => micStream?.getTracks().forEach((t) => t.stop());
  }, []);

  // === CONTINUOUS WAKE WORD LISTENER ===
  useEffect(() => {
  const SpeechRecognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    console.warn("SpeechRecognition not supported.");
    return;
  }

  const recognizer = new SpeechRecognition();
  recognizer.continuous = true;
  recognizer.interimResults = false;
  recognizer.lang = "en-US";

  let isListening = false;
  let restartTimer = null;

  const safeStart = () => {
    if (!isListening) {
      try {
        recognizer.start();
        isListening = true;
        console.debug("Wake listener started ✅");
      } catch (err) {
        if (err.name === "InvalidStateError") {
          // Already started — skip this one silently
        } else {
          console.warn("SpeechRecognition start error:", err);
        }
      }
    }
  };

  const safeStop = () => {
    try {
      recognizer.stop();
      isListening = false;
    } catch (err) {
      console.warn("Safe stop failed:", err);
    }
  };

  recognizer.onstart = () => (isListening = true);

  recognizer.onend = () => {
    isListening = false;
    // Restart safely after a short delay
    clearTimeout(restartTimer);
    restartTimer = setTimeout(safeStart, 1000);
  };

  recognizer.onerror = (e) => {
    if (e.error === "no-speech" || e.error === "aborted") {
      // harmless, just restart later
      clearTimeout(restartTimer);
      restartTimer = setTimeout(safeStart, 1500);
    } else {
      console.warn("Wake listener error:", e.error);
      clearTimeout(restartTimer);
      restartTimer = setTimeout(safeStart, 2000);
    }
  };

  recognizer.onresult = (event) => {
    const transcript = event.results[event.resultIndex][0].transcript
      .trim()
      .toLowerCase();
    if (transcript.includes("hey jarvis") || transcript.includes("ok jarvis")) {
      addLog("wake", "Wake word detected 🔊");
      setWakePulse(true);
      setTimeout(() => setWakePulse(false), 1000);
      handleVoiceCommand();
    }
  };

  // Start listener safely
  safeStart();
  wakeRecognizer.current = recognizer;
  addLog("system", "Wake-word listener activated.");

  return () => {
    clearTimeout(restartTimer);
    safeStop();
  };
}, []);


  // === HANDLE VOICE COMMAND ===
  const handleVoiceCommand = async () => {
    if (listening) return;
    setListening(true);
    addLog("system", "Listening for command…");

    const transcript = await listenOnce();
    setListening(false);

    if (!transcript) {
      addLog("error", "No speech detected.");
      return;
    }

    addLog("input", transcript);
    setEmotion("analyzing");

    // Transformation commands (local control)
    if (/twist|vortex/i.test(transcript)) {
      setTransformState("twist");
      addLog("action", "Twisting reactor geometry.");
      speak("Twisting core geometry, sir.");
      return;
    }
    if (/expand/i.test(transcript)) {
      setTransformState("expand");
      addLog("action", "Expanding reactor core.");
      speak("Expanding energy field.");
      return;
    }
    if (/contract|shrink/i.test(transcript)) {
      setTransformState("contract");
      addLog("action", "Contracting reactor.");
      speak("Contracting core field.");
      return;
    }
    if (/normal|reset/i.test(transcript)) {
      setTransformState("normal");
      addLog("action", "Returning reactor to stable form.");
      speak("Returning to default structure.");
      return;
    }

    try {
      const res = await sendMessage(transcript);
      addLog("response", res.text);

      // Emotion-driven state change
      if (res.text.match(/error|critical|alert/i)) setEmotion("critical");
      else if (res.text.match(/action|launch|open/i)) setEmotion("action");
      else setEmotion("calm");

      setSpeaking(true);
      speak(res.text, () => {
        setSpeaking(false);
        setEmotion("calm");
      });

      if (res.actions?.length) {
        for (const a of res.actions) {
          addLog("action", `${a.type} → ${a.value || ""}`);
          if (a.type === "open_url") window.open(a.value, "_blank");
        }
      }
    } catch (err) {
      addLog("error", err.message);
      speak("Connection issue, sir.");
      setEmotion("critical");
      setTimeout(() => setEmotion("calm"), 2000);
    }
  };

  // === BACKGROUND PARTICLES ===
  useEffect(() => {
    const canvas = document.getElementById("holoParticles");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    const resize = () => {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    };
    resize();
    window.addEventListener("resize", resize);

    const density =
      window.innerWidth < 600 ? 40 : window.innerWidth < 1024 ? 70 : 100;
    const particles = Array.from({ length: density }, () => ({
      x: Math.random() * canvas.width,
      y: Math.random() * canvas.height,
      z: Math.random() * 2 + 0.5,
    }));

    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      for (let p of particles) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.z, 0, Math.PI * 2);
        ctx.fillStyle = "rgba(0,255,255,0.25)";
        ctx.fill();
        p.y += p.z * 0.3;
        if (p.y > canvas.height) p.y = 0;
      }
      requestAnimationFrame(draw);
    };
    draw();

    return () => window.removeEventListener("resize", resize);
  }, []);

  // === RENDER ===
  return (
    <div className="jarvis-container">
      <canvas id="holoParticles"></canvas>
      <ArcReactor
        active={listening || speaking}
        wakePulse={wakePulse}
        emotion={emotion}
        volume={volume}
        transformState={transformState}
      />
      <HUDLogs logs={logs} />
      <div className="hud-text">
        {listening
          ? "Listening..."
          : speaking
          ? "Responding..."
          : "Say 'Hey Jarvis' to begin"}
      </div>
    </div>
  );
}
