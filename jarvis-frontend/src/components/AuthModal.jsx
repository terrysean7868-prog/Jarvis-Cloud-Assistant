// src/components/AuthModal.jsx
import React, { useState, useEffect, useRef } from "react";
import { listenOnce, speak, recordPcm16Once } from "../utils/speech";
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
      const ctx = audioContextRef.current;
      audioContextRef.current = null;
      try {
        if (ctx && ctx.state !== "closed") {
          const p = ctx.close();
          // Some browsers return a Promise; ignore rejections.
          if (p && typeof p.then === "function") p.catch(() => {});
        }
      } catch {}
    };
  }, []);

  const createTextHash = async (text) => {
    const normalized = String(text || "")
      .toLowerCase()
      .replace(/\s+/g, " ")
      .trim();
    if (!normalized) return "";
    if (!crypto?.subtle?.digest) {
      throw new Error("SHA-256 is not available in this browser context");
    }
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
      const duration = 5000; // 5 seconds (more reliable speech-to-text)
      const startTime = Date.now();
      
      // Capture a PCM16 sample for biometrics (server-side speaker verification).
      // This is independent from STT/transcript hashing.
      const pcmPromise = recordPcm16Once({
        sampleRateHz: 16000,
        maxMs: 5200,
        silenceStopMs: 1100,
        startRms: 0.012,
        silenceRms: 0.009,
      });

      // Start speech recognition (more stable across sessions than hashing raw audio)
      const transcriptPromise = listenOnce({
        timeout: 12000,
        interim: false,
        continuous: false,
        language: "en-US",
        maxAlternatives: 1
      });

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
                const pcm = await pcmPromise.catch(() => ({ audio_b64: null, sample_rate_hz: 16000 }));
                const transcript = await transcriptPromise;
                if (transcript && transcript.trim()) {
                  const hash = await createTextHash(transcript);
                  resolve({
                    voice_hash: hash,
                    voice_text: transcript,
                    audio_b64: pcm?.audio_b64 || null,
                    sample_rate_hz: pcm?.sample_rate_hz || 16000,
                  });
                  return;
                }
              } catch (e) {
                // ignore and fall back
              }

              // Fallback: hash frequency samples
              const combined = samples.flat().join('');
              try {
                const hash = await createTextHash(combined);
                const pcm = await pcmPromise.catch(() => ({ audio_b64: null, sample_rate_hz: 16000 }));
                resolve({
                  voice_hash: hash,
                  voice_text: null,
                  audio_b64: pcm?.audio_b64 || null,
                  sample_rate_hz: pcm?.sample_rate_hz || 16000,
                });
              } catch (err) {
                console.error("Hash creation failed:", err);
                const pcm = await pcmPromise.catch(() => ({ audio_b64: null, sample_rate_hz: 16000 }));
                resolve({
                  voice_hash: null,
                  voice_text: null,
                  audio_b64: pcm?.audio_b64 || null,
                  sample_rate_hz: pcm?.sample_rate_hz || 16000,
                });
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
      setError("Could not capture a stable voice hash. Please speak clearly and try again.");
      return;
    }

    const hash = String(voiceSample.voice_hash || "").trim().toLowerCase();
    if (!/^[a-f0-9]{64}$/.test(hash)) {
      setError("Invalid voice hash captured. Please retry voice authentication.");
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
          voice_sample_hash: hash,
          voice_sample_text: voiceSample.voice_text,
          audio_b64: voiceSample.audio_b64 || null,
          sample_rate_hz: voiceSample.sample_rate_hz || 16000,
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

