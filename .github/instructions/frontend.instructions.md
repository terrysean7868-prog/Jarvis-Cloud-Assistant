---
description: "Frontend React guidance for Jarvis UI work. Use when editing files under frontend/src/ or frontend/public/."
applyTo:
  - "frontend/src/**/*.js"
  - "frontend/src/**/*.jsx"
  - "frontend/src/**/*.css"
  - "frontend/public/**/*"
---

# Frontend Guidance

- Prefer the existing React app structure in [frontend/](frontend/) and the API client in [frontend/src/utils/api.js](frontend/src/utils/api.js).
- Keep UI changes consistent with the current component and styling patterns in [frontend/src/components/](frontend/src/components/) and [frontend/src/styles/](frontend/src/styles/).
- Use functional components and hooks; follow the existing lazy-loading and `Suspense` patterns for heavier screens.
- Preserve WebSocket, auth, and backend contract expectations when changing chat, autonomy, or device-control flows.
- Keep browser API usage guarded and resilient to missing permissions or unsupported environments.
- Prefer linking to [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/INSTALL.md](docs/INSTALL.md) rather than restating architecture or setup details.
- When both lifecycle status and concrete `action_results` are present, prefer rendering/speaking the concrete results instead of queued/awaiting placeholders.
- Keep delegated result UX aligned with backend normalized statuses (`ok`, `error`, `forbidden`) and `success`/`error` fields.
- Preserve pending-requirement and pending-clarification continuity cues in UI messaging so users can resume interrupted tasks cleanly.
