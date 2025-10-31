# modules/auto_update.py
DESCRIPTION = 'AI-powered self-updater: create or update modules via Telegram or voice.'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    import os, openai
    from utils.auto_sync import git_commit_and_push

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    if not OPENAI_API_KEY:
        print("⚠️ OPENAI_API_KEY not set — AI module generation disabled.")
        return

    openai.api_key = OPENAI_API_KEY

    def generate_module_code(prompt):
        """Generate module code using OpenAI."""
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a Jarvis AI assistant that writes clean Telegram bot modules."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        return response.choices[0].message["content"]

    def write_module_file(name, code):
        """Write AI-generated module to /modules."""
        safe_name = name.lower().replace(' ', '_')
        path = f"modules/{safe_name}.py"
        with open(path, "w", encoding="utf-8") as f:
            f.write(code)
        return path

    # --- Manual /addmodule command ---
    def add_module(update, context):
        if not context.args:
            update.message.reply_text("Usage: /addmodule <name> <short description>")
            return
        name = context.args[0]
        desc = " ".join(context.args[1:]) or "AI-generated Jarvis module."
        update.message.reply_text(f"🧠 Generating new module: {name}...")

        prompt = f"Create a Jarvis Telegram bot module named '{name}'. Description: {desc}. Include DESCRIPTION and register(dp, services, scheduler)."
        code = generate_module_code(prompt)
        path = write_module_file(name, code)
        update.message.reply_text(f"✅ Module {name} created successfully.")

        success, msg = git_commit_and_push(path, f"Added module {name}")
        update.message.reply_text(f"🔄 {msg}" if success else f"❌ {msg}")

    # --- Manual /updatemodule command ---
    def update_module(update, context):
        if not context.args:
            update.message.reply_text("Usage: /updatemodule <name> <update description>")
            return
        name = context.args[0]
        desc = " ".join(context.args[1:]) or "Improve existing module."
        fname = f"modules/{name.lower().replace(' ', '_')}.py"

        if not os.path.exists(fname):
            update.message.reply_text(f"⚠️ Module '{name}' not found.")
            return

        with open(fname, "r", encoding="utf-8") as f:
            old_code = f.read()

        prompt = f"Improve this Jarvis Telegram module named '{name}' based on: {desc}. Keep the same register(dp, services, scheduler) function.\n\n{old_code}"
        code = generate_module_code(prompt)

        with open(fname, "w", encoding="utf-8") as f:
            f.write(code)

        update.message.reply_text(f"✅ Module {name} updated.")
        success, msg = git_commit_and_push(fname, f"Updated module {name}")
        update.message.reply_text(f"🔄 {msg}" if success else f"❌ {msg}")

    dp.add_handler(CommandHandler("addmodule", add_module))
    dp.add_handler(CommandHandler("updatemodule", update_module))

# --- Voice helpers for voice.py ---
def create_module_from_voice(update, module_name):
    import os, openai
    from utils.auto_sync import git_commit_and_push

    openai.api_key = os.getenv("OPENAI_API_KEY")
    prompt = f"Create a Jarvis Telegram bot module named '{module_name}'. Include DESCRIPTION and register(dp, services, scheduler)."
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    code = response.choices[0].message["content"]
    safe_name = module_name.lower().replace(' ', '_')
    path = f"modules/{safe_name}.py"
    with open(path, "w", encoding="utf-8") as f:
        f.write(code)
    update.message.reply_text(f"✅ Module {module_name} created successfully.")
    success, msg = git_commit_and_push(path, f"Added module {module_name} (via voice)")
    update.message.reply_text(f"🔄 {msg}" if success else f"❌ {msg}")

def update_module_from_voice(update, module_name):
    import os, openai
    from utils.auto_sync import git_commit_and_push

    safe_name = module_name.lower().replace(' ', '_')
    path = f"modules/{safe_name}.py"

    if not os.path.exists(path):
        update.message.reply_text(f"⚠️ Module {module_name} not found.")
        return

    with open(path, "r", encoding="utf-8") as f:
        old_code = f.read()

    openai.api_key = os.getenv("OPENAI_API_KEY")
    prompt = f"Improve this Jarvis Telegram bot module named '{module_name}'. Keep the same function structure.\n\n{old_code}"
    response = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    code = response.choices[0].message["content"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(code)

    update.message.reply_text(f"✅ Module {module_name} updated successfully.")
    success, msg = git_commit_and_push(path, f"Updated module {module_name} (via voice)")
    update.message.reply_text(f"🔄 {msg}" if success else f"❌ {msg}")
