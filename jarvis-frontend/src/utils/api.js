// src/utils/api.js
// Priority:
// 1) Explicit build-time override: REACT_APP_API_URL
// 2) Development: use CRA proxy by using relative URLs ("" base)
// 3) Production: assume same-origin when served by backend; fallback to public Render URL
const isDev = process.env.NODE_ENV === "development";
const envBase = process.env.REACT_APP_API_URL;

let prodBase = "https://jarvis-cloud-assistant.onrender.com";
try {
  if (typeof window !== "undefined" && window.location && window.location.origin) {
    const origin = window.location.origin;
    // If the UI is hosted on a separate Render service, default the API to the known backend service.
    // Prefer explicit REACT_APP_API_URL for custom domains / non-default setups.
    if (/jarvis-frontend\.onrender\.com$/i.test(origin)) {
      prodBase = "https://jarvis-cloud-assistant.onrender.com";
    } else {
      prodBase = origin;
    }
  }
} catch {}

export const API_URL = envBase || (isDev ? "" : prodBase);

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

export async function addLearningExample(prompt, completion, sessionId, tags = [], timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/learning/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, prompt, completion, tags }),
  }, timeoutMs);

  if (!res.ok) {
    let errText = await res.text().catch(() => `HTTP ${res.status}`);
    throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
  }
  return await res.json();
}

export async function setAssistantName(assistantName, sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/user/assistant-name`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, assistant_name: assistantName }),
  }, timeoutMs);

  if (!res.ok) {
    let errText = await res.text().catch(() => `HTTP ${res.status}`);
    throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
  }
  return await res.json();
}

export async function getUserPreferences(sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/user/preferences/get`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }, timeoutMs);

  if (!res.ok) {
    let errText = await res.text().catch(() => `HTTP ${res.status}`);
    throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
  }
  return await res.json();
}

export async function setUserPreferences(preferences, sessionId, mode = "merge", timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/user/preferences/set`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, preferences: preferences || {}, mode }),
  }, timeoutMs);

  if (!res.ok) {
    let errText = await res.text().catch(() => `HTTP ${res.status}`);
    throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
  }
  return await res.json();
}

export async function getUserDevice(sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/user/device/get`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }),
  }, timeoutMs);

  if (!res.ok) {
    let errText = await res.text().catch(() => `HTTP ${res.status}`);
    throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
  }
  return await res.json();
}

export async function setUserDevice(deviceId, sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/user/device/set`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, device_id: deviceId }),
  }, timeoutMs);

  if (!res.ok) {
    let errText = await res.text().catch(() => `HTTP ${res.status}`);
    throw new Error(`HTTP error! status: ${res.status} - ${errText}`);
  }
  return await res.json();
}
