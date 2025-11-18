// src/utils/api.js
const API_URL = process.env.REACT_APP_API_URL || 
  (process.env.NODE_ENV === 'development' ? "http://localhost:8000" : "https://jarvis-cloud-assistant.onrender.com");

export async function sendMessage(text, mode = "chat", sessionId = null) {
  try {
    const res = await fetch(`${API_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode, user: "user", session_id: sessionId }),
    });
    if (!res.ok) {
      throw new Error(`HTTP error! status: ${res.status}`);
    }
    const data = await res.json();
    
    // Check if authentication is required
    if (data.auth_required) {
      return { text: data.text, actions: [], auth_required: true };
    }
    
    return data;
  } catch (err) {
    console.error("API error:", err);
    return { text: "Error contacting backend.", actions: [] };
  }
}

export async function setGitHubConfig(config) {
  try {
    const res = await fetch(`${API_URL}/api/github-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config),
    });
    return await res.json();
  } catch (err) {
    console.error("GitHub config error:", err);
    return { status: "error", message: err.message };
  }
}

export async function triggerGitSync() {
  try {
    const res = await fetch(`${API_URL}/api/git-sync`, {
      method: "POST",
    });
    return await res.json();
  } catch (err) {
    console.error("Git sync error:", err);
    return { status: "error", message: err.message };
  }
}

