# Render Sleep / Keep-Alive (Free Plan)

Render free web services can **sleep when idle**. That behavior is controlled by Render and cannot be fully disabled from inside your app.

This project provides a lightweight endpoint for uptime checks:

- `GET /health` (fast, always 200 if the process is alive)
- `GET /health?check_db=1` (best-effort DB ping; still returns 200)

## Recommended Fix (Works on Free Plan)

Use an external monitor to ping your service every 5 minutes:

### Option A: UptimeRobot (easiest)
1. Create a monitor: **HTTP(s)**
2. URL: `https://<your-render-service>.onrender.com/health`
3. Interval: **5 minutes**

### Option B: Cron on another machine/server
Run:

`curl -fsS https://<your-render-service>.onrender.com/health >/dev/null`

## Best Fix

Upgrade to a Render plan that does not sleep.

## Notes

- This does not change Jarvis UX; it only keeps the Render instance warm.
- If you have multiple services (frontend + backend), ping the backend `/health`.
