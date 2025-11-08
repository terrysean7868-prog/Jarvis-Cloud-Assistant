import React, { useEffect, useRef, useState } from "react";
import "./App.css";

export default function App() {
  const [status, setStatus] = useState("initializing");
  const [conversation, setConversation] = useState([
    { from: "jarvis", text: "Good morning. I am JARVIS. Always at your service. Say 'Hey Jarvis' to begin." },
  ]);
  const [interimText, setInterimText] = useState("");
  const recognitionRef = useRef(null);
  const audioStreamRef = useRef(null);
  const workerRef = useRef(null);
  const listeningRef = useRef(false);
  const wakeWordDetectedRef = useRef(false);
  const commandBufferRef = useRef("");
  const lastHotwordTrigger = useRef(0);

  // --- Backend communication ---
  const sendToBackend = async (text) => {
    setStatus("thinking");
    setInterimText("");
    wakeWordDetectedRef.current = false;
    const apiEndpoint =
      process.env.REACT_APP_API_URL || "/api/chat";

    try {
      const res = await fetch(apiEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, mode: "chat", user: "user" }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const reply = data?.text || "I didn’t quite catch that, sir.";
      const cleanReply = reply.split(/\{[\s\S]*"actions"[\s\S]*\}/)[0].trim() || reply;

      setConversation((c) => [...c, { from: "jarvis", text: cleanReply }]);
      speak(cleanReply);
    } catch (err) {
      console.error("Backend error:", err);
      const msg = "I encountered a connection problem, but I’m still here.";
      setConversation((c) => [...c, { from: "jarvis", text: msg }]);
      speak(msg);
      setStatus("listening");
      restartRecognition(250);
    } finally {
      setStatus("listening");
    }
  };

  const restartRecognition = (delay = 150) => {
    try {
      recognitionRef.current?.stop();
    } catch {}
    setTimeout(() => {
      try {
        recognitionRef.current?.start();
      } catch {}
    }, delay);
  };

  // --- Worker (voice activity detection) ---
  const createHotwordWorker = () => {
    const code = `
      let lastVoice = 0;
      function energyFromFloat32(data) {
        let sum = 0;
        for (let i = 0; i < data.length; i += 2) sum += data[i] * data[i];
        return Math.sqrt(sum / (data.length / 2));
      }
      onmessage = function(e) {
        const { type, payload } = e.data;
        if (type === 'analyze') {
          const arr = new Float32Array(payload);
          const energy = energyFromFloat32(arr);
          const now = Date.now();
          if (energy > 0.015) {
            lastVoice = now;
            postMessage({ event: 'voice', energy, time: now });
          }
        }
      };
    `;
    return new Worker(URL.createObjectURL(new Blob([code], { type: "application/javascript" })));
  };

  // --- Recognition + mic init ---
  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setConversation((c) => [...c, { from: "jarvis", text: "SpeechRecognition not supported. Try Chrome." }]);
      return;
    }

    const rec = new SpeechRecognition();
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    recognitionRef.current = rec;

    rec.onstart = () => setStatus("listening");
    rec.onerror = (e) => {
      console.warn("Speech error", e);
      restartRecognition(250);
    };

    rec.onresult = (e) => {
      let full = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) full += r[0].transcript;
        else setInterimText(r[0].transcript);
      }

      const text = full.trim().toLowerCase();
      if (!text) return;

      // detect "jarvis" or "hey jarvis"
      const wakeWords = ["hey jarvis", "jarvis", "ok jarvis"];
      const detected = wakeWords.find((w) => text.includes(w));

      if (detected) {
        wakeWordDetectedRef.current = true;
        playActivationSound();
        setStatus("activated");
        setConversation((c) => [...c, { from: "jarvis", text: "Yes sir, I’m listening..." }]);
        speak("Yes sir, I’m listening...");
        setInterimText("");
        return;
      }

      if (wakeWordDetectedRef.current && text.length > 2) {
        wakeWordDetectedRef.current = false;
        setConversation((c) => [...c, { from: "you", text }]);
        sendToBackend(text);
        setInterimText("");
      }
    };

    rec.onend = () => restartRecognition(100);

    // mic + worker
    const worker = createHotwordWorker();
    workerRef.current = worker;
    worker.onmessage = (ev) => {
      const now = Date.now();
      if (ev.data.event === "voice" && now - lastHotwordTrigger.current > 600) {
        lastHotwordTrigger.current = now;
        try {
          recognitionRef.current?.start();
        } catch {}
      }
    };

    navigator.mediaDevices
      .getUserMedia({ audio: { noiseSuppression: true, echoCancellation: true } })
      .then((stream) => {
        audioStreamRef.current = stream;
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);
        const proc = ctx.createScriptProcessor(2048, 1, 1);
        proc.onaudioprocess = (e) => {
          const input = e.inputBuffer.getChannelData(0);
          worker.postMessage({ type: "analyze", payload: input.buffer }, [input.buffer.slice(0)]);
        };
        src.connect(proc);
        proc.connect(ctx.destination);
        rec.start();
      })
      .catch(() => setConversation((c) => [...c, { from: "jarvis", text: "Microphone permission denied." }]));

    return () => {
      try { recognitionRef.current?.stop(); } catch {}
      try { audioStreamRef.current?.getTracks().forEach((t) => t.stop()); } catch {}
      try { workerRef.current?.terminate(); } catch {}
    };
  }, []);

  const playActivationSound = () => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g); g.connect(ctx.destination);
    o.frequency.value = 880; o.type = "sine"; g.gain.value = 0.25;
    o.start(); o.stop(ctx.currentTime + 0.12);
  };

  const speak = (text) => {
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "en-US";
    u.rate = 0.95;
    window.speechSynthesis.cancel();
    u.onend = () => restartRecognition(120);
    window.speechSynthesis.speak(u);
  };

  const getStatusColor = () => {
    switch (status) {
      case "activated":
      case "speaking":
        return "#00ffc8";
      case "listening":
        return "#00d4ff";
      case "thinking":
        return "#ff9f43";
      default:
        return "#6b7280";
    }
  };

  return (
    <div className={`app-root ${status === "listening" ? "listening" : ""}`}>
      <header className="app-header">
        <h1 className="title">JARVIS</h1>
        <div className="status">
          <span className="status-dot" style={{ background: getStatusColor() }} />
          <span>{status}</span>
        </div>
      </header>

      <div className="reactor-container">
        <div className={`reactor-ring ${status}`} />
        <div className="reactor-core" />
      </div>

      <main className="main-card">
        <section className="messages">
          {conversation.map((m, i) => (
            <div key={i} className={`msg ${m.from}`}>
              <div className="msg-label">{m.from === "you" ? "YOU" : "JARVIS"}</div>
              <div className="msg-bubble">{m.text}</div>
            </div>
          ))}
          {interimText && (
            <div className="msg you interim">
              <div className="msg-label">USER</div>
              <div className="msg-bubble">{interimText}</div>
            </div>
          )}
        </section>
      </main>
      <footer className="instructions">
        🎤 Say <strong>"Hey Jarvis"</strong> — hotword & always listening.
      </footer>
    </div>
  );
}
