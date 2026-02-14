# Agent Update Guide (Maintenance Playbook)

Last updated: 2026-02-06

Purpose: When an AI agent (or engineer) needs to update Jarvis, this file tells you **where to look first**, what is “contractual” (must stay consistent), and the minimal checklist to make changes safely.

## 1) Start Here (Fast Orientation)

If you are new to the repo, read these in order:
1) `docs/ARCHITECTURE.md` (system overview + flows)
2) `app.py` (routes, auth gates, mode toggles)
3) `src/core/chat_orchestrator.py` (policy + web/research pipeline)
4) `src/core/jarvis_brain.py` and `src/core/llm_adapter.py` (LLM + actions)
5) `src/core/executor.py` and `apps/pc_agent/pc_agent.py` (legacy alias: `pc_agent.py`) (action execution)

## 2) “Source of Truth” Rules

### Behavior defaults
- Non-secret default behavior is in `src/config/runtime_defaults.py`.
- Environment variables are primarily for secrets and deployment toggles.

### Secrets
- LLM keys are read via `src/config/secrets.py`.
- JWT sessions require `JARVIS_JWT_SECRET` in cloud mode.

### Action contract
Actions are dicts like:
- `{ "type": "web_search", "query": "..." }`
- `{ "type": "n8n_webhook", "path": "skills/...", "payload": {...} }`
- `{ "type": "open_app", "app_name": "notepad" }`

An action type is “real” only when:
- The LLM can emit it (prompt + parser allow it), AND
- Some executor can run it (server `ActionExecutor` or `pc_agent.py`), AND
- The UI can handle the resulting UX (especially for device actions)

## 3) Common Change Scenarios

### A) Add a new device action (runs on PC agent)
Checklist:
- Update `apps/pc_agent/pc_agent.py` (legacy alias: `pc_agent.py`):
  - Implement handler for the new action type
  - Ensure it is permission-guarded (use existing permission booleans)
  - Ensure any file paths respect `_is_path_allowed`
- Update server policy (if needed):
  - If it’s destructive/sensitive, add it to `ADMIN_ONLY_ACTION_TYPES` in `app.py`
  - If it must never run in cloud executor, ensure `src/core/executor.py` forbids it in cloud mode
- Update frontend:
  - If it needs explicit consent, ensure PermissionModal requests the right permission key
  - Ensure device dispatch uses `/api/device/dispatch`
- Add/adjust tests under `tests/` if there’s an existing test pattern for similar actions

### B) Add a server-side action (safe in cloud)
Examples: task creation, email drafting, webhook calls.
Checklist:
- Implement in `src/core/executor.py`
- In cloud mode, consider whether the action should be included in `safe_server_action_types` inside `POST /api/chat` response path
- Ensure payload size stays small for notifications

### C) Modify chat behavior (tone, routing, research)
Checklist:
- `src/core/llm_adapter.py`:
  - Any change in model selection, response schema, action parsing/dedupe
- `src/core/chat_orchestrator.py`:
  - Research detection, async research background job
  - Two-pass web context continuation logic
- `app.py`:
  - Explicit screenshot guard behavior
  - Cloud-mode “server actions only” behavior

### D) Skills (n8n webhooks)
Where skills come from:
- Preferred: MongoDB `skills` collection
- Fallback: `data/skills.json`

Checklist:
- Adding via API: use `/api/skills/add`
- Adding via voice: `JarvisBrain` supports “add skill …” and “enable/disable skill …”
- If you add a new skill schema field, confirm:
  - LLM skill prompt block remains compatible
  - API endpoints allow/validate the field

### E) Auth / sessions / roles
Checklist:
- JWT logic is in `src/utils/auth_tokens.py`
- Legacy sessions (file) are in `src/utils/session_manager.py`
- Principal resolution is in `app.py` (`_get_principal`) and affects authorization everywhere

## 4) Quick Debug Map (Where to Look When Something Breaks)

- UI cannot chat:
  - `jarvis-frontend/src/utils/api.js` (API_URL, request shape)
  - `app.py` `POST /api/chat` (VOICE_ONLY_MODE / CLOUD_MODE / session checks)

- Actions returned but not executed:
  - Cloud mode: UI must call `/api/device/dispatch` for device actions
  - Local mode: `src/core/executor.py` must support that action type

- PC agent connects but receives no jobs:
  - `WS /ws/agent` auth path in `app.py`
  - `DeviceHub` registration
  - device binding (`/api/user/device/*`) and ownership checks

- Permission popup loops / never applies:
  - Server endpoints: `/api/device/permissions` and `/api/device/permissions/grant`
  - Agent handler: `agent_set_permissions` in `apps/pc_agent/pc_agent.py`
  - Persisted permissions location differs between source and packaged agent UI (`pc_agent_app.py` uses %APPDATA%)

- Web research feels empty:
  - `src/core/chat_orchestrator.py` research pipeline
  - `src/core/executor.py` web_search/fetch_url behavior

## 5) Minimal Validation (Do Before You Ship)

- Run unit tests:
  - `pytest -q`
- If you changed chat/action schema:
  - Verify `/api/chat` returns a stable shape (`text`, `actions`, optional `task_id`)
- If you changed device actions:
  - Verify agent connects (`/ws/agent`) and a test dispatch (`/api/device/dispatch`) returns `queued`

## 6) Notes for AI Agents

When asked to implement a feature, always answer these first:
- Is this a **server action** or a **device action**?
- Does it need **admin** role or explicit **permission keys**?
- Must it be disabled in **cloud mode**?
- Which persistent store is involved (Mongo vs `data/*.json` fallback)?
