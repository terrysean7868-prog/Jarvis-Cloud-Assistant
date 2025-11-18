// src/components/AuthModal.jsx
import React, { useState, useEffect, useRef } from "react";
import { listenOnce, speak } from "../utils/speech";
import { setGitHubConfig } from "../utils/api";
import "./AuthModal.css";

const AuthModal = ({ onAuthSuccess, onClose }) => {
  const [mode, setMode] = useState("login"); // "login" or "register"
  const [username, setUsername] = useState("");
  const [isRecording, setIsRecording] = useState(false);
  const [status, setStatus] = useState("");
  const [error, setError] = useState("");
  const audioContextRef = useRef(null);
  const audioDataRef = useRef(null);

  // Initialize audio for voice sample
  useEffect(() => {
    const initAudio = async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioContextRef.current = new (window.AudioContext || window.webkitAudioContext)();
        const source = audioContextRef.current.createMediaStreamSource(stream);
        const analyser = audioContextRef.current.createAnalyser();
        analyser.fftSize = 2048;
        source.connect(analyser);
        audioDataRef.current = { stream, analyser };
      } catch (err) {
        console.error("Audio init failed:", err);
        setError("Microphone access required for voice authentication");
      }
    };
    initAudio();

    return () => {
      if (audioDataRef.current?.stream) {
        audioDataRef.current.stream.getTracks().forEach(track => track.stop());
      }
      if (audioContextRef.current) {
        audioContextRef.current.close();
      }
    };
  }, []);

  const createVoiceHash = async (audioBuffer) => {
    // Create a simple hash from audio data
    // In production, use proper voice biometrics
    const data = new Uint8Array(audioBuffer.getChannelData(0).buffer);
    const hashBuffer = await crypto.subtle.digest('SHA-256', data);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
  };

  const recordVoiceSample = async () => {
    if (!audioDataRef.current) {
      setError("Audio not initialized");
      return null;
    }

    setIsRecording(true);
    setStatus("Recording voice sample... Speak now.");

    try {
      // Record for 3 seconds
      const analyser = audioDataRef.current.analyser;
      const bufferLength = analyser.frequencyBinCount;
      const dataArray = new Uint8Array(bufferLength);
      
      const samples = [];
      const duration = 3000; // 3 seconds
      const startTime = Date.now();

      return new Promise((resolve) => {
        const collectSample = () => {
          if (Date.now() - startTime < duration) {
            analyser.getByteFrequencyData(dataArray);
            samples.push(Array.from(dataArray));
            requestAnimationFrame(collectSample);
          } else {
            setIsRecording(false);
            setStatus("Voice sample recorded");
            
            // Create hash from samples
            const combined = samples.flat().join('');
            crypto.subtle.digest('SHA-256', new TextEncoder().encode(combined))
              .then(hashBuffer => {
                const hashArray = Array.from(new Uint8Array(hashBuffer));
                const hash = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
                resolve(hash);
              });
          }
        };
        collectSample();
      });
    } catch (err) {
      setIsRecording(false);
      setError("Failed to record voice sample");
      return null;
    }
  };

  const handleAuth = async () => {
    if (!username.trim()) {
      setError("Please enter a username");
      return;
    }

    setError("");
    setStatus("Recording voice sample...");

    const voiceHash = await recordVoiceSample();
    if (!voiceHash) {
      return;
    }

    try {
      const response = await fetch(`${process.env.REACT_APP_API_URL || "http://localhost:8000"}/api/voice-auth`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: username.trim(),
          voice_sample_hash: voiceHash,
          action: mode
        })
      });

      const data = await response.json();

      if (data.status === "success") {
        setStatus("Authentication successful!");
        speak("Authentication successful. Welcome back.", () => {
          if (onAuthSuccess) {
            onAuthSuccess(data.session_id, username.trim());
          }
        });
      } else {
        setError(data.message || "Authentication failed");
        speak("Authentication failed. Please try again.");
      }
    } catch (err) {
      setError("Failed to connect to server");
      console.error("Auth error:", err);
    }
  };

  return (
    <div className="auth-modal-overlay" onClick={onClose}>
      <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
        <button className="auth-close" onClick={onClose}>×</button>
        
        <div className="auth-header">
          <h2>{mode === "login" ? "Voice Login" : "Voice Registration"}</h2>
          <p>Use your voice to authenticate</p>
        </div>

        <div className="auth-content">
          <div className="auth-input-group">
            <label>Username</label>
            <input
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder="Enter your username"
              disabled={isRecording}
            />
          </div>

          <div className="voice-sample-indicator">
            {isRecording ? (
              <div className="recording-pulse">
                <div className="pulse-ring"></div>
                <div className="pulse-ring"></div>
                <div className="pulse-ring"></div>
              </div>
            ) : (
              <div className="mic-icon">🎤</div>
            )}
            <p>{status || (mode === "login" ? "Click to login with your voice" : "Click to register your voice")}</p>
          </div>

          {error && <div className="auth-error">{error}</div>}
          {status && !error && <div className="auth-status">{status}</div>}

          <div className="auth-actions">
            <button
              className="auth-button primary"
              onClick={handleAuth}
              disabled={isRecording || !username.trim()}
            >
              {isRecording ? "Recording..." : (mode === "login" ? "Login" : "Register")}
            </button>
            
            <button
              className="auth-button secondary"
              onClick={() => {
                setMode(mode === "login" ? "register" : "login");
                setError("");
                setStatus("");
              }}
              disabled={isRecording}
            >
              {mode === "login" ? "Need to register?" : "Already registered?"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

export default AuthModal;

