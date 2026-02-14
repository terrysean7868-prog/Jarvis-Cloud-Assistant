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
  - `cd jarvis-frontend`
  - `npm install`
- Dev server:
  - `npm start`
- Production build:
  - `npm run build`

## Desktop (Jarvis Desktop)

- Python deps:
  - `pip install -r requirements/desktop.txt`
- Run (dev):
  - `python apps/desktop/jarvis_desktop.py`
- Run (web-shell desktop wrapper):
  - `python apps/desktop/jarvis_web_shell.py`
- Packaging note:
  - The desktop PyInstaller spec is `JarvisDesktop.spec`.

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
