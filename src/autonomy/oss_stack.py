from __future__ import annotations

from pathlib import Path

from src.config import runtime_defaults as rd

try:
    import cv2
except Exception:
    cv2 = None

try:
    import whisper  # type: ignore
except Exception:
    whisper = None

try:
    import aiohttp
except Exception:
    aiohttp = None


class OSSStack:
    """Optional OSS integrations: Ollama, Whisper, OpenCV."""

    def __init__(self, ollama_url: str = "http://127.0.0.1:11434"):
        self.ollama_url = ollama_url.rstrip("/")
        self._whisper_model = None

    def capabilities(self) -> dict:
        return {
            "ollama_enabled": bool(getattr(rd, "ENABLE_OLLAMA", True)),
            "chromadb_enabled": bool(getattr(rd, "ENABLE_CHROMADB", True)),
            "whisper_enabled": bool(getattr(rd, "ENABLE_WHISPER", False)),
            "opencv_enabled": bool(getattr(rd, "ENABLE_OPENCV", True)),
            "opencv_available": cv2 is not None,
            "whisper_available": whisper is not None,
            "aiohttp_available": aiohttp is not None,
            "ollama_url": self.ollama_url,
        }

    async def ollama_generate(self, *, model: str, prompt: str) -> dict:
        if not bool(getattr(rd, "ENABLE_OLLAMA", True)):
            return {"status": "error", "message": "Ollama integration is disabled by config"}
        if aiohttp is None:
            return {"status": "error", "message": "aiohttp is required for Ollama integration"}

        url = f"{self.ollama_url}/api/generate"
        payload = {"model": model, "prompt": prompt, "stream": False}
        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status != 200:
                        return {"status": "error", "message": await resp.text()}
                    data = await resp.json()
                    return {"status": "success", "response": data.get("response", "")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def transcribe_audio(self, audio_path: str, model_name: str = "base") -> dict:
        if not bool(getattr(rd, "ENABLE_WHISPER", False)):
            return {"status": "error", "message": "Whisper integration is disabled by config"}
        if whisper is None:
            return {"status": "error", "message": "whisper is not installed"}

        try:
            if self._whisper_model is None:
                self._whisper_model = whisper.load_model(model_name)
            result = self._whisper_model.transcribe(audio_path)
            return {"status": "success", "text": result.get("text", "")}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def analyze_image(self, image_path: str) -> dict:
        if not bool(getattr(rd, "ENABLE_OPENCV", True)):
            return {"status": "error", "message": "OpenCV integration is disabled by config"}
        if cv2 is None:
            return {"status": "error", "message": "opencv-python is not installed"}
        p = Path(image_path)
        if not p.exists():
            return {"status": "error", "message": f"Image not found: {image_path}"}
        img = cv2.imread(str(p))
        if img is None:
            return {"status": "error", "message": "OpenCV failed to read image"}

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 100, 200)
        edge_pixels = int((edges > 0).sum())
        return {
            "status": "success",
            "width": int(img.shape[1]),
            "height": int(img.shape[0]),
            "edge_pixels": edge_pixels,
        }
