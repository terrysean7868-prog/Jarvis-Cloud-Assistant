# Install

This repo has multiple runnable parts. Use the matching dependency set so installs are repeatable.

## Web (Backend API)

- Python deps:
  - `pip install -r requirements/web.txt`
- Run locally:
  - `run_local.bat`
  - or `python run_local.py --reload`

## UI (React)

- Node deps:
  - `cd frontend`
  - `npm install`
- Dev server:
  - `npm start`
- Production build:
  - `npm run build`

## Desktop (Jarvis Desktop)

- Python deps:
  - `pip install -r requirements/desktop.txt`
- Run (new canonical desktop app):
  - `python apps/desktop/desktop_app.py`
- Backward-compatible launchers (still supported):
  - `python apps/desktop/jarvis_desktop.py`
  - `python apps/desktop/jarvis_web_shell.py`
- Packaging note:
  - The desktop PyInstaller spec is `JarvisDesktop.spec`.

### Desktop builder (latest package each run)

- One-command build (Windows):
  - `build_desktop_app.bat`
- Cross-platform/advanced options:
  - `python scripts/build_desktop_app.py`
  - optional flags: `--install-frontend`, `--skip-frontend`, `--skip-clean`
- What it does:
  - rebuilds `frontend/build`
  - runs PyInstaller on `JarvisDesktop.spec`
  - outputs latest desktop app under `dist/`

### Desktop startup note (important)

- The desktop app waits for backend health at:
  - `GET /health` (primary)
  - `GET /api/health` (fallback for compatibility)
- If you change desktop runtime or backend startup behavior, rebuild `dist/Jarvis.exe` before testing.

### Desktop startup troubleshooting

If desktop shows "Backend not ready at http://127.0.0.1:18001":

1) Check if port is already in use:
  - `Get-NetTCPConnection -LocalPort 18001 -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess`
2) Inspect owning process:
  - `Get-Process -Id <OwningProcess>`
3) Verify backend health manually:
  - open `http://127.0.0.1:18001/health`
4) Rebuild desktop executable after code changes:
  - `build_desktop_app.bat`

## PC Agent (headless)

- Python deps:
  - `pip install -r requirements/pc_agent.txt`
- Run:
  - `run_pc_agent.bat`
  - or `python apps/pc_agent/pc_agent.py`

## PC Agent (Desktop UI)

- Python deps:
  - `pip install -r requirements/pc_agent_desktop.txt`
- Run:
  - `python apps/pc_agent/pc_agent_app.py --ui`
- Packaging note:
  - The PyInstaller spec is `JarvisPCAgent.spec`.

## Render (cloud)

- Render build uses `requirements/render.txt` (see `.render-build.sh`).

## Cloud Owned Model (Self-Hosted)

- To run your own OpenAI-compatible model service and train LoRA adapters, see:
  - `docs/CLOUD_OWNED_MODEL.md`

## Autonomous OSS Stack (Optional, Recommended)

Jarvis now includes an autonomous multi-agent runtime with optional open-source AI integrations.

- Install full local stack:
  - `pip install -r requirements/full.txt`
- Start local Ollama server (separate terminal):
  - `ollama serve`
- Pull a local coding/reasoning model:
  - `ollama pull llama3.1:8b`

Runtime defaults are in `src/config/runtime_defaults.py`:

- `LLM_PROVIDER = "ollama"`
- `PRIMARY_ENDPOINT = "http://127.0.0.1:11434/api/chat"`
- `AUTONOMY_ENABLED = True`

Optional integrations:

- ChromaDB for vector memory (`chromadb`)
- Whisper for local speech-to-text (`openai-whisper`)
- OpenCV for image/screen analysis (`opencv-python` / `opencv-python-headless`)
- APScheduler for background jobs (already included in core dependencies)

Autonomy API endpoints:

- `GET /api/autonomy/status`
- `POST /api/autonomy/goals`
- `GET /api/autonomy/goals`
- `GET /api/tasks`
- `GET /api/agents`
- `GET /api/device/list`
- `GET /api/self-improvement/proposals`
- `POST /api/self-improvement/proposals/decision`

## Autonomous Engineer Modules

Core autonomous modules are organized as:

- `src/autonomy/` (goal manager, runtime loop, OSS stack adapters)
- `src/agents/` (ResearchAgent, CodingAgent, DevOpsAgent, AutomationAgent, DeviceAgent, MonitoringAgent)
- `src/planning/` (task graph + dependency planner)
- `src/memory/` (knowledge store + vector abstraction)
- `src/tools/` (plugin tool registry + dynamic discovery)
- `src/learning/` (reflection/lessons engine)
- `src/safety/` (risk scoring and execution policy)
- `src/devops/` (Docker/build/deploy helpers)

Each autonomous agent supports:

- `plan()`
- `execute()`
- `evaluate()`

The dynamic router is:

- `src/agents/agent_controller.py`

## Open-Source Integrations (All Optional)

The runtime is open-source only (no paid API requirement):

- Ollama local runtime
- ChromaDB vector memory
- Whisper speech recognition
- OpenCV vision/screen analysis
- APScheduler background autonomy ticks

Toggle integrations in `src/config/runtime_defaults.py`:

- `ENABLE_OLLAMA`
- `ENABLE_CHROMADB`
- `ENABLE_WHISPER`
- `ENABLE_OPENCV`
- `AUTONOMY_ENABLED`

## Background Autonomy Loop

When enabled, Jarvis continuously executes:

1. read pending goals from MongoDB
2. build task graph
3. dispatch specialized agents
4. execute tools
5. evaluate and store reflections

Safety policy:

- LOW risk -> execute
- MEDIUM risk -> confirmation required
- HIGH risk -> block

## Tool Plugins and Self-Extension

Tools are auto-discovered from `src/tools/`.

Tool contract:

- `name`
- `description`
- `run(**kwargs)`

CodingAgent can generate new tool modules and trigger tool registry reload dynamically.
