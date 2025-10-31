import os
import re
import logging
import importlib
import sys
from openai import OpenAI

# Get OpenAI API key from secret environment variable
OPENAI_KEY = os.getenv("OPENAI_API_KEY") or os.getenv("OPENAI_SECRET_KEY")

client = OpenAI(api_key=OPENAI_KEY)
logger = logging.getLogger("jarvis.auto_update")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES_DIR = BASE_DIR


def extract_python_code(ai_text: str) -> str:
    """
    Extracts clean Python code from OpenAI response.
    Keeps only the contents inside ```python ... ``` or ``` ... ```.
    """
    if not ai_text:
        return ""

    # Try to extract fenced code block
    code_blocks = re.findall(r"```(?:python)?\s*([\s\S]*?)```", ai_text)
    if code_blocks:
        return code_blocks[0].strip()

    # Fallback: assume entire text is code
    return ai_text.strip()


def write_module_file(name: str, code: str) -> str:
    """Writes the module file to disk and returns path."""
    filename = re.sub(r"[^a-zA-Z0-9_]", "_", name.lower()) + ".py"
    filepath = os.path.join(MODULES_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(code)
    return filepath


def reload_module(name: str):
    """Hot-reload module after creation or update."""
    modname = f"modules.{name}"
    if modname in sys.modules:
        importlib.reload(sys.modules[modname])
    else:
        importlib.import_module(modname)


def update_help_list(module_name: str, description: str):
    """Add the module’s description to help file dynamically."""
    help_path = os.path.join(BASE_DIR, "help_index.txt")
    entry = f"{module_name}: {description}\n"
    existing = ""
    if os.path.exists(help_path):
        with open(help_path, "r", encoding="utf-8") as f:
            existing = f.read()

    if module_name not in existing:
        with open(help_path, "a", encoding="utf-8") as f:
            f.write(entry)


def generate_module_with_ai(prompt: str) -> str:
    """Call OpenAI to generate module code based on a user prompt."""
    full_prompt = f"""
You are an assistant for building Telegram bot modules.
Generate a valid Python file for a module compatible with python-telegram-bot v13.
Follow these strict rules:
1. Must define a variable `DESCRIPTION = "..."`.
2. Must define a function `register(dp, services, scheduler)` that adds any CommandHandler(s) or MessageHandler(s).
3. Do not include explanations, markdown, or any text outside code.
4. The code must be inside one clean Python file — no comments outside code fences.
User request: {prompt}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a Python code generator for Telegram modules."},
                {"role": "user", "content": full_prompt},
            ],
            temperature=0.4,
        )
        ai_text = response.choices[0].message.content
        return extract_python_code(ai_text)
    except Exception as e:
        logger.error(f"OpenAI generation error: {e}")
        raise


def create_module_from_voice(update, module_name: str):
    """Creates a new module via AI, saves it, reloads, and adds to /help."""
    try:
        update.message.reply_text(f"🧠 Generating new module: {module_name}")
        code = generate_module_with_ai(f"Create a new Telegram bot module named '{module_name}'")

        if not code.strip():
            update.message.reply_text("❌ AI returned no code.")
            return

        filepath = write_module_file(module_name, code)
        reload_module(module_name)
        update.message.reply_text(f"✅ New module '{module_name}' created and reloaded.")

        # Try to extract DESCRIPTION for /help
        desc_match = re.search(r'DESCRIPTION\s*=\s*["\']([^"\']+)["\']', code)
        if desc_match:
            update_help_list(module_name, desc_match.group(1))

        # Auto-sync to GitHub
        try:
            from utils.auto_sync import git_commit_and_push
            git_commit_and_push(filepath, f"Added new module: {module_name}")
            update.message.reply_text("🔁 AutoSync complete.")
        except Exception as e:
            update.message.reply_text(f"⚠️ AutoSync failed: {e}")

    except Exception as e:
        update.message.reply_text(f"❌ Failed to create module: {e}")


def update_module_from_voice(update, module_name: str):
    """Updates an existing module with improvements via AI."""
    try:
        filename = re.sub(r"[^a-zA-Z0-9_]", "_", module_name.lower()) + ".py"
        filepath = os.path.join(MODULES_DIR, filename)
        if not os.path.exists(filepath):
            update.message.reply_text(f"⚠️ Module '{module_name}' not found.")
            return

        with open(filepath, "r", encoding="utf-8") as f:
            old_code = f.read()

        update.message.reply_text(f"🧠 Updating module: {module_name}")
        prompt = f"Improve and modernize this module code while keeping same functionality:\n\n{old_code}"
        new_code = generate_module_with_ai(prompt)

        if not new_code.strip():
            update.message.reply_text("❌ AI returned no updated code.")
            return

        write_module_file(module_name, new_code)
        reload_module(module_name)
        update.message.reply_text(f"✅ Module '{module_name}' updated and reloaded.")

        try:
            from utils.auto_sync import git_commit_and_push
            git_commit_and_push(filepath, f"Updated module: {module_name}")
            update.message.reply_text("🔁 AutoSync complete.")
        except Exception as e:
            update.message.reply_text(f"⚠️ AutoSync failed: {e}")

    except Exception as e:
        update.message.reply_text(f"❌ Failed to update module: {e}")
