// src/components/MicAnalyzer.jsx
import React, { useEffect, useRef } from "react";
import "./../styles/hudOverlay.css";

export default function MicAnalyzer({ listening }) {
  const canvasRef = useRef(null);

  useEffect(() => {
    if (!listening) return;
    
    const canvas = canvasRef.current;
    const ctx = canvas.getContext("2d");
    
    // Use webkitAudioContext for Safari compatibility
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    const audioCtx = new AudioContext();
    
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;

    // Mobile-friendly audio constraints
    const audioConstraints = {
      audio: {
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
        sampleRate: { ideal: 16000 }
      }
    };

    navigator.mediaDevices.getUserMedia(audioConstraints)
      .then((stream) => {
        const src = audioCtx.createMediaStreamSource(stream);
        src.connect(analyser);
        const data = new Uint8Array(analyser.frequencyBinCount);
        let animFrameId;

        const draw = () => {
          animFrameId = requestAnimationFrame(draw);
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
        
        return () => {
          cancelAnimationFrame(animFrameId);
          stream.getTracks().forEach(track => track.stop());
          audioCtx.close();
        };
      })
      .catch((err) => {
        console.warn("MicAnalyzer: microphone access failed:", err?.message || err);
      });
  }, [listening]);

  return <canvas ref={canvasRef} width={200} height={200} className="mic-analyzer" />;
}
