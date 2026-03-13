// src/components/hud/MicWaveform.jsx
import React, { useEffect, useRef } from "react";

const MicWaveform = ({ active }) => {
  const canvasRef = useRef(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    let animationFrame;

    const drawWave = () => {
      const width = canvas.width;
      const height = canvas.height;
      ctx.clearRect(0, 0, width, height);

      const bars = 50;
      const mid = height / 2;
      for (let i = 0; i < bars; i++) {
        const x = (i / bars) * width;
        const amplitude = active ? Math.sin(Date.now() / 150 + i) * 15 : 2;
        const y = mid + amplitude;
        ctx.fillStyle = active ? "#00ffff" : "#444";
        ctx.fillRect(x, y, 3, -amplitude * 2);
      }
      animationFrame = requestAnimationFrame(drawWave);
    };

    drawWave();
    return () => cancelAnimationFrame(animationFrame);
  }, [active]);

  return <canvas ref={canvasRef} width={250} height={50} className="mic-wave" />;
};

export default MicWaveform;
