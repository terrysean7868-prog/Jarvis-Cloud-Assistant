// src/utils/speech.js
/**
 * Enhanced voice recognition with noise reduction and better accuracy
 * Uses Web Speech API with improved settings for clearer input
 */

// Audio context for noise reduction
let audioContext = null;
let analyser = null;
let microphone = null;
let processor = null;

function _isIOS() {
  try {
    const ua = (navigator.userAgent || "").toLowerCase();
    return /iphone|ipad|ipod/.test(ua);
  } catch {
    return false;
  }
}

export function speak(text, onEnd) {
  const synth = window.speechSynthesis;
  // Cancel any ongoing speech
  synth.cancel();
  
  const utter = new SpeechSynthesisUtterance(text);
  try {
    const preferred = (typeof window !== "undefined" && window.__jarvisPreferredLang) ? String(window.__jarvisPreferredLang) : "";
    if (preferred) utter.lang = preferred;
  } catch {}
  utter.pitch = 1.1;
  utter.rate = 1.05;
  utter.volume = 1.0;
  utter.onend = onEnd || (() => {});
  utter.onerror = (e) => {
    console.warn("Speech synthesis error:", e);
    if (onEnd) onEnd();
  };
  synth.speak(utter);
}

export function primeSpeechRecognition(language = "en-US") {
  return new Promise((resolve) => {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) return resolve(false);
    try {
      const rec = new SpeechRecognition();
      rec.lang = language;
      rec.continuous = false;
      rec.interimResults = true;
      let done = false;
      const finish = (ok) => {
        if (done) return;
        done = true;
        try { rec.onstart = null; rec.onend = null; rec.onerror = null; } catch {}
        resolve(ok);
      };
      rec.onstart = () => {
        // stop almost immediately; we only want to "unlock"/warm up recognition in a user gesture.
        setTimeout(() => {
          try { rec.stop(); } catch {}
        }, 200);
      };
      rec.onend = () => finish(true);
      rec.onerror = () => finish(false);
      rec.start();
      // Safety: if nothing happens, still resolve.
      setTimeout(() => finish(false), 1200);
    } catch {
      resolve(false);
    }
  });
}

/**
 * Enhanced listenOnce with noise reduction and better settings
 */
