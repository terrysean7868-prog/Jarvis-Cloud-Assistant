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

export function speak(text, onEnd) {
  const synth = window.speechSynthesis;
  // Cancel any ongoing speech
  synth.cancel();
  
  const utter = new SpeechSynthesisUtterance(text);
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

/**
 * Enhanced listenOnce with noise reduction and better settings
 */
export async function listenOnce(options = {}) {
  const {
    timeout = 10000,
    interim = false,
    continuous = false,
    maxAlternatives = 1,
    language = "en-US"
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
    
    // Better recognition settings
    recognition.grammars = null; // Use default grammar for better accuracy
    
    let timeoutId = null;
    let finalTranscript = "";
    let interimTranscript = "";

    // Set timeout
    if (timeout > 0) {
      timeoutId = setTimeout(() => {
        recognition.stop();
        resolve(finalTranscript || interimTranscript || null);
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

      // If we have final results and not waiting for more, resolve
      if (finalText && !continuous) {
        if (timeoutId) clearTimeout(timeoutId);
        recognition.stop();
        resolve(finalTranscript.trim() || null);
      }
    };

    recognition.onerror = (event) => {
      console.warn("Speech recognition error:", event.error);
      if (timeoutId) clearTimeout(timeoutId);
      
      // Handle specific errors gracefully
      if (event.error === "no-speech") {
        // No speech detected, return null
        resolve(null);
      } else if (event.error === "aborted") {
        // Recognition aborted, return what we have
        resolve(finalTranscript.trim() || null);
      } else if (event.error === "network") {
        // Network error, retry once
        setTimeout(() => {
          try {
            recognition.start();
          } catch (e) {
            resolve(null);
          }
        }, 500);
      } else {
        resolve(null);
      }
    };

    recognition.onend = () => {
      if (timeoutId) clearTimeout(timeoutId);
      // If we have a final transcript, return it
      if (finalTranscript.trim()) {
        resolve(finalTranscript.trim());
      } else if (interimTranscript.trim() && !continuous) {
        // Return interim if no final result
        resolve(interimTranscript.trim());
      } else if (!finalTranscript && !interimTranscript) {
        resolve(null);
      }
    };

    try {
      recognition.start();
    } catch (error) {
      console.warn("Failed to start recognition:", error);
      if (timeoutId) clearTimeout(timeoutId);
      resolve(null);
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
  if (audioContext && audioContext.state !== "closed") {
    audioContext.close();
    audioContext = null;
  }
  analyser = null;
}
