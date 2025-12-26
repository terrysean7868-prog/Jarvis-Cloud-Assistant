// src/components/AuthModal.jsx
import React, { useState, useEffect, useRef } from "react";
import { listenOnce, speak } from "../utils/speech";
import { API_URL } from "../utils/api";
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
        // Mobile-friendly audio constraints
        const audioConstraints = {
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            sampleRate: { ideal: 16000 }
          }
        };
        
        const stream = await navigator.mediaDevices.getUserMedia(audioConstraints);
        
        // Use webkitAudioContext for Safari compatibility
        const AudioContext = window.AudioContext || window.webkitAudioContext;
        audioContextRef.current = new AudioContext();
        
        const source = audioContextRef.current.createMediaStreamSource(stream);
        const analyser = audioContextRef.current.createAnalyser();
        analyser.fftSize = 2048;
        source.connect(analyser);
        
        // Also create a recorder for actual audio data
        const mediaRecorder = new (window.MediaRecorder || window.webkitMediaRecorder)(stream);
        const chunks = [];
        
        mediaRecorder.ondataavailable = (e) => {
          chunks.push(e.data);
        };
        
        mediaRecorder.onstop = () => {
          // Audio chunks are stored but we use frequency analysis for now
        };
        
        audioDataRef.current = { stream, analyser, mediaRecorder, chunks };
      } catch (err) {
        console.error("Audio init failed:", err);
        
        // Provide specific error messages for different scenarios
        let errorMsg = "Microphone access required for voice authentication";
        if (err.name === "NotAllowedError") {
          errorMsg = "Please allow microphone access in your browser settings";
        } else if (err.name === "NotFoundError") {
          errorMsg = "No microphone found on this device";
        } else if (err.name === "SecurityError") {
          errorMsg = "Microphone access not allowed (HTTPS required on some browsers)";
        }
        
        setError(errorMsg);
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

  const createTextHash = async (text) => {
    const normalized = String(text || "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
    const encoder = new TextEncoder();
    const data = encoder.encode(normalized);
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
    setStatus("Recording voice sample... Say the same short phrase you will use for both register and login.");

    try {
      const { analyser, mediaRecorder } = audioDataRef.current;
      const bufferLength = analyser.frequencyBinCount;
      const dataArray = new Uint8Array(bufferLength);
      
      const samples = [];
      const duration = 3000; // 3 seconds
      const startTime = Date.now();
      
      // Start speech recognition (more stable across sessions than hashing raw audio)
      const transcriptPromise = listenOnce({ timeout: 3500, interim: false, continuous: false });

      // Start MediaRecorder for actual audio capture
      if (mediaRecorder && mediaRecorder.state === 'inactive') {
        mediaRecorder.start();
      }

      return new Promise((resolve) => {
        const collectSample = () => {
          if (Date.now() - startTime < duration) {
            analyser.getByteFrequencyData(dataArray);
            samples.push(Array.from(dataArray));
            // Use requestAnimationFrame for smoother collection on mobile
            requestAnimationFrame(collectSample);
          } else {
            // Stop the recorder
            if (mediaRecorder && mediaRecorder.state === 'recording') {
              mediaRecorder.stop();
            }
            
            setIsRecording(false);
            setStatus("Voice sample recorded");
            
            (async () => {
              try {
                const transcript = await transcriptPromise;
                if (transcript && transcript.trim()) {
                  const hash = await createTextHash(transcript);
                  resolve({ voice_hash: hash, voice_text: transcript });
                  return;
                }
              } catch (e) {
                // ignore and fall back
              }

              // Fallback: hash frequency samples
              const combined = samples.flat().join('');
              try {
                const encoder = new TextEncoder();
                const data = encoder.encode(combined);
                const hashBuffer = await crypto.subtle.digest('SHA-256', data);
                const hashArray = Array.from(new Uint8Array(hashBuffer));
                const hash = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
                resolve({ voice_hash: hash, voice_text: null });
              } catch (err) {
                console.error("Hash creation failed:", err);
                resolve({ voice_hash: btoa(combined).substring(0, 64), voice_text: null });
              }
            })();
          }
        };
        collectSample();
      });
    } catch (err) {
      setIsRecording(false);
      console.error("Voice recording error:", err);
      setError("Failed to record voice sample: " + err.message);
      return null;
    }
  };

  const handleAuth = async () => {
    if (!username.trim()) {
      setError("Please enter a username");
      return;
    }

    setError("");
    setStatus("Recording voice sample... Use the same phrase you used when registering.");

    const voiceSample = await recordVoiceSample();
    if (!voiceSample || !voiceSample.voice_hash) {
      return;
    }

    try {
      const isLocalApi = /^https?:\/\/localhost(?::\d+)?$/i.test(API_URL) || /^https?:\/\/127\.0\.0\.1(?::\d+)?$/i.test(API_URL);
      const role = (mode === 'register' && isLocalApi && username.trim().toLowerCase() === 'admin') ? 'admin' : undefined;

      const response = await fetch(`${API_URL}/api/voice-auth`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: username.trim(),
          voice_sample_hash: voiceSample.voice_hash,
          voice_sample_text: voiceSample.voice_text,
          action: mode,
          ...(role ? { role } : {})
        })
      });

      const data = await response.json();

      if (data.status === "success") {
        setStatus("Authentication successful!");
        speak("Authentication successful. Welcome back.", () => {
          if (onAuthSuccess) {
            onAuthSuccess(
              data.session_id,
              data.username || username.trim(),
              data.role,
              data.permissions
            );
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
          <p>Tip: speak the same short phrase for both registration and login.</p>
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
            <p>
              {status || (mode === "login" ? "Click to login with your voice (use the same phrase as registration)" : "Click to register your voice (pick a short phrase and reuse it for login)")}
            </p>
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

