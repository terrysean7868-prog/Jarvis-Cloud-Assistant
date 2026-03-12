// src/utils/api.js
// Priority:
// 1) Explicit build-time override: REACT_APP_API_URL
// 2) Development: use CRA proxy by using relative URLs ("" base)
// 3) Production: assume same-origin when served by backend; fallback to public Render URL
const isDev = process.env.NODE_ENV === "development";
const envBase = process.env.REACT_APP_API_URL;

let queryBase = "";
try {
  if (typeof window !== "undefined" && window.location && window.location.search) {
    const params = new URLSearchParams(window.location.search);
    queryBase = String(params.get("api_url") || "").trim();
  }
} catch {}

const isHttpUrl = (value) => /^https?:\/\//i.test(String(value || "").trim());
const queryApi = isHttpUrl(queryBase) ? queryBase.replace(/\/$/, "") : "";

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

export const API_URL = envBase || queryApi || (isDev ? "" : prodBase);

const DEFAULT_TIMEOUT = parseInt(process.env.REACT_APP_API_TIMEOUT_MS || "20000", 10); // 20s

function timeoutFetch(url, opts = {}, timeout = DEFAULT_TIMEOUT) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  const init = { ...opts, signal: controller.signal };

  return fetch(url, init)
    .finally(() => clearTimeout(id));
}

async function throwHttpError(res) {
  let bodyText = await res.text().catch(() => `HTTP ${res.status}`);
  let bodyJson = null;
  try {
    bodyJson = bodyText ? JSON.parse(bodyText) : null;
  } catch {
    bodyJson = null;
  }
  const err = new Error(`HTTP error! status: ${res.status} - ${bodyText}`);
  err.status = res.status;
  if (bodyJson && typeof bodyJson === "object") {
    err.detail = bodyJson.detail ?? bodyJson;
  }
  throw err;
}

