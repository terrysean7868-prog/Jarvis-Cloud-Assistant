# jarvis_service.py - Enhanced Jarvis Cloud Assistant
import os, sys, time, pkgutil, importlib, logging, traceback
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from dotenv import load_dotenv

load_dotenv()

# --- Optional: auto pull/update ---
try:
    import update_repo  # noqa: F401
except Exception:
    pass

from config import TELEGRAM_TOKEN, MONGODB_URI, OPENWEATHER_KEY, DEFAULT_LANG, GITHUB_REPO, GITHUB_TOKEN

if not TELEGRAM_TOKEN:
    print('ERROR: TELEGRAM_TOKEN not configured in environment. Exiting.')
    sys.exit(1)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('jarvis')

# --- Shared service references ---
services = {
    'mongodb_uri': MONGODB_URI,
    'openweather': OPENWEATHER_KEY,
    'github_token': GITHUB_TOKEN,
    'github_repo': GITHUB_REPO,
    'default_lang': DEFAULT_LANG,
}

# --- Import core utils ---
from utils.scheduler import scheduler
from utils import auto_sync

updater = Updater(TELEGRAM_TOKEN, use_context=True)
dp = updater.dispatcher

loaded_modules = []
modules_dir = os.path.join(os.path.dirname(__file__), 'modules')
sys.path.insert(0, modules_dir)


def load_modules():
    """Dynamically import and register all modules."""
    global loaded_modules
    loaded_modules = []
    for finder, name, ispkg in pkgutil.iter_modules([modules_dir]):
        try:
            mod = importlib.import_module(name)
            importlib.reload(mod)  # always reload fresh
            if hasattr(mod, 'register'):
                mod.register(dp, services, scheduler)
                loaded_modules.append({
                    'name': name,
                    'module': mod,
                    'desc': getattr(mod, 'DESCRIPTION', ''),
                })
                logger.info(f'Loaded module: {name}')
        except Exception as e:
            logger.error(f'Failed to load module {name}: {e}')
            traceback.print_exc()


# --- Core Commands ---
def start(update, context):
    update.message.reply_text('Hello Sir — Jarvis Cloud Assistant online. Use /help to see modules.')


def help_cmd(update, context):
    """Improved help command with module descriptions."""
    text = "🧠 *Jarvis Commands (Loaded Modules)*\n\n"
    for m in loaded_modules:
        desc = m.get('desc', '')
        text += f"• `{m['name']}` — {desc}\n"
    text += "\n💡 Use `/reload` to reload modules, or `/status` to check system status."
    update.message.reply_text(text, parse_mode="Markdown")


def status_cmd(update, context):
    """Reports current service status and git info."""
    from datetime import datetime
    git_ok, git_status, git_err = auto_sync.get_git_status()
    status = [
        "📊 *Jarvis Cloud Assistant Status*",
        f"🧩 Loaded Modules: {len(loaded_modules)}",
        f"🕒 Uptime: {datetime.utcnow().isoformat()} UTC",
        f"🌤️ Weather API: {'✅' if OPENWEATHER_KEY else '❌ Missing'}",
        f"🗄️ MongoDB: {'✅' if MONGODB_URI else '❌ Missing'}",
        f"🐙 GitHub: {'✅' if GITHUB_TOKEN and GITHUB_REPO else '❌ Missing'}",
    ]
    if git_ok:
        status.append("📁 Git status:\n" + (git_status.strip() or "Clean"))
    else:
        status.append(f"⚠️ Git check error: {git_err}")
    update.message.reply_text("\n".join(status), parse_mode="Markdown")


def reload_cmd(update, context):
    """Reload all modules on demand and optionally auto-sync."""
    update.message.reply_text("♻️ Reloading modules...")
    load_modules()
    update.message.reply_text(f"✅ Reloaded {len(loaded_modules)} modules successfully.")
    # Optional auto-sync after reload
    if os.environ.get("AUTO_SYNC_AFTER_RELOAD", "0") == "1":
        ok, msg = auto_sync.git_commit_and_push(
            __file__,
            f"Auto-sync: Reloaded {len(loaded_modules)} modules"
        )
        update.message.reply_text(f"🔄 {msg}")


def text_handler(update, context):
    """Handles free-text commands like 'add module <name>'."""
    text = (update.message.text or '').strip()
    if not text:
        update.message.reply_text('Sorry Sir, I did not understand. Use /help.')
        return
    lower = text.lower()
    if lower.startswith('add module '):
        modname = lower.split('add module ', 1)[1].strip()
        update.message.reply_text(f'⚙️ Attempting to add module *{modname}* via automation pipeline.', parse_mode="Markdown")
        token = services.get('github_token')
        repo = services.get('github_repo')
        if token and repo:
            import requests
            url = f'https://api.github.com/repos/{repo}/dispatches'
            headers = {
                'Authorization': f'token {token}',
                'Accept': 'application/vnd.github.everest-preview+json'
            }
            payload = {'event_type': 'generate-module', 'client_payload': {'module': modname}}
            r = requests.post(url, headers=headers, json=payload)
            if r.status_code in (200, 204):
                update.message.reply_text('✅ Module generation triggered. Watch GitHub Actions for progress.')
                if os.environ.get("AUTO_SYNC_AFTER_RELOAD", "0") == "1":
                    auto_sync.git_commit_and_push(__file__, f"Triggered module add: {modname}")
            else:
                update.message.reply_text(f'❌ Failed to trigger automation: {r.text}')
        else:
            update.message.reply_text('⚠️ Automation token or repo not configured.')
        return
    update.message.reply_text('Sorry Sir, I did not understand. Use /help.')


# --- Register core handlers ---
dp.add_handler(CommandHandler('start', start))
dp.add_handler(CommandHandler('help', help_cmd))
dp.add_handler(CommandHandler('status', status_cmd))
dp.add_handler(CommandHandler('reload', reload_cmd))
dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_handler))

# --- Initial module load ---
load_modules()

# --- Optional Voice Integration ---
if os.environ.get("ENABLE_VOICE", "0") == "1":
    try:
        import modules.voice as voice
        logger.info("🎤 Voice module enabled.")
    except Exception as e:
        logger.error(f"Voice integration failed: {e}")

# --- Start service ---
if __name__ == '__main__':
    updater.start_polling()
    logger.info('Jarvis service started.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        updater.stop()
        scheduler.shutdown()
