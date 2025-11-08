import React, { useEffect, useRef, useState } from "react";
import "./App.css";

export default function App() {
  const [status, setStatus] = useState("initializing");
  const [conversation, setConversation] = useState([{ from: "jarvis", text: "Good morning. I am JARVIS. Always at your service. Say 'Hey Jarvis' to begin." }]);
  const [interimText, setInterimText] = useState("");
  const recognitionRef = useRef(null);
  const listeningRef = useRef(false);
  const wakeWordDetectedRef = useRef(false);
  const commandBufferRef = useRef("");

  const sendToBackend = async (text) => {
    setStatus("thinking");
    setInterimText("");
    wakeWordDetectedRef.current = false;

    // Determine API endpoint
    // For local dev: use proxy (package.json) or direct localhost
    // For Render/production: use REACT_APP_API_URL
    let apiEndpoint;
    if (process.env.REACT_APP_API_URL) {
      // Production/Render: use explicit URL
      apiEndpoint = `${process.env.REACT_APP_API_URL}/api/chat`;
    } else {
      // Local dev: try proxy first, fallback to direct
      // React proxy will handle /api/* routes automatically
      apiEndpoint = "/api/chat";
    }

    console.log("Sending request to:", apiEndpoint);
    console.log("Backend URL:", process.env.REACT_APP_API_URL || "http://localhost:8000 (proxy)");

    try {
      const res = await fetch(apiEndpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, mode: "chat", user: "user" })
      });

      if (!res.ok) {
        throw new Error(`HTTP error! status: ${res.status}`);
      }

      const data = await res.json();
      const reply = data.text || "I apologize, I didn't understand that.";

      // Clean reply text (remove JSON actions if present)
      const cleanReply = reply.split(/\{[\s\S]*"actions"[\s\S]*\}/)[0].trim() || reply;

      setConversation(c => [...c, { from: "jarvis", text: cleanReply }]);
      speak(cleanReply);

      // Show actions if any were taken
      if (data.actions && data.actions.length > 0) {
        const actionSummary = `✅ Executed ${data.actions.length} action(s)`;
        setConversation(c => [...c, { from: "jarvis", text: actionSummary, type: "system" }]);
      }

    } catch (e) {
      console.error("Backend error:", e);
      console.error("Error details:", e.message);

      // More helpful error message
      let errorMsg = "I'm sorry, I'm having trouble connecting to the server. ";
      if (e.message.includes("Failed to fetch") || e.message.includes("NetworkError")) {
        errorMsg += "Please make sure the backend is running on http://localhost:8000";
      } else {
        errorMsg += `Error: ${e.message}`;
      }

      setConversation(c => [...c, { from: "jarvis", text: errorMsg }]);
      speak(errorMsg);
    } finally {
      setStatus("listening");
    }
  };

  useEffect(() => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      setStatus("no-speech-api");
      setConversation(c => [...c, { from: "jarvis", text: "Error: Your browser does not support the Web Speech API. Please use Chrome or Edge." }]);
      return;
    }

    const rec = new SpeechRecognition();
    rec.lang = "en-US";
    rec.interimResults = true;
    rec.continuous = true;
    rec.maxAlternatives = 3; // Increased for better accuracy

    rec.onstart = () => {
      listeningRef.current = true;
      setStatus("listening");
      console.log("🎤 Voice recognition started");
    };

    rec.onerror = (e) => {
      console.warn("Speech recognition error:", e.error);

      if (e.error === "no-speech" || e.error === "network" || e.error === "aborted") {
        // Typical benign errors – just restart listening after a short delay
        setStatus("idle");
        setTimeout(() => {
          try { rec.start(); } catch (err) {
            console.log("Restart failed:", err);
          }
        }, 1000);
      } else if (e.error === "not-allowed") {
        alert("Microphone access denied. Please allow mic permission and refresh.");
      } else {
        // Unexpected errors
        setStatus("error");
      }
    };


    rec.onresult = (e) => {
      let fullTranscript = "";
      let hasFinal = false;

      // Process all results
      for (let i = e.resultIndex; i < e.results.length; ++i) {
        const result = e.results[i];
        const transcript = result[0].transcript.trim();

        if (result.isFinal) {
          hasFinal = true;
          fullTranscript += " " + transcript;
        } else {
          // Show interim results for better UX
          setInterimText(transcript);
        }
      }

      if (hasFinal && fullTranscript) {
        const text = fullTranscript.trim().toLowerCase();
        commandBufferRef.current += " " + text;

        // Check for wake word variations
        const wakeWords = ["hey jarvis", "hey jarvis", "jarvis", "activate jarvis"];
        let wakeWordFound = null;
        let wakeWordIndex = -1;

        for (const wakeWord of wakeWords) {
          const index = commandBufferRef.current.toLowerCase().indexOf(wakeWord);
          if (index !== -1) {
            wakeWordFound = wakeWord;
            wakeWordIndex = index;
            break;
          }
        }

        if (wakeWordFound) {
          // Wake word detected!
          wakeWordDetectedRef.current = true;
          setStatus("activated");

          // Extract command after wake word
          const afterWakeWord = commandBufferRef.current.substring(wakeWordIndex + wakeWordFound.length).trim();

          if (afterWakeWord) {
            // Play activation sound (optional beep)
            playActivationSound();
            setConversation(c => [...c, { from: "you", text: afterWakeWord }]);
            sendToBackend(afterWakeWord);
            commandBufferRef.current = "";
            wakeWordDetectedRef.current = false;
          } else {
            // Wake word detected but no command yet
            playActivationSound();
            setStatus("ready");
            setConversation(c => [...c, { from: "jarvis", text: "Yes, sir? I'm listening..." }]);
            speak("Yes, sir? I'm listening.");
            commandBufferRef.current = "";
          }
        } else {
          // No wake word, but if we're in activated state, treat as command
          if (wakeWordDetectedRef.current && text.length > 2) {
            sendToBackend(text);
            commandBufferRef.current = "";
            wakeWordDetectedRef.current = false;
          }
        }

        // Keep buffer manageable (last 50 words)
        const words = commandBufferRef.current.split(" ");
        if (words.length > 50) {
          commandBufferRef.current = words.slice(-20).join(" ");
        }

        setInterimText("");
      }
    };

    rec.onend = () => {
      listeningRef.current = false;
      setStatus("idle");
      setInterimText("");

      // Auto-restart for continuous listening
      if (!wakeWordDetectedRef.current) {
        setTimeout(() => {
          try {
            rec.start();
            setStatus("listening");
          } catch (e) {
            console.warn("Failed to restart recognition:", e);
          }
        }, 300);
      }
    };

    recognitionRef.current = rec;

    // Request microphone permission and start
    navigator.mediaDevices.getUserMedia({ audio: true })
      .then(() => {
        try {
          rec.start();
          setStatus("listening");
        } catch (e) {
          console.warn("Start failed:", e);
          setStatus("error");
        }
      })
      .catch((err) => {
        console.error("Microphone permission denied:", err);
        setStatus("permission-denied");
      });

    return () => {
      try {
        rec.stop();
      } catch (e) {
        console.warn("Stop error:", e);
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function playActivationSound() {
    // Create a subtle beep sound
    const audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const oscillator = audioContext.createOscillator();
    const gainNode = audioContext.createGain();

    oscillator.connect(gainNode);
    gainNode.connect(audioContext.destination);

    oscillator.frequency.value = 800;
    oscillator.type = "sine";

    gainNode.gain.setValueAtTime(0.3, audioContext.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.01, audioContext.currentTime + 0.1);

    oscillator.start(audioContext.currentTime);
    oscillator.stop(audioContext.currentTime + 0.1);
  }

  function speak(text) {
    if (!window.speechSynthesis) {
      console.warn("Speech synthesis not available");
      return;
    }

    // Cancel any ongoing speech
    window.speechSynthesis.cancel();

    // Clean text (remove markdown, code blocks, JSON, etc.)
    let cleanText = text
      .replace(/```[\s\S]*?```/g, "")  // Remove code blocks
      .replace(/`[^`]+`/g, "")          // Remove inline code
      .replace(/\{[\s\S]*"actions"[\s\S]*\}/g, "")  // Remove JSON actions
      .replace(/\{[\s\S]*\}/g, "")      // Remove any remaining JSON
      .replace(/\*\*(.*?)\*\*/g, "$1")  // Remove markdown bold
      .replace(/\*(.*?)\*/g, "$1")      // Remove markdown italic
      .replace(/#{1,6}\s/g, "")         // Remove markdown headers
      .replace(/\[(.*?)\]\(.*?\)/g, "$1") // Remove markdown links
      .replace(/\n+/g, ". ")            // Replace newlines with periods
      .trim();

    if (!cleanText || cleanText.length < 2) {
      console.log("No text to speak after cleaning");
      return;
    }

    // Ensure text ends with punctuation for natural speech
    if (!/[.!?]$/.test(cleanText)) {
      cleanText += ".";
    }

    console.log("Speaking:", cleanText);

    const utterance = new SpeechSynthesisUtterance(cleanText);
    utterance.rate = 0.95;  // Slightly slower for clarity
    utterance.pitch = 1.0;
    utterance.volume = 1.0;
    utterance.lang = "en-US";

    // Try to find a good voice (prefer male, English voices)
    const voices = window.speechSynthesis.getVoices();
    let preferredVoice = voices.find(v =>
      (v.name.toLowerCase().includes("google") && v.lang.startsWith("en")) ||
      (v.name.toLowerCase().includes("microsoft david") && v.lang.startsWith("en")) ||
      (v.name.toLowerCase().includes("microsoft mark") && v.lang.startsWith("en"))
    );

    // Fallback to any English voice
    if (!preferredVoice) {
      preferredVoice = voices.find(v => v.lang.startsWith("en"));
    }

    if (preferredVoice) {
      utterance.voice = preferredVoice;
      console.log("Using voice:", preferredVoice.name);
    }

    // Event handlers
    utterance.onstart = () => {
      console.log("Speech started");
      setStatus("speaking");
    };

    utterance.onend = () => {
      console.log("Speech ended");
      setStatus("listening");
    };

    utterance.onerror = (event) => {
      console.log("Speech error");
      setStatus("listening");
    };

    // Speak with a small delay to ensure previous speech is cancelled
    setTimeout(() => {
      try {
        window.speechSynthesis.speak(utterance);
      } catch (error) {
        console.error("Error speaking:", error);
        setStatus("listening");
      }
    }, 100);
  }

  // Load voices when available
  useEffect(() => {
    if (window.speechSynthesis) {
      const loadVoices = () => {
        window.speechSynthesis.getVoices();
      };
      loadVoices();
      if (speechSynthesis.onvoiceschanged !== undefined) {
        speechSynthesis.onvoiceschanged = loadVoices;
      }
    }
  }, []);

  const getStatusColor = () => {
    switch (status) {
      case "listening": return "#00ffc8"; // cyan green (JARVIS active)
      case "activated": case "ready": return "#00d4ff"; // bright blue (activated)
      case "thinking": return "#ff6b00"; // orange (processing)
      case "speaking": return "#00d4ff"; // blue (speaking)
      case "error": case "no-speech-api": case "permission-denied": return "#ff4444"; // red (error)
      default: return "#6b7280"; // gray (idle)
    }
  };

  const isListeningState = status === "listening" || status === "activated" || status === "ready";

  return (
    <div className={`container ${isListeningState ? 'listening' : ''}`}>
      {/* Listening background circles */}
      {isListeningState && (
        <div className="listening-overlay">
          <div className="listening-circle"></div>
          <div className="listening-circle"></div>
          <div className="listening-circle"></div>
        </div>
      )}

      <div className="header">
        <h1>JARVIS</h1>
        <div className="status-indicator">
          <div
            className="status-dot"
            style={{
              backgroundColor: getStatusColor(),
              color: getStatusColor()
            }}
          />
          <span>{status.replace(/-/g, " ")}</span>
        </div>
      </div>

      <div className="card">
        <div className="messages">
          {conversation.map((m, i) => (
            <div key={i} className={`message ${m.from}`}>
              <div className="message-label">{m.from === "you" ? "USER" : "JARVIS"}</div>
              <div className={`message-bubble ${m.from}`}>
                {m.text}
              </div>
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
          <strong>🎤 Voice Commands</strong>
          <small>Say <strong>"Hey Jarvis"</strong> followed by your command</small>
        </div>
      </div>
    </div>
  );
}
