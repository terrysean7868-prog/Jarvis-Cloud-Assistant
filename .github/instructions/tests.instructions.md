---
description: "Test guidance for Jarvis pytest files and fixtures. Use when editing tests or test helpers under tests/."
applyTo:
  - "tests/**/*.py"
---

# Test Guidance

- Follow the existing pytest style in [tests/](tests/) and [pyproject.toml](pyproject.toml).
- Prefer targeted tests that cover the behavior being changed rather than broad end-to-end rewrites.
- Match the repo's async patterns and fixture style, including `monkeypatch`, `tmp_path`, and session-level cleanup where needed.
- When patching modules, import the canonical module directly if you need to replace private helpers or module-level state.
- Keep tests deterministic and isolated from external services when possible.
- Use [docs/AGENT_UPDATE_GUIDE.md](docs/AGENT_UPDATE_GUIDE.md) as the reference for what should be validated after behavior changes.
- For behavior-layer changes in [src/core/llm_adapter.py](src/core/llm_adapter.py), add focused tests for:
  - compound intent detection and ordered action-chain generation,
  - clarification capture/resume flow,
  - continuation behavior when side questions occur during pending clarification.
- For delegated execution/result changes, include targeted assertions that normalized status mapping and `success`/`error` interpretation remain consistent across backend-visible payloads.
