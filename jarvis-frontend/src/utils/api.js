// src/utils/api.js

// Automatically detect environment variable system
const API_BASE =
  process.env.REACT_APP_API_URL ||
  import.meta?.env?.VITE_API_URL ||
  "https://jarvis-cloud-assistant.onrender.com";

export async function sendMessage(text) {
  const token = localStorage.getItem("jarvis_token");

  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: token ? `Bearer ${token}` : "",
    },
    body: JSON.stringify({ text }),
  });

  if (!res.ok) throw new Error(`Failed to connect to server (${res.status})`);
  return await res.json();
}
