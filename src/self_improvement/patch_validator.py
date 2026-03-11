from __future__ import annotations

from typing import Any

from src.config.settings import settings


class PatchValidator:
    """Validates whether self-improvement patches are allowed and safe to apply."""

    FORBIDDEN_TOKENS = [
        "rm -rf",
        "os.remove('/",
        "os.remove(\"/",
        "shutil.rmtree('/')",
        "DROP DATABASE",
        "JARVIS_JWT_SECRET",
        "OPENAI_API_KEY",
        "PRIMARY_API_KEY",
    ]

    def validate_patch(self, patch_text: str) -> dict[str, Any]:
        text = patch_text or ""

        if settings.cloud_mode:
            return {
                "status": "blocked",
                "allowed": False,
                "reason": "Self-modification is disabled in cloud mode.",
            }

        for token in self.FORBIDDEN_TOKENS:
            if token.lower() in text.lower():
                return {
                    "status": "blocked",
                    "allowed": False,
                    "reason": f"Forbidden token detected: {token}",
                }

        return {"status": "success", "allowed": True}
