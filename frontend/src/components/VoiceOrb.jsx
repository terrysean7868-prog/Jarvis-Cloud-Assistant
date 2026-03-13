import React, { useState } from "react";
import { motion } from "framer-motion";
import "../styles/voiceOrb.css";

export default function VoiceOrb({ active, onClick }) {
  const [hovered, setHovered] = useState(false);

  return (
    <div className="voice-orb-container">
      <motion.div
        className="voice-orb"
        onClick={onClick}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        animate={{
          scale: active ? 1.2 : hovered ? 1.05 : 1,
          boxShadow: active
            ? "0 0 40px rgba(0,255,255,1)"
            : "0 0 15px rgba(0,255,255,0.6)",
        }}
        transition={{ type: "spring", stiffness: 150, damping: 10 }}
      >
        <div className="orb-core" />
      </motion.div>
    </div>
  );
}
