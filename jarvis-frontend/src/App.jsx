import React, { useEffect, useRef, useState } from "react";
import { PorcupineWorker } from "@picovoice/porcupine-web";
import { WebVoiceProcessor } from "@picovoice/web-voice-processor";
import "./App.css";

export default function App() {
  const [conversation, setConversation] = useState([
    { from: "jarvis", text: "System online. Say 'Jarvis' to begin." },
  ]);
  const [interimText, setInterimText] = useState("");
  const [reactorPulse, setReactorPulse] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const recognitionRef = useRef(null);
  const wakeWordDetectedRef = useRef(false);

  // ---------- Backend logic ----------
  const sendToBackend = async (text) => {
    setInterimText("");
    wakeWordDetectedRef.current = false;

    const apiEndpoint =
      process.env.REACT_APP_API_URL || "http://localhost:8000/api/chat";

    try {
      const res = await fetch(apiEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, mode: "chat", user: "user" }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const reply = data?.text || "I didn’t quite catch that.";
      setConversation((c) => [...c, { from: "jarvis", text: reply }]);
      speak(reply);
    } catch (err) {
      console.error("Backend error:", err);
      const msg = "Connection issue detected, staying online.";
      setConversation((c) => [...c, { from: "jarvis", text: msg }]);
      speak(msg);
    }
  };

  // ---------- Wakeword (Porcupine v3) ----------
  useEffect(() => {
    let porcupineWorker = null;
    let mounted = true;

    async function initWakeword() {
      try {
        const accessKey = process.env.REACT_APP_PICOVOICE_KEY;
        if (!accessKey) throw new Error("Missing REACT_APP_PICOVOICE_KEY");

        porcupineWorker = await PorcupineWorker.create(
          accessKey,
          [{ builtin: "jarvis", sensitivity: 0.7 }],
          {
            processCallback: (keywordIndex) => {
              if (!mounted) return;
              if (keywordIndex >= 0) {
                console.log("Wakeword detected: Jarvis");
                playBeep();
                wakeWordDetectedRef.current = true;
                setReactorPulse(true);
                setTimeout(() => setReactorPulse(false), 2000);
                setConversation((c) => [
                  ...c,
                  { from: "jarvis", text: "Yes sir, I’m listening..." },
                ]);
                speak("Yes sir, I’m listening...");
              }
            },
          }
        );

        await WebVoiceProcessor.subscribe(porcupineWorker);
        await WebVoiceProcessor.start();
        console.log("✅ Porcupine wakeword ready (v3.0.3)");
      } catch (err) {
        console.error("Wakeword error", err);
      }
    }

    initWakeword();

    return () => {
      mounted = false;
      try {
        const wvp = WebVoiceProcessor.getInstance();
        if (wvp && typeof wvp.stop === "function") wvp.stop();
      } catch {}
      try {
        porcupineWorker?.release?.();
      } catch {}
    };
  }, []);

  // ---------- Speech recognition ----------
  useEffect(() => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return;
    const rec = new SR();
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;

    rec.onresult = (e) => {
      let final = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) final += r[0].transcript;
        else setInterimText(r[0].transcript);
      }
      if (final.trim() && wakeWordDetectedRef.current) {
        wakeWordDetectedRef.current = false;
        setConversation((c) => [...c, { from: "you", text: final }]);
        sendToBackend(final);
      }
      setInterimText("");
    };

    rec.onend = () => {
      try {
        rec.start();
      } catch {}
    };

    navigator.mediaDevices
      .getUserMedia({ audio: true })
      .then(() => rec.start())
      .catch(() => console.warn("Microphone error"));
    recognitionRef.current = rec;

    return () => {
      try {
        rec.stop();
      } catch {}
    };
  }, [sendToBackend]);

  // ---------- Speech synthesis ----------
  const speak = (text) => {
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 0.95;
    u.lang = "en-US";
    u.onstart = () => setSpeaking(true);
    u.onend = () => setSpeaking(false);
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(u);
  };

  const playBeep = () => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g);
    g.connect(ctx.destination);
    o.frequency.value = 900;
    o.type = "sine";
    g.gain.value = 0.2;
    o.start();
    o.stop(ctx.currentTime + 0.15);
  };

  // ---------- UI ----------
  return (
    <div className="app-root">
      <div className="card">
        <div className={`suit3d ${speaking ? "speaking" : ""}`}>
          <div className={`reactor ${reactorPulse ? "pulsing" : ""}`}>
            <div className="reactor-core"></div>
            <div className="reactor-ring"></div>
          </div>
        </div>

        <h1 className="title">JARVIS</h1>

        <div className="chat">
          {conversation.map((m, i) => (
            <div key={i} className={`msg ${m.from}`}>
              <div className="who">{m.from.toUpperCase()}</div>
              <div className="bubble">{m.text}</div>
            </div>
          ))}
          {interimText && (
            <div className="msg you interim">
              <div className="bubble">{interimText}</div>
            </div>
          )}
        </div>

        <div className="hint">
          🎤 Say <b>"Jarvis"</b> to wake me — always listening.
        </div>
      </div>
    </div>
  );
}