export async function listenOnce(options = {}) {
  const {
    timeout = 10000,
    silenceTimeoutMs,
    interim = false,
    continuous = false,
    maxAlternatives = 1,
    language = (typeof navigator !== "undefined" && navigator.language) ? navigator.language : "en-US"
  } = options;

  return new Promise((resolve, reject) => {
    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition;
    
    if (!SpeechRecognition) {
      console.error("Speech recognition not supported in this browser.");
      return resolve(null);
    }

    const recognition = new SpeechRecognition();
    
    // Enhanced settings for better accuracy
    recognition.lang = language;
    recognition.continuous = continuous;
    recognition.interimResults = interim;
    recognition.maxAlternatives = maxAlternatives;

    // Do not set recognition.grammars here.
    // Some browsers throw when assigning a non-SpeechGrammarList value.
    
    let timeoutId = null;
    let silenceId = null;
    let settled = false;
    let finalTranscript = "";
    let interimTranscript = "";

    const finish = (value) => {
      if (settled) return;
      settled = true;
      try { if (timeoutId) clearTimeout(timeoutId); } catch {}
      try { if (silenceId) clearTimeout(silenceId); } catch {}
      try {
        recognition.onstart = null;
        recognition.onresult = null;
        recognition.onerror = null;
        recognition.onend = null;
      } catch {}
      resolve(value);
    };

    const scheduleSilenceFinish = () => {
      try { if (silenceId) clearTimeout(silenceId); } catch {}
      // Mobile Chrome often fails to mark results as final; treat ~1.1s of silence as end of utterance.
      // iOS tends to need a slightly longer pause.
      const ms = Number.isFinite(silenceTimeoutMs)
        ? Math.max(400, silenceTimeoutMs)
        : (_isIOS() ? 1500 : 1100);
      silenceId = setTimeout(() => {
        try { recognition.stop(); } catch {}
        finish(finalTranscript.trim() || interimTranscript.trim() || null);
      }, ms);
    };

    // Set timeout
    if (timeout > 0) {
      timeoutId = setTimeout(() => {
        try { recognition.stop(); } catch {}
        finish(finalTranscript.trim() || interimTranscript.trim() || null);
      }, timeout);
    }

    recognition.onstart = () => {
      console.log("Voice recognition started");
    };

    recognition.onresult = (event) => {
      let interimText = "";
      let finalText = "";

      for (let i = event.resultIndex; i < event.results.length; i++) {
        const transcript = event.results[i][0].transcript;
        if (event.results[i].isFinal) {
          finalText += transcript + " ";
        } else {
          interimText += transcript + " ";
        }
      }

      finalTranscript += finalText;
      interimTranscript = interimText;

      if ((finalText || interimText) && !continuous) {
        scheduleSilenceFinish();
      }

      // If we have final results and not waiting for more, resolve
      if (finalText && !continuous) {
        try { if (timeoutId) clearTimeout(timeoutId); } catch {}
        try { if (silenceId) clearTimeout(silenceId); } catch {}
        try { recognition.stop(); } catch {}
        finish(finalTranscript.trim() || null);
      }
    };

    recognition.onerror = (event) => {
      console.warn("Speech recognition error:", event.error);
      try { if (timeoutId) clearTimeout(timeoutId); } catch {}
      try { if (silenceId) clearTimeout(silenceId); } catch {}
      
      // Handle specific errors gracefully
      if (event.error === "no-speech") {
        // No speech detected, return null
        finish(null);
      } else if (event.error === "aborted") {
        // Recognition aborted, return what we have
        finish(finalTranscript.trim() || null);
      } else if (event.error === "not-allowed" || event.error === "service-not-allowed") {
        finish(null);
      } else if (event.error === "network") {
        // Network error (common on mobile). Return what we have.
        finish(finalTranscript.trim() || interimTranscript.trim() || null);
      } else {
        finish(finalTranscript.trim() || interimTranscript.trim() || null);
      }
    };

    recognition.onend = () => {
      // If we already settled via timeout/silence/result, ignore.
      if (settled) return;
      finish(finalTranscript.trim() || (interimTranscript.trim() && !continuous ? interimTranscript.trim() : null) || null);
    };

    try {
      recognition.start();
    } catch (error) {
      console.warn("Failed to start recognition:", error);
      try { if (timeoutId) clearTimeout(timeoutId); } catch {}
      try { if (silenceId) clearTimeout(silenceId); } catch {}
      finish(null);
    }
  });
}

/**
 * Initialize audio processing for noise reduction
 */
export async function initAudioProcessing() {
  try {
    if (!audioContext) {
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
    }

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        sampleRate: 16000, // Optimal for speech recognition
        channelCount: 1
      }
    });

    if (microphone) {
      microphone.disconnect();
    }

    microphone = audioContext.createMediaStreamSource(stream);
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.8;

    // Create a script processor for noise reduction
    if (audioContext.createScriptProcessor) {
      processor = audioContext.createScriptProcessor(4096, 1, 1);
    } else if (audioContext.createJavaScriptNode) {
      processor = audioContext.createJavaScriptNode(4096, 1, 1);
    }

    if (processor) {
      processor.onaudioprocess = (event) => {
        // Simple noise gate - only process if volume is above threshold
        const inputBuffer = event.inputBuffer;
        const inputData = inputBuffer.getChannelData(0);
        let sum = 0;
        
        for (let i = 0; i < inputData.length; i++) {
          sum += Math.abs(inputData[i]);
        }
        
        const average = sum / inputData.length;
        const threshold = 0.01; // Noise gate threshold
        
        if (average < threshold) {
          // Below threshold, zero out
          const outputBuffer = event.outputBuffer;
          const outputData = outputBuffer.getChannelData(0);
          for (let i = 0; i < outputData.length; i++) {
            outputData[i] = 0;
          }
        }
      };

      microphone.connect(processor);
      processor.connect(analyser);
    } else {
      microphone.connect(analyser);
    }

    return { stream, analyser };
  } catch (error) {
    console.warn("Audio processing initialization failed:", error);
    return null;
  }
}

/**
 * Get current audio level for visualization
 */
