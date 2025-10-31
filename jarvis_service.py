# jarvis_service.py - Jarvis Cloud Assistant (AI + Voice Enhanced)
import os, sys, time, pkgutil, importlib, logging, traceback, json
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from dotenv import load_dotenv
import requests

load_dotenv()

# Try auto-pull
try:
    import update_repo  # noqa: F401
except Exception:
    pass

from config import (
    TELEGRAM_TOKEN, MONGODB_URI, OPENWEATHER_KEY,
    DEFAULT_LANG, GITHUB_REPO, GITHUB_TOKEN
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TELEGRAM_TOKEN:
    print("ERROR: TELEGRAM_TOKEN not configured in environment. Exiting.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis")

# --- Shared Services ---
services = {
    "mongodb_uri": MONGODB_URI,
    "openweather": OPENWEATHER_KEY,
    "github_token": GITHUB_TOKEN,
    "github_repo": GITHUB_REPO,
    "default_lang": DEFAULT_LANG,
    "openai_key": OPENAI_API_KEY
}

from utils.scheduler import scheduler
from utils import auto_sync

updater = Updater(TELEGRAM_TOKEN, use_context=True)
dp = updater.dispatcher

loaded_modules = []
modules_dir = os.path.join(os.path.dirname(__file__), "modules")
sys.path.insert(0, modules_dir)


def load_modules():
    """Dynamically import and register all modules."""
    global loaded_modules
    loaded_modules = []
    for finder, name, ispkg in pkgutil.iter_modules([modules_dir]):
        try:
            mod = importlib.import_module(name)
            importlib.reload(mod)
            if hasattr(mod, "register"):
                mod.register(dp, services, scheduler)
                loaded_modules.append({
                    "name": name,
                    "module": mod,
                    "desc": getattr(mod, "DESCRIPTION", "")
                })
                logger.info(f"Loaded module: {name}")
        except Exception as e:
            logger.error(f"Failed to load module {name}: {e}")
            traceback.print_exc()


# --- Core Commands ---
def start(update, context):
    update.message.reply_text("Hello Sir — Jarvis Cloud Assistant online. Use /help to see modules.")


def help_cmd(update, context):
    text = "🧠 *Jarvis Commands (Loaded Modules)*\n\n"
    for m in loaded_modules:
        desc = m.get("desc", "")
        text += f"• `{m['name']}` — {desc}\n"
    text += "\n💡 `/reload` reloads modules | `/status` checks system | `/autosync` commits to GitHub"
    update.message.reply_text(text, parse_mode="Markdown")


def status_cmd(update, context):
    """Report health and git status."""
    from datetime import datetime
    git_ok, git_status, git_err = auto_sync.get_git_status()
    status = [
        "📊 *Jarvis Status*",
        f"🧩 Modules: {len(loaded_modules)}",
        f"🕒 Uptime: {datetime.utcnow().isoformat()} UTC",
        f"🌤️ Weather: {'✅' if OPENWEATHER_KEY else '❌'}",
        f"🗄️ MongoDB: {'✅' if MONGODB_URI else '❌'}",
        f"🐙 GitHub: {'✅' if GITHUB_TOKEN and GITHUB_REPO else '❌'}",
        f"🤖 OpenAI: {'✅' if OPENAI_API_KEY else '❌'}",
    ]
    if git_ok:
        status.append("📁 Git:\n" + (git_status.strip() or "Clean"))
    else:
        status.append(f"⚠️ Git Error: {git_err}")
    update.message.reply_text("\n".join(status), parse_mode="Markdown")


def reload_cmd(update, context):
    update.message.reply_text("♻️ Reloading modules...")
    load_modules()
    update.message.reply_text(f"✅ Reloaded {len(loaded_modules)} modules.")


def autosync_cmd(update, context):
    """Manually trigger GitHub sync."""
    update.message.reply_text("🔄 Starting auto-sync...")
    ok, msg = auto_sync.git_commit_and_push(__file__, "Manual auto-sync triggered by user")
    update.message.reply_text(f"📦 {msg}")


def text_handler(update, context):
    """Handles text like 'add module xyz'."""
    text = (update.message.text or "").strip()
    if not text:
        update.message.reply_text("Sorry Sir, I did not understand. Use /help.")
        return
    lower = text.lower()
    if lower.startswith("add module "):
        modname = lower.split("add module ", 1)[1].strip()
        trigger_module_add(update, modname)
    else:
        update.message.reply_text("Sorry Sir, I did not understand. Use /help.")


def trigger_module_add(update, modname):
    """Triggers GitHub dispatch for new module creation."""
    token, repo = services.get("github_token"), services.get("github_repo")
    if token and repo:
        url = f"https://api.github.com/repos/{repo}/dispatches"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.everest-preview+json"
        }
        payload = {"event_type": "generate-module", "client_payload": {"module": modname}}
        r = requests.post(url, headers=headers, json=payload)
        if r.status_code in (200, 204):
            update.message.reply_text(f"✅ Module generation triggered: `{modname}`", parse_mode="Markdown")
        else:
            update.message.reply_text(f"❌ GitHub dispatch failed: {r.text}")
    else:
        update.message.reply_text("⚠️ GitHub credentials not configured.")


