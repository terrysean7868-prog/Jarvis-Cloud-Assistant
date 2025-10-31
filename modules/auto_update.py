# modules/auto_update.py
import os, openai, logging, importlib, sys
from utils.auto_sync import git_commit_and_push

logger = logging.getLogger("jarvis.auto_update")
openai.api_key = os.getenv("OPENAI_API_KEY")

MODULES_DIR = os.path.join(os.path.dirname(__file__))

def _write_module_file(module_name, code):
    safe_name = module_name.lower().replace(" ", "_")
    path = os.path.join(MODULES_DIR, f"{safe_name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    return path

def _generate_module_code(module_name, instruction):
    prompt = f"""
You are an expert Python developer.
Generate a Telegram-compatible Jarvis module for '{module_name}'.

Each module must:
- define DESCRIPTION (string)
- define register(dp, services, scheduler)
- use clean, production-safe Python
- never include secrets or hardcoded tokens
- respond with meaningful messages

The purpose of this module is: {instruction}
    """
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.5,
    )
    return resp.choices[0].message.content


def _reload_module(module_name, update):
    """Reload or import a module dynamically."""
    safe_name = module_name.lower().replace(" ", "_")
    try:
        if safe_name in sys.modules:
            importlib.reload(sys.modules[safe_name])
        else:
            sys.path.insert(0, MODULES_DIR)
            importlib.import_module(safe_name)
        update.message.reply_text(f"♻️ Module '{module_name}' reloaded successfully.")
    except Exception as e:
        update.message.reply_text(f"⚠️ Reload failed for {module_name}: {e}")
        logger.exception(e)


def create_module_from_voice(update, module_name):
    update.message.reply_text(f"🧠 Generating new module: {module_name}")
    try:
        code = _generate_module_code(module_name, f"Create a new Jarvis feature named {module_name}.")
        path = _write_module_file(module_name, code)
        success, msg = git_commit_and_push(path, f"Add new module: {module_name}")
        update.message.reply_text(f"✅ Module '{module_name}' created and synced.\n{msg}")
        _reload_module(module_name, update)
    except Exception as e:
        logger.exception(e)
        update.message.reply_text(f"❌ Failed to create module: {e}")


def update_module_from_voice(update, module_name):
    safe_name = module_name.lower().replace(" ", "_")
    path = os.path.join(MODULES_DIR, f"{safe_name}.py")
    if not os.path.exists(path):
        update.message.reply_text(f"⚠️ Module '{module_name}' not found.")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            existing_code = f.read()

        prompt = f"""
Improve and refactor this Jarvis module code while keeping its purpose same.
File name: {module_name}
Existing code:
{existing_code}
        """

        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
        )
        new_code = resp.choices[0].message.content
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_code)

        success, msg = git_commit_and_push(path, f"Update module: {module_name}")
        update.message.reply_text(f"✅ Module '{module_name}' updated and synced.\n{msg}")
        _reload_module(module_name, update)
    except Exception as e:
        logger.exception(e)
        update.message.reply_text(f"❌ Failed to update module: {e}")
