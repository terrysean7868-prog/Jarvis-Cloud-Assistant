import hashlib
import json
import logging
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, NoReturn


HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]

IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = (Path(sys.executable).resolve().parent if IS_FROZEN else REPO_ROOT)

_APPDATA_BASE = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(APP_DIR)
APPDATA_DIR = (Path(_APPDATA_BASE) / "JarvisDesktop").resolve()
LOG_FILE = APPDATA_DIR / "jarvis_desktop.log"


def _ensure_runtime_dirs() -> None:
    try:
        APPDATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def _setup_logging() -> None:
    _ensure_runtime_dirs()
    try:
        logging.basicConfig(
            filename=str(LOG_FILE),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    except Exception:
        pass


def _message_box(title: str, text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def _fatal(title: str, text: str, exc: Exception | None = None) -> NoReturn:
    try:
        _setup_logging()
        logging.error(text)
        if exc is not None:
            logging.error("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass
    _message_box(title, f"{text}\n\nLog: {LOG_FILE}")
    raise SystemExit(text)


try:
    import requests
except Exception as e:
    _fatal("Jarvis Desktop failed to start", "Missing dependency: requests. Install requirements and try again.", e)

try:
    from PySide6 import QtCore, QtGui, QtWidgets
except Exception as e:
    _fatal(
        "Jarvis Desktop failed to start",
        "Missing dependency: PySide6. Install with: pip install -r requirements\\desktop.txt",
        e,
    )


CONFIG_PATH = APPDATA_DIR / "jarvis_desktop_config.json"


def _normalize_phrase(text: str) -> str:
    return " ".join((text or "").lower().strip().split())


def phrase_to_hash(phrase: str) -> str:
    normalized = _normalize_phrase(phrase)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_config() -> Dict[str, Any]:
    try:
        if CONFIG_PATH.exists():
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        # IMPORTANT: Desktop app should not inherit the cloud/server URL used by the PC Agent.
        # Use a desktop-specific env var instead.
        "api_url": os.getenv("JARVIS_DESKTOP_API_URL", "http://127.0.0.1:18001").rstrip("/"),
        "username": "",
        "phrase": "",
        "session_id": "",
    }


def _is_local_api_url(api_url: str) -> bool:
    u = (api_url or "").strip().lower()
    return ("127.0.0.1" in u) or ("localhost" in u)


def save_config(cfg: Dict[str, Any]) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


@dataclass
class AuthResult:
    ok: bool
    message: str
    session_id: str = ""
    username: str = ""
    role: str = ""


class WorkerSignals(QtCore.QObject):
    done = QtCore.Signal(object)


class ThreadWorker(QtCore.QRunnable):
    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    @QtCore.Slot()
    def run(self):
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception as e:
            result = e
        self.signals.done.emit(result)


class JarvisDesktop(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Jarvis Desktop")
        self.resize(980, 720)

        self.pool = QtCore.QThreadPool.globalInstance()
        self.cfg = load_config()

        self.session_id: str = (self.cfg.get("session_id") or "").strip()
        self.username: str = (self.cfg.get("username") or "").strip()

        self._build_ui()
        self._refresh_auth_ui()

    def _build_ui(self):
        root = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(root)

        # --- Connection / Auth ---
        auth_group = QtWidgets.QGroupBox("Connection & Authentication")
        auth_layout = QtWidgets.QGridLayout(auth_group)

        self.api_url = QtWidgets.QLineEdit(self.cfg.get("api_url", "http://127.0.0.1:18001"))
        self.api_url.setPlaceholderText("http://127.0.0.1:18001")

        self.username_in = QtWidgets.QLineEdit(self.cfg.get("username", ""))
        self.username_in.setPlaceholderText("username")

        self.phrase_in = QtWidgets.QLineEdit(self.cfg.get("phrase", ""))
        self.phrase_in.setEchoMode(QtWidgets.QLineEdit.Password)
        self.phrase_in.setPlaceholderText("your short phrase (same for register/login)")

        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["login", "register"])

        self.btn_auth = QtWidgets.QPushButton("Authenticate")
        self.btn_auth.clicked.connect(self.on_auth)

        self.btn_logout = QtWidgets.QPushButton("Logout")
        self.btn_logout.clicked.connect(self.on_logout)

        self.lbl_auth = QtWidgets.QLabel("Not authenticated")
        self.lbl_auth.setWordWrap(True)

        auth_layout.addWidget(QtWidgets.QLabel("API URL"), 0, 0)
        auth_layout.addWidget(self.api_url, 0, 1, 1, 3)
        auth_layout.addWidget(QtWidgets.QLabel("Username"), 1, 0)
        auth_layout.addWidget(self.username_in, 1, 1)
        auth_layout.addWidget(QtWidgets.QLabel("Phrase"), 1, 2)
        auth_layout.addWidget(self.phrase_in, 1, 3)
        auth_layout.addWidget(QtWidgets.QLabel("Mode"), 2, 0)
        auth_layout.addWidget(self.mode, 2, 1)
        auth_layout.addWidget(self.btn_auth, 2, 2)
        auth_layout.addWidget(self.btn_logout, 2, 3)
        auth_layout.addWidget(self.lbl_auth, 3, 0, 1, 4)

        # --- Chat ---
        chat_group = QtWidgets.QGroupBox("Chat")
        chat_layout = QtWidgets.QVBoxLayout(chat_group)

        self.chat_log = QtWidgets.QPlainTextEdit()
        self.chat_log.setReadOnly(True)
        self.chat_log.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)

        input_row = QtWidgets.QHBoxLayout()
        self.msg_in = QtWidgets.QLineEdit()
        self.msg_in.setPlaceholderText("Type a message…")
        self.msg_in.returnPressed.connect(self.on_send)

        self.btn_send = QtWidgets.QPushButton("Send")
        self.btn_send.clicked.connect(self.on_send)

        # Desktop app is local-only and does not expose cloud PC-agent bridging.
        self.btn_get_agent_cfg = QtWidgets.QPushButton("PC Agent (cloud-only)")
        self.btn_get_agent_cfg.setEnabled(False)

        input_row.addWidget(self.msg_in, 1)
        input_row.addWidget(self.btn_send)
        input_row.addWidget(self.btn_get_agent_cfg)

        chat_layout.addWidget(self.chat_log, 1)
        chat_layout.addLayout(input_row)

        layout.addWidget(auth_group)
        layout.addWidget(chat_group, 1)

        self.setCentralWidget(root)

        # Status bar
        self.status = QtWidgets.QStatusBar()
        self.setStatusBar(self.status)

    def _append(self, who: str, text: str):
        text = (text or "").strip()
        if not text:
            return
        self.chat_log.appendPlainText(f"{who}: {text}")
        self.chat_log.verticalScrollBar().setValue(self.chat_log.verticalScrollBar().maximum())

    def _api(self) -> str:
        api = (self.api_url.text() or "").strip().rstrip("/")
        # Prevent accidental cloud usage if a user pastes a Render URL.
        if api and not _is_local_api_url(api):
            raise ValueError(
                "Desktop app supports local API only (http://127.0.0.1:<port>). "
                f"Got: {api}"
            )
        return api

    def _refresh_auth_ui(self):
        authed = bool(self.session_id)
        if authed:
            self.lbl_auth.setText(f"Authenticated as {self.username}")
        else:
            self.lbl_auth.setText("Not authenticated")

        self.btn_logout.setEnabled(authed)
        self.btn_send.setEnabled(authed)
        self.msg_in.setEnabled(authed)
        self.btn_get_agent_cfg.setEnabled(authed)

    def _persist(self):
        try:
            self.cfg["api_url"] = self._api()
        except Exception:
            # If invalid, don't overwrite last known good.
            pass
        self.cfg["username"] = self.username_in.text().strip()
        # Store phrase so user doesn't have to retype; you can clear it if you prefer.
        self.cfg["phrase"] = self.phrase_in.text()
        self.cfg["session_id"] = self.session_id
        save_config(self.cfg)

    # ---------------- Actions ----------------
    def on_auth(self):
        try:
            api = self._api()
        except Exception as e:
            self.status.showMessage(str(e), 7000)
            return
        username = self.username_in.text().strip().lower()
        phrase = self.phrase_in.text()
        mode = self.mode.currentText()

        if not api:
            self.status.showMessage("API URL is required", 4000)
            return
        if not username:
            self.status.showMessage("Username is required", 4000)
            return
        if not phrase.strip():
            self.status.showMessage("Phrase is required (used as voice hash)", 4000)
            return

        self.status.showMessage("Authenticating…")
        self.btn_auth.setEnabled(False)

        worker = ThreadWorker(self._do_auth, api, username, phrase, mode)
        worker.signals.done.connect(self._on_auth_done)
        self.pool.start(worker)

    def _do_auth(self, api: str, username: str, phrase: str, mode: str) -> AuthResult:
        h = phrase_to_hash(phrase)
        payload = {
            "username": username,
            "voice_sample_hash": h,
            "voice_sample_text": _normalize_phrase(phrase),
            "action": mode,
        }
        r = requests.post(f"{api}/api/voice-auth", json=payload, timeout=30)
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("status") == "success":
            return AuthResult(
                ok=True,
                message=data.get("message") or "ok",
                session_id=(data.get("session_id") or ""),
                username=(data.get("username") or username),
                role=(data.get("role") or ""),
            )
        return AuthResult(ok=False, message=data.get("message") or f"HTTP {r.status_code}")

    def _on_auth_done(self, result: object):
        self.btn_auth.setEnabled(True)
        if isinstance(result, Exception):
            self.status.showMessage(f"Auth failed: {result}", 8000)
            return
        if not isinstance(result, AuthResult):
            self.status.showMessage("Auth failed: unknown response", 8000)
            return

        if not result.ok:
            self.status.showMessage(f"Auth failed: {result.message}", 8000)
            return

        self.session_id = result.session_id
        self.username = result.username
        self._persist()
        self._refresh_auth_ui()
        self.status.showMessage("Authenticated", 4000)
        self._append("system", "Authenticated.")

    def on_logout(self):
        try:
            api = self._api()
        except Exception as e:
            self.status.showMessage(str(e), 7000)
            return
        sid = self.session_id
        if not sid:
            return

        self.status.showMessage("Logging out…")
        worker = ThreadWorker(self._do_logout, api, sid)
        worker.signals.done.connect(self._on_logout_done)
        self.pool.start(worker)

    def _do_logout(self, api: str, sid: str) -> bool:
        try:
            r = requests.post(f"{api}/api/logout", json={"session_id": sid}, timeout=15)
            return r.ok
        except Exception:
            return False

    def _on_logout_done(self, ok: object):
        self.session_id = ""
        self.cfg["session_id"] = ""
        save_config(self.cfg)
        self._refresh_auth_ui()
        self.status.showMessage("Logged out", 4000)

    def on_send(self):
        text = self.msg_in.text().strip()
        if not text:
            return
        self.msg_in.clear()
        self._append(self.username or "user", text)

        try:
            api = self._api()
        except Exception as e:
            self._append("system", f"Error: {e}")
            self.status.showMessage("Invalid API URL", 6000)
            return
        sid = self.session_id
        worker = ThreadWorker(self._do_chat, api, text, sid)
        worker.signals.done.connect(self._on_chat_done)
        self.pool.start(worker)

    def _do_chat(self, api: str, text: str, sid: str) -> Dict[str, Any]:
        payload = {"text": text, "mode": "chat", "user": "user", "session_id": sid}
        r = requests.post(f"{api}/api/chat", json=payload, timeout=120)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return {"status": "error", "message": f"HTTP {r.status_code}"}

    def _on_chat_done(self, data: object):
        if isinstance(data, Exception):
            self._append("system", f"Error: {data}")
            self.status.showMessage("Chat error", 6000)
            return

        if not isinstance(data, dict):
            self._append("system", "Error: invalid server response")
            return

        txt = (data.get("text") or data.get("message") or "").strip()
        if txt:
            self._append("Jarvis", txt)

    def on_get_agent_config(self):
        api = self._api()
        sid = self.session_id
        if not sid:
            return
        self.status.showMessage("Fetching agent token/secret…")
        worker = ThreadWorker(self._do_get_agent_config, api, sid)
        worker.signals.done.connect(self._on_get_agent_config_done)
        self.pool.start(worker)

    def _do_get_agent_config(self, api: str, sid: str) -> Dict[str, Any]:
        r = requests.post(f"{api}/api/agent/config", json={"session_id": sid}, timeout=30)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {}

    def _on_get_agent_config_done(self, cfg: object):
        if isinstance(cfg, Exception):
            self.status.showMessage(f"Failed: {cfg}", 8000)
            return
        if not isinstance(cfg, dict):
            self.status.showMessage("Failed: invalid response", 8000)
            return

        token = str(cfg.get("agent_token") or "").strip()
        shared = str(cfg.get("agent_shared_secret") or "").strip()
        if not token and not shared:
            self.status.showMessage("No token/secret returned", 6000)
            return

        msg = []
        if token:
            msg.append(f"Agent token:\n{token}")
        if shared:
            msg.append(f"Shared secret:\n{shared}")
        text = "\n\n".join(msg)

        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle("PC Agent Credentials")
        dlg.setText("Copy the values below.")
        dlg.setDetailedText(text)
        dlg.setStandardButtons(QtWidgets.QMessageBox.Ok)
        dlg.exec()

        # Put both in clipboard for convenience.
        QtWidgets.QApplication.clipboard().setText(text)
        self.status.showMessage("Copied to clipboard", 4000)


def main():
    app = QtWidgets.QApplication([])
    w = JarvisDesktop()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
