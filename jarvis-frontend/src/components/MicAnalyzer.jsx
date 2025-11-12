// src/components/MicAnalyzer.jsx
import React, { useEffect, useRef } from "react";
import "./../styles/hudOverlay.css";

export default function MicAnalyzer({ listening }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    if (!listening) return;
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;

    navigator.mediaDevices.getUserMedia({ audio: true }).then((stream) => {
      const src = audioCtx.createMediaStreamSource(stream);
      src.connect(analyser);
      const data = new Uint8Array(analyser.frequencyBinCount);

      const draw = () => {
        requestAnimationFrame(draw);
        analyser.getByteFrequencyData(data);

        ctx.clearRect(0, 0, canvas.width, canvas.height);
        const cx = canvas.width / 2;
        const cy = canvas.height / 2;
        const radius = 50;

        ctx.beginPath();
        for (let i = 0; i < data.length; i++) {
          const angle = (i / data.length) * 2 * Math.PI;
          const barLength = radius + data[i] / 8;
          const x = cx + Math.cos(angle) * barLength;
          const y = cy + Math.sin(angle) * barLength;
          ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = "rgba(0,255,255,0.8)";
        ctx.lineWidth = 2;
        ctx.shadowBlur = 10;
        ctx.shadowColor = "#00ffff";
        ctx.stroke();
      };
      draw();
    });
  }, [listening]);

  return <canvas ref={canvasRef} width={200} height={200} className="mic-analyzer" />;
}
