// src/utils/api.js
const API_URL = process.env.REACT_APP_API_URL ||
  "https://jarvis-cloud-assistant.onrender.com";

export async function sendMessage(text) {
  try {
    const res = await fetch(`${API_URL}/api/message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    return data;
  } catch (err) {
    console.error("API error:", err);
    return { text: "Error contacting backend.", actions: [] };
  }
}