# --- OpenAI Integration for Voice ---
def interpret_voice_command(text):
    """Send user speech text to OpenAI for intent detection."""
    if not OPENAI_API_KEY:
        return None
    prompt = f"""You are Jarvis AI agent.
User said: "{text}".
If it’s a request to create a new Jarvis module, respond only with: add module <name>.
If not, respond with 'none'."""
    try:
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 30,
            },
            timeout=10
        )
        result = r.json()
        cmd = result["choices"][0]["message"]["content"].strip().lower()
        return cmd
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return None


# --- Voice Integration (Extended) ---
if os.environ.get("ENABLE_VOICE", "0") == "1":
    from modules import voice
    logger.info("🎤 Voice module active with AI integration.")

    def enhanced_voice_handler(update, context):
        """Wrap voice module: speech → text → AI → action."""
        user = update.message.from_user.first_name or "User"
        import speech_recognition as sr
        from pydub import AudioSegment
        ogg, wav = "voice.ogg", "voice.wav"
        try:
            file = update.message.voice.get_file()
            file.download(ogg)
            AudioSegment.from_file(ogg).export(wav, format="wav")
            rec = sr.Recognizer()
            with sr.AudioFile(wav) as src:
                audio = rec.record(src)
                text = rec.recognize_google(audio, language="en-IN")

            update.message.reply_text(f"{user}, you said: {text}")
            cmd = interpret_voice_command(text)
            if cmd and cmd.startswith("add module"):
                modname = cmd.replace("add module", "").strip()
                update.message.reply_text(f"🧠 AI detected new module request: {modname}")
                trigger_module_add(update, modname)
            elif cmd == "none":
                update.message.reply_text("Okay Sir, no module action detected.")
            else:
                update.message.reply_text("⚙️ Unable to interpret voice command clearly.")
        except Exception as e:
            update.message.reply_text(f"🎤 Voice error: {e}")
        finally:
            for f in (ogg, wav):
                if os.path.exists(f):
                    os.remove(f)

    dp.add_handler(MessageHandler(Filters.voice, enhanced_voice_handler))


# --- Register base handlers ---
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("help", help_cmd))
dp.add_handler(CommandHandler("status", status_cmd))
dp.add_handler(CommandHandler("reload", reload_cmd))
dp.add_handler(CommandHandler("autosync", autosync_cmd))
dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_handler))

# --- Initial module load ---
load_modules()

# --- Start Bot ---
if __name__ == "__main__":
    updater.start_polling()
    logger.info("Jarvis service started.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        updater.stop()
        scheduler.shutdown()
