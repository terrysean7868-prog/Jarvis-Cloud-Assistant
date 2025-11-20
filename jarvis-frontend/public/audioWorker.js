// public/audioWorker.js
// Placeholder: receives small audio metrics from main thread and can compute heavier transforms
onmessage = (ev) => {
    const data = ev.data || {};
    if (data.type === "process") {
      // Expand if needed — currently unused
      const vol = data.volume || 0;
      // respond with smoothed value
      postMessage({ type: "smoothed", volume: vol });
    }
  };
  