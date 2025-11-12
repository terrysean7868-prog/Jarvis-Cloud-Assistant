import React, { useEffect, useRef, useState, useCallback } from "react";
import "./App.css";
import "./reactor.css";
import DottedRings from "./components/DottedRings";

export default function App() {
  const [status, setStatus] = useState("initializing");
  const [conversation, setConversation] = useState([
    { from: "jarvis", text: "Good morning. I am JARVIS. Always at your service. Say 'Hey Jarvis' to begin." },
  ]);
  const [interimText, setInterimText] = useState("");
  const [reactorEnergy, setReactorEnergy] = useState(0.1);
  const [isProcessing, setIsProcessing] = useState(false);

  const recognitionRef = useRef(null);
  const audioStreamRef = useRef(null);
  const workerRef = useRef(null);
  const wakeWordDetectedRef = useRef(false);
  const lastHotwordTrigger = useRef(0);

  // --- Backend communication ---
  const sendToBackend = useCallback(async (text) => {
    setIsProcessing(true);
    setStatus("thinking");
    setInterimText("");
    wakeWordDetectedRef.current = false;

    const apiEndpoint = process.env.REACT_APP_API_URL || "/api/chat";

    try {
      const res = await fetch(apiEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, mode: "chat", user: "user" }),
      });

      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const reply = data?.text || "I didn’t quite catch that, sir.";

      setConversation((c) => [...c, { from: "jarvis", text: reply }]);
      speak(reply);
    } catch (err) {
      console.error("Backend error:", err);
      const msg = "I encountered a connection problem, but I’m still here.";
      setConversation((c) => [...c, { from: "jarvis", text: msg }]);
      speak(msg);
    } finally {
      setIsProcessing(false);
      setStatus("listening");
      restartRecognition(250);
    }
  }, []);

  const restartRecognition = useCallback((delay = 150) => {
    try {
      recognitionRef.current?.stop();
    } catch {}
    setTimeout(() => {
      try {
        recognitionRef.current?.start();
      } catch {}
    }, delay);
  }, []);

  // --- Hotword detection worker ---
  const createHotwordWorker = useCallback(() => {
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
          postMessage({ event: 'energy', energy });
        }
      };
    `;
    return new Worker(URL.createObjectURL(new Blob([code], { type: "application/javascript" })));
  }, []);

  // --- Speech Recognition setup ---
  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setConversation((c) => [
        ...c,
        { from: "jarvis", text: "SpeechRecognition not supported. Try Chrome or Edge." },
      ]);
      return;
    }

    const rec = new SpeechRecognition();
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    recognitionRef.current = rec;

    rec.onstart = () => setStatus("listening");
    rec.onerror = (e) => {
      if (e.error === "aborted") return;
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

    const worker = createHotwordWorker();
    workerRef.current = worker;

    worker.onmessage = (ev) => {
      const { event, energy } = ev.data;
      if (event === "energy") setReactorEnergy(Math.min(energy * 8, 1));
      if (event === "voice") {
        const now = Date.now();
        if (now - lastHotwordTrigger.current > 600) {
          lastHotwordTrigger.current = now;
          try {
            recognitionRef.current?.start();
          } catch {}
        }
      }
    };

    navigator.mediaDevices
      .getUserMedia({ audio: { noiseSuppression: true, echoCancellation: true } })
      .then(async (stream) => {
        audioStreamRef.current = stream;
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const src = ctx.createMediaStreamSource(stream);

        try {
          const workletCode = `
            class VADProcessor extends AudioWorkletProcessor {
              process(inputs) {
                const input = inputs[0][0];
                if (input) {
                  const buf = new Float32Array(input);
                  this.port.postMessage(buf.buffer, [buf.buffer]);
                }
                return true;
              }
            }
            registerProcessor('vad-processor', VADProcessor);
          `;
          const blob = new Blob([workletCode], { type: "application/javascript" });
          const url = URL.createObjectURL(blob);
          await ctx.audioWorklet.addModule(url);

          const node = new AudioWorkletNode(ctx, "vad-processor");
          node.port.onmessage = (e) => {
            worker.postMessage({ type: "analyze", payload: e.data }, [e.data]);
          };
          src.connect(node);
          node.connect(ctx.destination);
        } catch (err) {
          console.warn("AudioWorklet unavailable, fallback to ScriptProcessor:", err);
          const proc = ctx.createScriptProcessor(2048, 1, 1);
          proc.onaudioprocess = (e) => {
            const input = e.inputBuffer.getChannelData(0);
            worker.postMessage({ type: "analyze", payload: input.buffer }, [input.buffer.slice(0)]);
          };
          src.connect(proc);
          proc.connect(ctx.destination);
        }

        rec.start();
      })
      .catch(() => {
        setConversation((c) => [...c, { from: "jarvis", text: "Microphone permission denied." }]);
      });

    return () => {
      recognitionRef.current?.stop?.();
      audioStreamRef.current?.getTracks?.().forEach((t) => t.stop());
      workerRef.current?.terminate?.();
    };
  }, [createHotwordWorker, restartRecognition, sendToBackend]);

  const playActivationSound = useCallback(() => {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.connect(g);
    g.connect(ctx.destination);
    o.frequency.value = 880;
    o.type = "sine";
    g.gain.value = 0.25;
    o.start();
    o.stop(ctx.currentTime + 0.12);
  }, []);

  const speak = useCallback(
    (text) => {
      const u = new SpeechSynthesisUtterance(text);
      u.lang = "en-US";
      u.rate = 0.95;
      window.speechSynthesis.cancel();
      u.onend = () => restartRecognition(120);
      window.speechSynthesis.speak(u);
    },
    [restartRecognition]
  );

  const getStatusColor = useCallback(() => {
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
  }, [status]);

  return (
    <div className={`app-root ${status === "listening" ? "listening" : ""}`}>
      <header className="app-header">
        <h1 className="title">JARVIS</h1>
        <div className="status">
          <span className="status-dot" style={{ background: getStatusColor() }} />
          <span>{status}</span>
        </div>
      </header>

      <DottedRings audioLevel={reactorEnergy} status={status} />

      <div className="conversation">
        {conversation.map((m, i) => (
          <div key={i} className={`message ${m.from}`}>
            {m.text}
          </div>
        ))}
        {interimText && (
          <div className="message interim">
            {interimText}
          </div>
        )}
        {isProcessing && (
          <div className="message jarvis processing">
            <span className="processing-dots">Processing<span className="dot">.</span><span className="dot">.</span><span className="dot">.</span></span>
          </div>
        )}
      </div>

      <footer className="instructions">
        <div className="mic-status" style={{ color: getStatusColor() }}>
          🎤 {status === "listening" ? "Listening..." : status}
        </div>
        <div className="hint">
          Say <strong>"Hey Jarvis"</strong> to begin
        </div>
      </footer>
    </div>
  );
}
