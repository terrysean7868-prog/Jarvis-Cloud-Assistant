// src/components/VoiceInput.jsx
import React from "react";

const VoiceInput = ({ onVoice, active }) => (
  <button
    className={`mic-btn ${active ? "listening" : ""}`}
    onClick={onVoice}
    title="Speak to Jarvis"
  >
    🎤
  </button>
);

export default VoiceInput;