export function getAudioLevel() {
  if (!analyser) return 0;

  const dataArray = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(dataArray);

  let sum = 0;
  for (let i = 0; i < dataArray.length; i++) {
    sum += dataArray[i];
  }

  return sum / (dataArray.length * 255); // Normalize to 0-1
}

/**
 * Cleanup audio resources
 */
export function cleanupAudio() {
  if (processor) {
    processor.disconnect();
    processor = null;
  }
  if (microphone) {
    microphone.disconnect();
    microphone = null;
  }
  const ctx = audioContext;
  audioContext = null;
  if (ctx) {
    try {
      if (ctx.state !== "closed") {
        const p = ctx.close();
        if (p && typeof p.then === "function") p.catch(() => {});
      }
    } catch {}
  }
  analyser = null;
}

function _arrayBufferToBase64(buf) {
  const bytes = new Uint8Array(buf);
  let binary = "";
  // chunk to avoid call stack limits
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode.apply(null, chunk);
  }
  return btoa(binary);
}

function _resampleToTarget(float32, srcRate, targetRate) {
  if (!float32 || !float32.length) return new Float32Array();
  if (srcRate === targetRate) return float32;
  const ratio = srcRate / targetRate;
  const newLen = Math.max(1, Math.round(float32.length / ratio));
  const out = new Float32Array(newLen);
  for (let i = 0; i < newLen; i++) {
    const pos = i * ratio;
    const idx = Math.floor(pos);
    const frac = pos - idx;
    const a = float32[idx] || 0;
    const b = float32[idx + 1] || a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

function _floatTo16BitPCM(float32) {
  const out = new Int16Array(float32.length);
  for (let i = 0; i < float32.length; i++) {
    let s = Math.max(-1, Math.min(1, float32[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

function _trimLeadingTrailingSilence(float32, sampleRateHz, options = {}) {
  const {
    threshold = 0.008,
    padMs = 140,
    minMs = 220,
    maxTrimMs = 26000,
  } = options;

  if (!float32 || !float32.length) return new Float32Array();

  const maxSamples = Math.max(1, Math.floor((Math.max(500, maxTrimMs) / 1000) * sampleRateHz));
  const n = Math.min(float32.length, maxSamples);

  let first = -1;
  let last = -1;
  for (let i = 0; i < n; i++) {
    if (Math.abs(float32[i]) >= threshold) {
      first = i;
      break;
    }
  }
  if (first === -1) return new Float32Array();
  for (let i = n - 1; i >= 0; i--) {
    if (Math.abs(float32[i]) >= threshold) {
      last = i;
      break;
    }
  }
  if (last < first) return new Float32Array();

  const pad = Math.floor((Math.max(0, padMs) / 1000) * sampleRateHz);
  let start = Math.max(0, first - pad);
  let end = Math.min(n, last + pad + 1);
  const minSamples = Math.floor((Math.max(0, minMs) / 1000) * sampleRateHz);
  if ((end - start) < minSamples) {
    // Keep at least a minimal slice centered around detected speech.
    const mid = Math.floor((first + last) / 2);
    start = Math.max(0, mid - Math.floor(minSamples / 2));
    end = Math.min(n, start + minSamples);
  }

  return float32.slice(start, end);
}

// Records audio using WebAudio and returns LINEAR16 PCM base64.
// Works on iOS where MediaRecorder may be missing.
export async function recordPcm16Once(options = {}) {
  const {
    maxMs = 6500,
    sampleRateHz = 16000,
    silenceStopMs = 1200,
    silenceRms = 0.008,
    startRms = 0.015,
  } = options;

  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext || !navigator.mediaDevices?.getUserMedia) {
    return { audio_b64: null, sample_rate_hz: sampleRateHz };
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
    video: false,
  });

  const ctx = new AudioContext();
  try {
    if (ctx.state === "suspended") {
      try { await ctx.resume(); } catch {}
    }

    const source = ctx.createMediaStreamSource(stream);
    const processorNode = ctx.createScriptProcessor(4096, 1, 1);

    const chunks = [];
    let total = 0;
    let started = false;
    let silenceSince = null;
    const startedAt = performance.now();

    const stopAll = () => {
      try { processorNode.disconnect(); } catch {}
      try { source.disconnect(); } catch {}
      try { stream.getTracks().forEach(t => t.stop()); } catch {}
    };

    let finished = false;
    const done = () => {
      if (finished) return;
      finished = true;
      stopAll();
      try {
        if (ctx && ctx.state !== "closed") {
          const p = ctx.close();
          if (p && typeof p.then === "function") p.catch(() => {});
        }
      } catch {}
    };

    const resultPromise = new Promise((resolve) => {
      const finish = () => {
        done();
        const flat = new Float32Array(total);
        let offset = 0;
        for (const c of chunks) {
          flat.set(c, offset);
          offset += c.length;
        }

        const resampled = _resampleToTarget(flat, ctx.sampleRate, sampleRateHz);

        // If we never crossed the start threshold, treat as no usable speech.
        if (!started) {
          resolve({
            audio_b64: null,
            sample_rate_hz: sampleRateHz,
          });
          return;
        }

        const trimmed = _trimLeadingTrailingSilence(resampled, sampleRateHz, {
          threshold: Math.max(0.004, Number(silenceRms) || 0.008),
          padMs: 150,
          minMs: 240,
          maxTrimMs: maxMs,
        });

        if (!trimmed || !trimmed.length) {
          resolve({
            audio_b64: null,
            sample_rate_hz: sampleRateHz,
          });
          return;
        }

        const pcm16 = _floatTo16BitPCM(trimmed);
        resolve({
          audio_b64: _arrayBufferToBase64(pcm16.buffer),
          sample_rate_hz: sampleRateHz,
        });
      };

      processorNode.onaudioprocess = (e) => {
        const input = e.inputBuffer.getChannelData(0);

        // Compute RMS for silence detection
        let sum = 0;
        for (let i = 0; i < input.length; i++) {
          const v = input[i];
          sum += v * v;
        }
        const rms = Math.sqrt(sum / input.length);

        if (!started && rms >= startRms) {
          started = true;
          silenceSince = null;
        }

        if (started) {
          if (rms < silenceRms) {
            if (silenceSince == null) silenceSince = performance.now();
          } else {
            silenceSince = null;
          }
        }

        // Save audio
        const copy = new Float32Array(input.length);
        copy.set(input);
        chunks.push(copy);
        total += copy.length;

        const now = performance.now();
        const elapsed = now - startedAt;
        const silentFor = silenceSince != null ? (now - silenceSince) : 0;

        if (elapsed >= maxMs) {
          finish();
        } else if (started && silenceSince != null && silentFor >= silenceStopMs) {
          finish();
        }
      };

      setTimeout(() => {
        // Safety cutoff
        try { finish(); } catch {}
      }, Math.max(1000, maxMs + 250));
    });

    source.connect(processorNode);
    processorNode.connect(ctx.destination);

    return await resultPromise;
  } catch {
    try { stream.getTracks().forEach(t => t.stop()); } catch {}
    try {
      if (ctx && ctx.state !== "closed") {
        const p = ctx.close();
        if (p && typeof p.then === "function") p.catch(() => {});
      }
    } catch {}
    return { audio_b64: null, sample_rate_hz: sampleRateHz };
  }
}

// Start recording PCM16 and return a controller that can be stopped manually.
// This enables "hold-to-talk" (press to start, release to stop).
export async function startPcm16Recorder(options = {}) {
  const {
    maxMs = 20000,
    sampleRateHz = 16000,
  } = options;

  const AudioContext = window.AudioContext || window.webkitAudioContext;
  if (!AudioContext || !navigator.mediaDevices?.getUserMedia) {
    return null;
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
    video: false,
  });

  const ctx = new AudioContext();
  try {
    if (ctx.state === "suspended") {
      try { await ctx.resume(); } catch {}
    }

    const source = ctx.createMediaStreamSource(stream);
    const processorNode = ctx.createScriptProcessor(4096, 1, 1);

    const chunks = [];
    let total = 0;
    const startedAt = performance.now();
    let finished = false;
    let resolveFinish = null;
    let rejectFinish = null;

    const stopAll = () => {
      try { processorNode.disconnect(); } catch {}
      try { source.disconnect(); } catch {}
      try { stream.getTracks().forEach(t => t.stop()); } catch {}
    };

    const done = () => {
      if (finished) return;
      finished = true;
      stopAll();
      try {
        if (ctx && ctx.state !== "closed") {
          const p = ctx.close();
          if (p && typeof p.then === "function") p.catch(() => {});
        }
      } catch {}
    };

    const finishPromise = new Promise((resolve, reject) => {
      resolveFinish = resolve;
      rejectFinish = reject;
    });

    const finalize = () => {
      if (finished) return;
      done();
      try {
        const flat = new Float32Array(total);
        let offset = 0;
        for (const c of chunks) {
          flat.set(c, offset);
          offset += c.length;
        }

        const resampled = _resampleToTarget(flat, ctx.sampleRate, sampleRateHz);
        const trimmed = _trimLeadingTrailingSilence(resampled, sampleRateHz, {
          threshold: 0.004,
          padMs: 150,
          minMs: 240,
          maxTrimMs: maxMs,
        });

        if (!trimmed || !trimmed.length) {
          resolveFinish({ audio_b64: null, sample_rate_hz: sampleRateHz });
          return;
        }

        const pcm16 = _floatTo16BitPCM(trimmed);
        resolveFinish({
          audio_b64: _arrayBufferToBase64(pcm16.buffer),
          sample_rate_hz: sampleRateHz,
        });
      } catch (e) {
        rejectFinish(e);
      }
    };

    processorNode.onaudioprocess = (e) => {
      const input = e.inputBuffer.getChannelData(0);

      const copy = new Float32Array(input.length);
      copy.set(input);
      chunks.push(copy);
      total += copy.length;

      const now = performance.now();
      const elapsed = now - startedAt;
      if (elapsed >= maxMs) {
        finalize();
      }
    };

    source.connect(processorNode);
    processorNode.connect(ctx.destination);

    // Safety cutoff
    const cutoff = setTimeout(() => {
      try { finalize(); } catch {}
    }, Math.max(1000, maxMs + 250));

    return {
      stop: async () => {
        try { clearTimeout(cutoff); } catch {}
        try { finalize(); } catch {}
        return await finishPromise;
      },
      cancel: () => {
        try { clearTimeout(cutoff); } catch {}
        try { done(); } catch {}
        try { resolveFinish({ audio_b64: null, sample_rate_hz: sampleRateHz }); } catch {}
      },
    };
  } catch (e) {
    try { stream.getTracks().forEach(t => t.stop()); } catch {}
    try {
      if (ctx && ctx.state !== "closed") {
        const p = ctx.close();
        if (p && typeof p.then === "function") p.catch(() => {});
      }
    } catch {}
    return null;
  }
}

// Start a WebSpeech recognition session that can be stopped manually.
// This is a fallback for hold-to-talk when server-side audio transcription is disabled.
export function startWebSpeechHold(options = {}) {
  const {
    language = (typeof navigator !== "undefined" && navigator.language) ? navigator.language : "en-US",
    maxMs = 20000,
  } = options;

  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return null;

  const recognition = new SpeechRecognition();
  recognition.lang = language;
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.maxAlternatives = 1;

  let settled = false;
  let transcript = "";
  let timeoutId = null;

  const promise = new Promise((resolve) => {
    const finish = (value) => {
      if (settled) return;
      settled = true;
      try { if (timeoutId) clearTimeout(timeoutId); } catch {}
      try {
        recognition.onresult = null;
        recognition.onerror = null;
        recognition.onend = null;
      } catch {}
      resolve(value);
    };

    recognition.onresult = (event) => {
      try {
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const r = event.results[i];
          const t = (r && r[0] && r[0].transcript) ? String(r[0].transcript) : "";
          if (t) transcript += t + " ";
        }
      } catch {}
    };

    recognition.onerror = () => {
      finish(transcript.trim() || null);
    };

    recognition.onend = () => {
      finish(transcript.trim() || null);
    };

    if (maxMs > 0) {
      timeoutId = setTimeout(() => {
        try { recognition.stop(); } catch {}
        finish(transcript.trim() || null);
      }, maxMs);
    }
  });

  try {
    recognition.start();
  } catch {
    // If start fails, return a no-op controller.
    return {
      stop: async () => null,
      promise: Promise.resolve(null),
    };
  }

  return {
    stop: () => {
      try { recognition.stop(); } catch {}
    },
    promise,
  };
}
