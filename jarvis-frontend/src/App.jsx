import React, { useEffect, useRef, useState } from "react";
import { WebVoiceProcessor } from "@picovoice/web-voice-processor";
import { PorcupineWorker } from "@picovoice/porcupine-web";
import "./App.css";

export default function App() {
  const [status, setStatus] = useState("initializing");
  const [conversation, setConversation] = useState([
    { from: "jarvis", text: "Good morning. I am JARVIS. Always at your service. Say 'Jarvis' to begin." },
  ]);
  const [interimText, setInterimText] = useState("");
  const [ringActive, setRingActive] = useState(false);

  const recognitionRef = useRef(null);
  const listeningRef = useRef(false);
  const wakeWordDetectedRef = useRef(false);
  const commandBufferRef = useRef("");

  // ---------- Backend ----------
  const sendToBackend = async (text) => {
    setStatus("thinking");
    setInterimText("");
    wakeWordDetectedRef.current = false;
    const apiEndpoint = process.env.REACT_APP_API_URL
      ? `${process.env.REACT_APP_API_URL}/api/chat`
      : "/api/chat";

    try {
      const res = await fetch(apiEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, mode: "chat", user: "user" }),
      });
      if (!res.ok) throw new Error(`HTTP error ${res.status}`);

      const data = await res.json();
      const reply = data?.text || "I didn’t quite catch that, sir.";
      const cleanReply = reply.split(/\{[\s\S]*"actions"[\s\S]*\}/)[0].trim() || reply;

      setConversation((c) => [...c, { from: "jarvis", text: cleanReply }]);
      speak(cleanReply);
    } catch (err) {
      console.error("Backend error:", err);
      const msg = "I encountered a network issue, but I’m still here.";
      setConversation((c) => [...c, { from: "jarvis", text: msg }]);
      speak(msg);
      setStatus("listening");
      restartRecognition();
    } finally {
      setStatus("listening");
    }
  };

  const restartRecognition = (delay = 250) => {
    try { recognitionRef.current?.stop(); } catch {}
    setTimeout(() => {
      try { recognitionRef.current?.start(); } catch {}
    }, delay);
  };

  // ---------- Wakeword (Porcupine) ----------
  useEffect(() => {
    async function initWakeword() {
      try {
        setStatus("loading wakeword");
        const modelResp = await fetch(process.env.PUBLIC_URL + "/wakeword/jarvis_wasm.ppn");
        const modelBuffer = await modelResp.arrayBuffer();

        const porcupineWorker = await PorcupineWorker.create(
          [
            { custom: modelBuffer, label: "jarvis", sensitivity: 0.6 },
          ],
          (keywordIndex) => {
            if (keywordIndex >= 0) {
              console.log("Wakeword detected: Jarvis");
              playActivationSound();
              wakeWordDetectedRef.current = true;
              setRingActive(true);
              setTimeout(() => setRingActive(false), 2000);
              setStatus("activated");
              setConversation((c) => [...c, { from: "jarvis", text: "Yes sir, I’m listening..." }]);
              speak("Yes sir, I’m listening...");
            }
          }
        );

        await WebVoiceProcessor.subscribe(porcupineWorker);
        await WebVoiceProcessor.start();
        setStatus("listening");
        console.log("Porcupine ready");
      } catch (err) {
        console.error("Wakeword init failed:", err);
        setStatus("error");
      }
    }

    initWakeword();
    return () => { WebVoiceProcessor.stop().catch(() => {}); };
  }, []);

  // ---------- Speech Recognition ----------
  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setConversation((c) => [
        ...c,
        { from: "jarvis", text: "Your browser does not support Web Speech API." },
      ]);
      setStatus("no-speech-api");
      return;
    }

    const rec = new SpeechRecognition();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = true;

    rec.onstart = () => { listeningRef.current = true; setStatus("listening"); };
    rec.onerror = (e) => {
      console.error("Speech error:", e);
      if (["no-speech", "aborted"].includes(e.error)) return;
      setStatus("error");
      restartRecognition();
    };
    rec.onresult = (e) => {
      let finalText = "";
      for (let i = e.resultIndex; i < e.results.length; ++i) {
        const r = e.results[i];
        const t = r[0].transcript.trim();
        if (r.isFinal) finalText += " " + t;
        else setInterimText(t);
      }
      if (finalText.trim()) {
        const text = finalText.toLowerCase();
        commandBufferRef.current += " " + text;
        if (wakeWordDetectedRef.current) {
          wakeWordDetectedRef.current = false;
          setConversation((c) => [...c, { from: "you", text }]);
          sendToBackend(text);
        }
        setInterimText("");
        commandBufferRef.current = commandBufferRef.current.split(" ").slice(-20).join(" ");
      }
    };
    rec.onend = () => { listeningRef.current = false; restartRecognition(); };

    recognitionRef.current = rec;
    navigator.mediaDevices.getUserMedia({
      audio: { noiseSuppression: true, echoCancellation: true, autoGainControl: true },
    }).then(() => rec.start()).catch(() => setStatus("permission-denied"));

    return () => { try { rec.stop(); } catch {} };
  }, []);

  // ---------- Utilities ----------
  const playActivationSound = () => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = "sine"; osc.frequency.value = 880;
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.1);
    osc.start(); osc.stop(ctx.currentTime + 0.1);
  };

  const speak = (text) => {
    if (!window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    const clean = text.replace(/```[\s\S]*?```/g, "").replace(/\n+/g, ". ").trim();
    const u = new SpeechSynthesisUtterance(clean);
    u.lang = "en-US"; u.rate = 0.95;
    u.onstart = () => { setStatus("speaking"); recognitionRef.current?.stop(); };
    u.onend = () => { setStatus("listening"); restartRecognition(); };
    window.speechSynthesis.speak(u);
  };

  const getStatusColor = () => {
    switch (status) {
      case "listening":
      case "activated": return "#00ffc8";
      case "thinking": return "#ff6b00";
      case "speaking": return "#00d4ff";
      case "error":
      case "permission-denied": return "#ff4444";
      default: return "#6b7280";
    }
  };

  const isListeningState = ["listening", "activated"].includes(status);

  // ---------- UI ----------
  return (
    <div className={`container ${isListeningState ? "listening" : ""}`}>
      {ringActive && <div className="neon-ring"></div>}
      <div className="header">
        <h1>JARVIS</h1>
        <div className="status-indicator">
          <div className="status-dot" style={{ backgroundColor: getStatusColor() }} />
          <span>{status.replace(/-/g, " ")}</span>
        </div>
      </div>

      <div className="card">
        <div className="messages">
          {conversation.map((m, i) => (
            <div key={i} className={`message ${m.from}`}>
              <div className="message-label">{m.from === "you" ? "USER" : "JARVIS"}</div>
              <div className={`message-bubble ${m.from}`}>{m.text}</div>
            </div>
          ))}
          {interimText && (
            <div className="message you">
              <div className="message-label">USER (listening...)</div>
              <div className="message-bubble you interim">{interimText}</div>
            </div>
          )}
        </div>
        <div className="instructions">
          <strong>🎤 Wake Word:</strong>
          <small>Say <strong>"Jarvis"</strong> to wake me up — offline & always listening.</small>
        </div>
      </div>
    </div>
  );
}