export async function sendMessage(text, mode = "chat", sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  try {
    const res = await timeoutFetch(`${API_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, mode, user: "user", session_id: sessionId }),
    }, timeoutMs);

    if (!res.ok) {
      await throwHttpError(res);
    }
    const data = await res.json();
    return data;
  } catch (err) {
    console.error("sendMessage error:", err);
    const isTimeout = err && err.name === "AbortError";
    const message = isTimeout
      ? "Request timed out. Please try again."
      : (err?.message || "Request failed. Please try again.");

    // Return a consistent structure so callers can always render something.
    return {
      status: "error",
      message,
      text: message,
      actions: [],
    };
  }
}

// (Capabilities endpoint removed)
export async function addLearningExample(prompt, completion, sessionId, tags = [], timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/learning/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, prompt, completion, tags }),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
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
    await throwHttpError(res);
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
    await throwHttpError(res);
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
    await throwHttpError(res);
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
    await throwHttpError(res);
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
    await throwHttpError(res);
  }
  return await res.json();
}

export async function configureMyPc(sessionId, deviceId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const body = { session_id: sessionId };
  if (deviceId) body.device_id = deviceId;

  const res = await timeoutFetch(`${API_URL}/api/user/device/configure`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function dispatchDeviceActions(actions, sessionId, sourceText = "", deviceId = null, ownerUsername = null, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {
    session_id: sessionId,
    actions: Array.isArray(actions) ? actions : [],
    source_text: sourceText || "",
  };
  if (deviceId) body.device_id = deviceId;
  if (ownerUsername) body.owner_username = ownerUsername;

  const res = await timeoutFetch(`${API_URL}/api/device/dispatch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAgentConfig(sessionId, deviceId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const body = { session_id: sessionId };
  if (deviceId) body.device_id = deviceId;

  const res = await timeoutFetch(`${API_URL}/api/agent/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getSystemInfo(sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  qs.set("session_id", sessionId);

  const res = await timeoutFetch(`${API_URL}/api/system/info?${qs.toString()}`,
    { method: "GET" },
    timeoutMs
  );

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function grantDevicePermissions(sessionId, permissions, deviceId = null, ownerUsername = null, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {
    session_id: sessionId,
    permissions: permissions || {},
  };
  if (deviceId) body.device_id = deviceId;
  if (ownerUsername) body.owner_username = ownerUsername;

  const res = await timeoutFetch(`${API_URL}/api/device/permissions/grant`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getSavedDevicePermissions(sessionId, deviceId = null, ownerUsername = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  qs.set("session_id", sessionId);
  if (deviceId) qs.set("device_id", deviceId);
  if (ownerUsername) qs.set("owner_username", ownerUsername);

  const res = await timeoutFetch(`${API_URL}/api/device/permissions?${qs.toString()}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function googleSpeechToText(sessionId, audioBase64, language = null, sampleRateHz = 16000, timeoutMs = 45000) {
  const body = {
    session_id: sessionId,
    audio_b64: audioBase64,
    sample_rate_hz: sampleRateHz,
  };
  if (language) body.language = language;

  const res = await timeoutFetch(`${API_URL}/api/stt/google`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function secureVoiceToText(sessionId, audioBase64, language = null, sampleRateHz = 16000, timeoutMs = 45000) {
  const body = {
    session_id: sessionId,
    audio_b64: audioBase64,
    sample_rate_hz: sampleRateHz,
  };
  if (language) body.language = language;

  const res = await timeoutFetch(`${API_URL}/api/voice/secure-transcribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export function getNotificationsWsUrl(sessionId) {
  const sid = (sessionId || "").toString();
  if (!sid) return null;

  try {
    const origin = (API_URL && API_URL.startsWith("http"))
      ? API_URL
      : (typeof window !== "undefined" && window.location && window.location.origin)
        ? window.location.origin
        : "";

    const u = new URL(origin);
    const wsProto = u.protocol === "https:" ? "wss:" : "ws:";
    return `${wsProto}//${u.host}/ws/notifications?session_id=${encodeURIComponent(sid)}`;
  } catch (e) {
    // If URL parsing fails (e.g. API_URL is relative in dev), fallback to current origin.
    try {
      const origin = (typeof window !== "undefined" && window.location && window.location.origin)
        ? window.location.origin
        : "";
      if (!origin) return null;
      const u = new URL(origin);
      const wsProto = u.protocol === "https:" ? "wss:" : "ws:";
      return `${wsProto}//${u.host}/ws/notifications?session_id=${encodeURIComponent(sid)}`;
    } catch {
      return null;
    }
  }
}

export async function stopTask(sessionId = null, taskId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {};
  if (sessionId) body.session_id = sessionId;
  if (taskId) body.task_id = taskId;

  const res = await timeoutFetch(`${API_URL}/api/stop-task`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // Keep backward compatible: if no body fields, still send an empty JSON object.
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function deleteTaskByTitle(sessionId, title, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {
    session_id: sessionId,
    title: String(title || "").trim(),
  };

  const res = await timeoutFetch(`${API_URL}/api/delete-task-by-title`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getTasks(sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  if (sessionId) qs.set("session_id", String(sessionId));

  const url = `${API_URL}/api/tasks${qs.toString() ? `?${qs.toString()}` : ""}`;
  const res = await timeoutFetch(url, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAutonomyStatus(sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  if (sessionId) qs.set("session_id", String(sessionId));
  const url = `${API_URL}/api/autonomy/status${qs.toString() ? `?${qs.toString()}` : ""}`;
  const res = await timeoutFetch(url, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function createAutonomyGoal(goal, sessionId = null, priority = 5, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {
    goal: String(goal || "").trim(),
    priority: Number(priority || 5),
  };
  if (sessionId) body.session_id = sessionId;

  const res = await timeoutFetch(`${API_URL}/api/autonomy/goals`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAutonomyGoals({ sessionId = null, statuses = "pending,running,failed,completed", limit = 25, timeoutMs = DEFAULT_TIMEOUT } = {}) {
  const qs = new URLSearchParams();
  qs.set("statuses", String(statuses || "pending,running,failed,completed"));
  qs.set("limit", String(limit || 25));
  if (sessionId) qs.set("session_id", String(sessionId));

  const res = await timeoutFetch(`${API_URL}/api/autonomy/goals?${qs.toString()}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function updateAutonomyGoalGraph(goalId, payload = {}, timeoutMs = DEFAULT_TIMEOUT) {
  const gid = String(goalId || "").trim();
  if (!gid) throw new Error("goalId is required");

  const res = await timeoutFetch(`${API_URL}/api/autonomy/goals/${encodeURIComponent(gid)}/graph`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function controlAutonomyRuntime(action, sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const res = await timeoutFetch(`${API_URL}/api/autonomy/control`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: String(action || "").trim().toLowerCase(), session_id: sessionId || null }),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAnatomyState(sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  if (sessionId) qs.set("session_id", String(sessionId));
  const url = `${API_URL}/api/anatomy/state${qs.toString() ? `?${qs.toString()}` : ""}`;
  const res = await timeoutFetch(url, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAgents(sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  if (sessionId) qs.set("session_id", String(sessionId));
  const url = `${API_URL}/api/agents${qs.toString() ? `?${qs.toString()}` : ""}`;

  const res = await timeoutFetch(url, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getDeviceList(sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  if (sessionId) qs.set("session_id", String(sessionId));
  const url = `${API_URL}/api/device/list${qs.toString() ? `?${qs.toString()}` : ""}`;

  const res = await timeoutFetch(url, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getSelfImprovementProposals(sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  if (sessionId) qs.set("session_id", String(sessionId));
  const url = `${API_URL}/api/self-improvement/proposals${qs.toString() ? `?${qs.toString()}` : ""}`;

  const res = await timeoutFetch(url, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function decideSelfImprovementProposal(proposalId, decision, sessionId = null, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {
    proposal_id: String(proposalId || "").trim(),
    decision: String(decision || "").trim().toLowerCase(),
  };
  if (sessionId) body.session_id = sessionId;

  const res = await timeoutFetch(`${API_URL}/api/self-improvement/proposals/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAdminUpdateHistory(sessionId, limit = 100, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  qs.set("session_id", String(sessionId || ""));
  qs.set("limit", String(limit || 100));

  const res = await timeoutFetch(`${API_URL}/api/admin/updates/history?${qs.toString()}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAdminProgressiveUpdateReport(sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  qs.set("session_id", String(sessionId || ""));

  const res = await timeoutFetch(`${API_URL}/api/admin/updates/progressive-report?${qs.toString()}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function getAdminUpdateConfig(sessionId, timeoutMs = DEFAULT_TIMEOUT) {
  const qs = new URLSearchParams();
  qs.set("session_id", String(sessionId || ""));

  const res = await timeoutFetch(`${API_URL}/api/admin/updates/config?${qs.toString()}`, {
    method: "GET",
    headers: { "Content-Type": "application/json" },
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function runAdminAutoUpdate({ sessionId, description, scopes = null, autoInstallDeps = null, dryRun = false }, timeoutMs = 180000) {
  const body = {
    session_id: sessionId,
    description: String(description || "").trim(),
    dry_run: !!dryRun,
  };
  if (Array.isArray(scopes) && scopes.length) body.scopes = scopes;
  if (typeof autoInstallDeps === "boolean") body.auto_install_deps = autoInstallDeps;

  const res = await timeoutFetch(`${API_URL}/api/admin/updates/auto`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function runAdminUpdate({ sessionId, filePath, description, autoInstallDeps = null, dryRun = false }, timeoutMs = 120000) {
  const body = {
    session_id: sessionId,
    file_path: filePath,
    description,
    dry_run: !!dryRun,
  };
  if (typeof autoInstallDeps === "boolean") {
    body.auto_install_deps = autoInstallDeps;
  }

  const res = await timeoutFetch(`${API_URL}/api/admin/updates/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}

export async function rollbackAdminUpdate({ sessionId, filePath, backupPath = null }, timeoutMs = DEFAULT_TIMEOUT) {
  const body = {
    session_id: sessionId,
    file_path: filePath,
  };
  if (backupPath) body.backup_path = backupPath;

  const res = await timeoutFetch(`${API_URL}/api/admin/updates/rollback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, timeoutMs);

  if (!res.ok) {
    await throwHttpError(res);
  }
  return await res.json();
}
