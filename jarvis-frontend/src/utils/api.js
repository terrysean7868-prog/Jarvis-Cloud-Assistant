// src/utils/api.js
export const API_URL = process.env.REACT_APP_API_URL ||
  (process.env.NODE_ENV === 'development' ? "http://localhost:18001" : "https://jarvis-cloud-assistant.onrender.com");

const DEFAULT_TIMEOUT = parseInt(process.env.REACT_APP_API_TIMEOUT_MS || "20000", 10); // 20s

function timeoutFetch(url, opts = {}, timeout = DEFAULT_TIMEOUT) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  const init = { ...opts, signal: controller.signal };

  return fetch(url, init)
    .finally(() => clearTimeout(id));
}

export async function sendMessage(text, mode = "chat", sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  try {
    const res = await timeoutFetch(`${API_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode, user: "user", session_id: sessionId }),
    }, timeoutMs);

    if (!res.ok) {
      // Try to parse error body for better messaging
      let errText = await res.text().catch(() => `HTTP ${res.status}`);
      throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
    }
    const data = await res.json();
    return data;
  } catch (err) {
    console.error("sendMessage error:", err);
    // return consistent error structure for UI to handle
    return { status: "error", message: err.name === 'AbortError' ? "request_timeout" : err.message };
  }
}
