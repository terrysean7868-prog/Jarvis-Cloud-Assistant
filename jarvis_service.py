# jarvis_service.py - Jarvis Cloud Assistant (Autonomous AI Agent)
import os, sys, time, pkgutil, importlib, logging
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from dotenv import load_dotenv
load_dotenv()

# Try pulling latest repo automatically (non-blocking)
try:
    import update_repo  # noqa: F401
except Exception:
    pass

from config import TELEGRAM_TOKEN, MONGODB_URI, OPENWEATHER_KEY, DEFAULT_LANG, GITHUB_REPO, GITHUB_TOKEN

if not TELEGRAM_TOKEN:
    print("❌ ERROR: TELEGRAM_TOKEN not configured in environment. Exiting.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("jarvis")

# --- Global services dictionary ---
services = {
    "mongodb_uri": MONGODB_URI,
    "openweather": OPENWEATHER_KEY,
    "github_token": GITHUB_TOKEN,
    "github_repo": GITHUB_REPO,
    "default_lang": DEFAULT_LANG,
}

from utils.scheduler import scheduler

# --- Initialize Telegram Bot ---
updater = Updater(TELEGRAM_TOKEN, use_context=True)
dp = updater.dispatcher

# ---------------------------------------------------------------------------
# Core Commands
# ---------------------------------------------------------------------------
def start(update, context):
    update.message.reply_text("🤖 Hello Sir — Jarvis Cloud Assistant online.\nUse /help to see modules.")

def help_cmd(update, context):
    text = "🧠 Jarvis commands (loaded modules):\n"
    for m in loaded_modules:
        text += f"- {m['name']} : {m.get('desc', '')}\n"
    text += "\nYou can say or type:\n• add module <name>\n• update module <name>\n• /reload — Reload modules\n• /autosync — Push changes to GitHub"
    update.message.reply_text(text)

dp.add_handler(CommandHandler("start", start))
dp.add_handler(CommandHandler("help", help_cmd))

# ---------------------------------------------------------------------------
# Dynamic Module Loading
# ---------------------------------------------------------------------------
loaded_modules = []
modules_dir = os.path.join(os.path.dirname(__file__), "modules")
sys.path.insert(0, modules_dir)

def load_all_modules():
    global loaded_modules
    loaded_modules.clear()
    for finder, name, ispkg in pkgutil.iter_modules([modules_dir]):
        try:
            mod = importlib.import_module(name)
            if hasattr(mod, "register"):
                mod.register(dp, services, scheduler)
                loaded_modules.append({"name": name, "module": mod, "desc": getattr(mod, "DESCRIPTION", "")})
                logger.info(f"✅ Loaded module: {name}")
        except Exception as e:
            logger.exception(f"❌ Failed to load module {name}: {e}")

# Load modules on startup
load_all_modules()

# ---------------------------------------------------------------------------
# Text-based command handler (fallback)
# ---------------------------------------------------------------------------
def text_handler(update, context):
    text = (update.message.text or "").strip()
    if not text:
        update.message.reply_text("Sorry Sir, I didn’t catch that. Use /help.")
        return

    lower = text.lower()
    if lower.startswith("add module "):
        modname = lower.split("add module ", 1)[1].strip()
        update.message.reply_text(f"🧠 Attempting to create module: {modname}")
        try:
            from modules import auto_update
            auto_update.create_module_from_voice(update, modname)
        except Exception as e:
            update.message.reply_text(f"❌ Auto module creation failed: {e}")
        return

    if lower.startswith("update module "):
        modname = lower.split("update module ", 1)[1].strip()
        update.message.reply_text(f"🧠 Attempting to update module: {modname}")
        try:
            from modules import auto_update
            auto_update.update_module_from_voice(update, modname)
        except Exception as e:
            update.message.reply_text(f"❌ Auto module update failed: {e}")
        return

    update.message.reply_text("⚙️ Sorry Sir, I did not understand. Use /help.")

dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_handler))

# ---------------------------------------------------------------------------
# Git AutoSync & Module Reload
# ---------------------------------------------------------------------------
from utils.auto_sync import git_commit_and_push
import importlib

def autosync(update, context):
    success, msg = git_commit_and_push(".", "Manual autosync triggered")
    update.message.reply_text(f"🔁 AutoSync: {msg}")

def reload_modules(update, context):
    global loaded_modules
    reloaded, failed = [], []
    for m in loaded_modules:
        name = m["name"]
        try:
            if name in sys.modules:
                importlib.reload(sys.modules[name])
            else:
                importlib.import_module(name)
            reloaded.append(name)
        except Exception as e:
            logger.exception(f"Failed to reload {name}: {e}")
            failed.append(name)

    # Reload global module list
    load_all_modules()
    update.message.reply_text(f"♻️ Reloaded: {', '.join(reloaded) or 'None'}\n❌ Failed: {', '.join(failed) or 'None'}")

dp.add_handler(CommandHandler("autosync", autosync))
dp.add_handler(CommandHandler("reload", reload_modules))

# ---------------------------------------------------------------------------
# Bot Main Loop
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    updater.start_polling()
    logger.info("🚀 Jarvis service started and listening.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        updater.stop()
        scheduler.shutdown()

# ---------------------------------------------------------------------------
# Optional: Voice Integration Auto-Start
# ---------------------------------------------------------------------------
if os.environ.get("ENABLE_VOICE", "0") == "1":
    try:
        from modules.voice import register as voice_register
        voice_register(dp, services, scheduler)
        print("🎙️ Voice listener loaded successfully.")
    except Exception as e:
        print("⚠️ Failed to start voice listener:", e)
