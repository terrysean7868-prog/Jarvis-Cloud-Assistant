import os
import sys
from datetime import datetime, UTC
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from pymongo import MongoClient

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.utils.voice_auth import _hash_normalized_text, _norm_text


if load_dotenv is not None:
    load_dotenv()

DEFAULT_MONGO_URI = os.getenv("MONGODB_URI") or "mongodb://localhost:27017"
DEFAULT_DB_NAME = os.getenv("MONGODB_DB_NAME", "jarvis_db")


app = FastAPI(title="Jarvis Local Voice Hash UI")


class SaveReq(BaseModel):
    username: str
    voice_text: str
    db_name: str | None = None


def _is_local_mongo_uri(uri: str) -> bool:
    raw = (uri or "").strip().lower()
    return ("localhost" in raw) or ("127.0.0.1" in raw) or ("::1" in raw)


def _assert_safe_target(db_name: str):
    safe_db = (db_name or "").strip()
    if not safe_db:
        raise HTTPException(status_code=400, detail="Database name is required.")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Local Voice Hash UI</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; max-width: 760px; }}
    .row {{ margin-bottom: 12px; }}
    input, textarea, button {{ width: 100%; padding: 10px; font-size: 14px; }}
    button {{ cursor: pointer; }}
    .two {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
    .status {{ padding: 10px; border-radius: 8px; background: #f3f4f6; white-space: pre-wrap; }}
    .ok {{ background: #ecfdf3; }}
    .err {{ background: #fef2f2; }}
  </style>
</head>
<body>
  <h2>Local Voice Hash Capture</h2>
  <p>Speak your phrase, verify transcript, then save hash for a user in your configured MongoDB database.</p>

  <div class=\"row\">
    <small id=\"envInfo\"></small>
  </div>
  <div class=\"row\">
    <small id=\"permInfo\"></small>
  </div>

  <div class=\"row\">
    <label>Username</label>
    <input id=\"username\" value=\"terry\" />
  </div>

  <div class=\"row\">
    <label>DB Name</label>
    <input id=\"dbName\" value=\"{DEFAULT_DB_NAME}\" />
  </div>

  <div class=\"two\">
    <button id=\"enableMicBtn\">✅ Enable Mic Access</button>
    <button id=\"speakBtn\">🎤 Speak Phrase</button>
    <button id=\"saveBtn\">💾 Save Hash</button>
  </div>

  <div class=\"row\" style=\"margin-top:12px\">
    <label>Captured Transcript</label>
    <textarea id=\"voiceText\" rows=\"4\" placeholder=\"Transcript appears here...\"></textarea>
  </div>

  <div id=\"status\" class=\"status\">Ready.</div>

<script>
const statusEl = document.getElementById('status');
const envInfoEl = document.getElementById('envInfo');
const permInfoEl = document.getElementById('permInfo');
const usernameEl = document.getElementById('username');
const dbNameEl = document.getElementById('dbName');
const voiceTextEl = document.getElementById('voiceText');
const speakBtn = document.getElementById('speakBtn');
const enableMicBtn = document.getElementById('enableMicBtn');

let micPermissionGranted = false;

envInfoEl.textContent = 'Host: ' + window.location.host + ' | Secure Context: ' + String(window.isSecureContext);

async function refreshPermissionState() {{
  if (!navigator.permissions || !navigator.permissions.query) {{
    permInfoEl.textContent = 'Mic Permission State: unavailable in this browser';
    return;
  }}
  try {{
    const result = await navigator.permissions.query({{ name: 'microphone' }});
    const state = result && result.state ? result.state : 'unknown';
    permInfoEl.textContent = 'Mic Permission State: ' + state;
    result.onchange = () => {{
      const nextState = result && result.state ? result.state : 'unknown';
      permInfoEl.textContent = 'Mic Permission State: ' + nextState;
    }};
  }} catch {{
    permInfoEl.textContent = 'Mic Permission State: unknown';
  }}
}}

refreshPermissionState();

function setStatus(msg, ok=false) {{
  statusEl.className = 'status ' + (ok ? 'ok' : 'err');
  statusEl.textContent = msg;
}}

async function ensureMicPermission() {{
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
    setStatus('Browser does not support microphone access here. Open this UI on http://localhost:8787 in Chrome/Edge.');
    return false;
  }}
  if (navigator.permissions && navigator.permissions.query) {{
    try {{
      const p = await navigator.permissions.query({{ name: 'microphone' }});
      if (p.state === 'denied') {{
        setStatus('Mic is blocked by browser. Click lock icon near URL → Site settings → Microphone: Allow, then reload page.');
        await refreshPermissionState();
        return false;
      }}
    }} catch {{}}
  }}
  if (micPermissionGranted) return true;
  try {{
    setStatus('Requesting microphone permission...', true);
    const stream = await navigator.mediaDevices.getUserMedia({{ audio: true, video: false }});
    try {{ stream.getTracks().forEach((t) => t.stop()); }} catch {{}}
    micPermissionGranted = true;
    setStatus('Microphone permission granted.', true);
    await refreshPermissionState();
    return true;
  }} catch {{
    setStatus('Microphone permission denied or unavailable. Allow mic in browser site settings, then reload this page.');
    await refreshPermissionState();
    return false;
  }}
}}

enableMicBtn.addEventListener('click', async () => {{
  enableMicBtn.disabled = true;
  try {{
    await ensureMicPermission();
  }} finally {{
    enableMicBtn.disabled = false;
  }}
}});

speakBtn.addEventListener('click', async () => {{
  speakBtn.disabled = true;
  try {{
    const okMic = await ensureMicPermission();
    if (!okMic) return;

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {{
    setStatus('SpeechRecognition not supported in this browser. Use Chrome/Edge, or type phrase manually.');
    return;
  }}

  const recog = new SR();
  recog.lang = 'en-US';
  recog.interimResults = false;
  recog.continuous = false;
  recog.maxAlternatives = 1;

  setStatus('Listening... speak now', true);
  let gotResult = false;

  const timeoutId = setTimeout(() => {{
    try {{ recog.stop(); }} catch {{}}
    if (!gotResult) {{
      setStatus('No speech captured in time. Click Speak and talk immediately.', false);
    }}
  }}, 8000);

  recog.onresult = (event) => {{
    gotResult = true;
    const text = event.results?.[0]?.[0]?.transcript || '';
    voiceTextEl.value = text;
    setStatus('Captured transcript:\n' + text, true);
  }};

  recog.onerror = (e) => {{
    const err = e?.error || 'unknown';
    if (err === 'not-allowed' || err === 'service-not-allowed') {{
      setStatus('Speech permission blocked. Allow microphone and speech recognition permissions in browser settings.');
      return;
    }}
    if (err === 'no-speech') {{
      setStatus('No speech detected. Move closer to mic and try again.');
      return;
    }}
    if (err === 'network') {{
      setStatus('Speech service network error. Check internet or type phrase manually.');
      return;
    }}
    if (err === 'aborted') {{
      setStatus('Speech capture aborted. Click Speak Phrase and try again.');
      return;
    }}
    setStatus('Speech error: ' + err);
  }};

  recog.onend = () => {{
    try {{ clearTimeout(timeoutId); }} catch {{}}
    if (!voiceTextEl.value.trim()) {{
      setStatus('No speech captured. Try again and allow microphone access.');
    }}
  }};

  try {{ recog.start(); }} catch (e) {{ setStatus('Could not start mic: ' + (e?.message || e)); }}
  }} finally {{
    speakBtn.disabled = false;
  }}
}});

document.getElementById('saveBtn').addEventListener('click', async () => {{
  const username = usernameEl.value.trim();
  const voice_text = voiceTextEl.value.trim();
  const db_name = dbNameEl.value.trim();

  if (!username) {{ setStatus('Username is required.'); return; }}
  if (!voice_text) {{ setStatus('Speak first (or type phrase) before saving.'); return; }}

  setStatus('Saving...', true);

  try {{
    const res = await fetch('/api/save-voice-hash', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ username, voice_text, db_name }})
    }});
    const data = await res.json();
    if (!res.ok || data.status !== 'success') {{
      setStatus((data && (data.message || data.detail)) || 'Save failed');
      return;
    }}
    setStatus(
      'Saved successfully.\n' +
      'username: ' + data.username + '\n' +
      'db: ' + data.db_name + '\n' +
      'hash: ' + data.voice_hash + '\n' +
      'normalized_text: ' + data.normalized_text,
      true
    );
  }} catch (e) {{
    setStatus('Request failed: ' + (e?.message || e));
  }}
}});
</script>
</body>
</html>
"""
    )


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_probe():
    return Response(status_code=204)


@app.post("/api/save-voice-hash")
async def save_voice_hash(req: SaveReq):
    username = (req.username or "").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="username is required")

    spoken = (req.voice_text or "").strip()
    if not spoken:
        raise HTTPException(status_code=400, detail="voice_text is required")

    db_name = (req.db_name or DEFAULT_DB_NAME).strip()
    _assert_safe_target(db_name)

    normalized_text = _norm_text(spoken)
    voice_hash = _hash_normalized_text(spoken)
    if not voice_hash:
        raise HTTPException(status_code=400, detail="Unable to derive hash from voice_text")

    client = MongoClient(DEFAULT_MONGO_URI, serverSelectionTimeoutMS=8000)
    try:
        client.admin.command("ping")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Mongo connect failed: {e}")

    col = client[db_name]["auth_users"]
    user_doc = col.find_one({"username": username})
    if not user_doc:
        try:
            client.close()
        except Exception:
            pass
        raise HTTPException(status_code=404, detail=f"User '{username}' not found in db '{db_name}'")

    now = datetime.now(UTC).isoformat()
    col.update_one(
        {"username": username},
        {
            "$set": {
                "voice_hash": voice_hash,
                "voice_samples": [{"hash": voice_hash, "text": normalized_text or None, "created_at": now}],
                "updated_at": now,
            }
        },
    )

    try:
        client.close()
    except Exception:
        pass

    return {
        "status": "success",
        "username": username,
        "db_name": db_name,
        "voice_hash": voice_hash,
        "normalized_text": normalized_text,
        "message": "Voice hash replaced and old hashes removed (single sample kept).",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8787)
