# jarvis_service.py - Jarvis Cloud Assistant (Replit-ready)
import os, sys, time, pkgutil, importlib, logging
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters
from dotenv import load_dotenv
load_dotenv()

# try autopull
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

services = {
    'mongodb_uri': MONGODB_URI,
    'openweather': OPENWEATHER_KEY,
    'github_token': GITHUB_TOKEN,
    'github_repo': GITHUB_REPO,
    'default_lang': DEFAULT_LANG
}

from utils.scheduler import scheduler

updater = Updater(TELEGRAM_TOKEN, use_context=True)
dp = updater.dispatcher

def start(update, context):
    update.message.reply_text('Hello Sir — Jarvis Cloud Assistant online. Use /help to see modules.')

def help_cmd(update, context):
    text = 'Jarvis commands (loaded modules):\n'
    for m in loaded_modules:
        text += '- {} : {}\n'.format(m['name'], m.get('desc',''))
    text += '\nYou can request new modules by sending: add module <name>'
    update.message.reply_text(text)

dp.add_handler(CommandHandler('start', start))
dp.add_handler(CommandHandler('help', help_cmd))

loaded_modules = []
modules_dir = os.path.join(os.path.dirname(__file__), 'modules')
sys.path.insert(0, modules_dir)
for finder, name, ispkg in pkgutil.iter_modules([modules_dir]):
    try:
        mod = importlib.import_module(name)
        if hasattr(mod, 'register'):
            mod.register(dp, services, scheduler)
            loaded_modules.append({'name': name, 'module': mod, 'desc': getattr(mod, 'DESCRIPTION', '')})
            logger.info('Loaded module: %s', name)
    except Exception as e:
        logger.exception('Failed to load module %s: %s', name, e)

def text_handler(update, context):
    text = (update.message.text or '').strip()
    if not text:
        update.message.reply_text('Sorry Sir, I did not understand. Use /help.')
        return
    lower = text.lower()
    if lower.startswith('add module '):
        modname = lower.split('add module ',1)[1].strip()
        update.message.reply_text('Attempting to add module {} via automation pipeline.'.format(modname))
        token = services.get('github_token')
        repo = services.get('github_repo')
        if token and repo:
            import requests, json
            url = 'https://api.github.com/repos/{}/dispatches'.format(repo)
            headers = {'Authorization': 'token {}'.format(token), 'Accept': 'application/vnd.github.everest-preview+json'}
            payload = {'event_type':'generate-module', 'client_payload': {'module': modname}}
            r = requests.post(url, headers=headers, json=payload)
            if r.status_code in (200,204):
                update.message.reply_text('Module generation triggered. Watch GitHub PRs for progress.')
            else:
                update.message.reply_text('Failed to trigger automation. Check server logs.')
        else:
            update.message.reply_text('Automation token or repo not configured.')
        return
    update.message.reply_text('Sorry Sir, I did not understand. Use /help.')

dp.add_handler(MessageHandler(Filters.text & (~Filters.command), text_handler))

if __name__ == '__main__':
    updater.start_polling()
    logger.info('Jarvis service started.')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        updater.stop()
        scheduler.shutdown()
