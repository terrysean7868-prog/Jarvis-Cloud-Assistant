# Digital Assistant Platform

A small, self-hostable digital assistant built with a FastAPI backend and an optional React web UI. It supports voice-based authentication (browser speech-to-text), session handling, and a growing set of assistant “tools” (internet fetch/search, memory, local automation when running on your own PC).

This README is intentionally generic and avoids any personal assistant details.

## What’s included

- Backend API (FastAPI) in `app.py`
- Optional React frontend in `jarvis-frontend/`
- Health endpoint for monitoring: `GET /health` (optional DB ping: `/health?check_db=1`)
- Render deployment assets: `render.yaml`, `.render-build.sh`, `requirements.render.txt`

## Quick start (Windows)

1. Create a local environment file:
   - Copy `.env.template` to `.env` and fill in the values you need.

2. Start locally:
   - `./startup.ps1` (if you use the PowerShell launcher)
   - or `./run_local.bat`

3. Open:
   - Frontend (dev): http://localhost:3000
   - Backend: http://localhost:8000
   - API docs: http://localhost:8000/docs

## Manual setup

Backend:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Frontend:

```powershell
cd jarvis-frontend
npm install
npm start
```

## Voice authentication tip

For best results, speak the same short phrase during registration and login (minor transcript differences can affect matching).

## Cloud vs local safety

If you deploy this to a cloud host, treat it as “cloud mode”: avoid exposing local-PC automation features to the public internet unless you have a strong security model in place.

## Render keep-alive

On the Render free plan, services can sleep when idle. If you need higher availability, use an external monitor to ping the health endpoint.

- docs/RENDER_SLEEP_KEEPALIVE.md

## Notes on secrets

- Keeping `.env` locally is fine for running on your PC.
- For cloud deployments, prefer host environment variables instead of committing secrets.
