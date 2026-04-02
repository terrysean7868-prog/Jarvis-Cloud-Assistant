# Jarvis Cloud Assistant Workspace Instructions

Use this repository as a multi-surface assistant system. Keep changes aligned with the canonical docs and the scoped instruction files under [.github/instructions](.github/instructions).

Primary references:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the system map and runtime flow
- [docs/AGENT_UPDATE_GUIDE.md](docs/AGENT_UPDATE_GUIDE.md) for safe change paths and validation
- [docs/INSTALL.md](docs/INSTALL.md) for setup, run, and build commands
- [.github/instructions/frontend.instructions.md](.github/instructions/frontend.instructions.md) for UI work
- [.github/instructions/backend.instructions.md](.github/instructions/backend.instructions.md) for backend/core Python work
- [.github/instructions/tests.instructions.md](.github/instructions/tests.instructions.md) for pytest work

## Project Logic Map

### Surfaces

- Web backend: [apps/web/app.py](apps/web/app.py)
- Desktop runtime: [apps/desktop/desktop_app.py](apps/desktop/desktop_app.py)
- PC agent: [apps/pc_agent/pc_agent.py](apps/pc_agent/pc_agent.py)
- Web UI: [frontend/](frontend/)
- Core logic: [src/core/](src/core/)

### Main request flow

1. UI sends chat or automation requests to the backend.
2. The backend resolves auth, mode, and policy.
3. `ChatOrchestrator` coordinates brain, LLM, policy, and execution.
4. `JarvisBrain` shapes intent and actions.
5. `LLMAdapter` talks to the model provider and normalizes actions.
6. `ActionExecutor` runs safe server-side actions or hands off device actions.
7. PC-agent tasks are delivered over WebSocket when the action must run on a user machine.
8. Results are pushed back through notifications and persisted where needed.

### Mode rules

- Local mode favors developer convenience and can execute more actions directly.
- Cloud mode requires auth and blocks local/device/file-side execution on the server.
- Device actions belong on the PC agent, not the hosted backend.

### Persistence and state

- MongoDB is the preferred durable store when available.
- JSON files under [data/](data/) are fallback or local-only state.
- Session, task, learning, skill, and device state must stay consistent across the backend and clients.

### Change discipline

- Prefer the canonical implementation files over root compatibility wrappers.
- Keep backend policy, agent permissions, and frontend UX aligned when changing actions.
- Update tests whenever behavior, schemas, or mode restrictions change.
- Link to docs instead of repeating architecture details in new files.

### Common logic areas

- Auth and roles: JWT/session resolution in the backend
- Chat and research: `ChatOrchestrator`, `JarvisBrain`, and `LLMAdapter`
- Execution: `ActionExecutor` plus the PC agent for device-side work
- Desktop startup: health checks and packaging flow
- Skills and autonomy: skill catalog, task manager, and autonomy endpoints

If you are unsure where logic belongs, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first, then edit the canonical implementation file for that surface.