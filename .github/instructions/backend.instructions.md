---
description: "Backend and core Python guidance for Jarvis API, orchestration, and agent behavior. Use when editing server or core runtime Python files."
applyTo:
  - "apps/web/**/*.py"
  - "src/core/**/*.py"
  - "src/utils/**/*.py"
  - "apps/pc_agent/**/*.py"
---

# Backend Guidance

- Treat [apps/web/app.py](apps/web/app.py) as the canonical FastAPI entrypoint and [apps/pc_agent/pc_agent.py](apps/pc_agent/pc_agent.py) as the canonical PC agent implementation.
- Keep the cloud/local split intact. Changes that affect action execution, permissions, auth, or device dispatch should be checked against cloud-mode restrictions.
- Preserve the action contract across the brain, orchestrator, executor, PC agent, and frontend. If one side changes, verify the others still agree.
- Prefer small, explicit Python changes that fit the existing async and Pydantic style.
- Keep compatibility wrappers at the repository root unchanged unless a wrapper must be updated for compatibility.
- Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and [docs/AGENT_UPDATE_GUIDE.md](docs/AGENT_UPDATE_GUIDE.md) for system boundaries, routing, and safe change paths.
- Treat [src/core/llm_adapter.py](src/core/llm_adapter.py) as the behavior/intelligence layer: intent parsing, multi-step chaining, clarification memory, and response tone should be changed there first.
- Maintain execution-result normalization in backend result paths: rely on canonical `success` and `error` semantics and preserve status mapping (`ok`, `error`, `forbidden`) for downstream UI/task updates.
- Preserve clarification continuity behavior: when required details are missing, backend/adapter should store pending clarification context, resume the original task when details arrive, and allow side questions without losing pending state.
- Avoid hardcoded deployment hosts/origins in runtime logic; prefer environment-driven config (for example `JARVIS_PUBLIC_SERVER_URL`, `JARVIS_CORS_ORIGINS`) with explicit safe defaults.
- When adding action aliases/wrappers (especially `device_action`), preserve compatibility with both wrapped and direct action types so cloud dispatch and PC agent stay contract-safe.
