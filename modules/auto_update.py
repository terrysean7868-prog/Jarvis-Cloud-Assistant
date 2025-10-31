# modules/auto_update.py
DESCRIPTION = 'AI-powered self-updater: create or update modules via Telegram or voice.'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    import os, openai, subprocess, textwrap
    from utils.auto_sync import git_commit_and_push

    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    openai.api_key = OPENAI_API_KEY

    def generate_module_code(prompt):
        """Use OpenAI to generate Python module code."""
        resp = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a Jarvis AI code assistant. Generate working Telegram module code."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        return resp.choices[0].message["content"]

    def write_module_file(name, code):
        """Write module file under /modules."""
        fname = f"modules/{name.lower().replace(' ', '_')}.py"
        with open(fname, "w", encoding="utf-8") as f:
            f.write(code)
        return fname

    def create_module(update, context):
        if not context.args:
            update.message.reply_text("Usage: /addmodule <module name> <short description>")
            return
        name = context.args[0]
        description = " ".join(context.args[1:]) or "Custom AI-generated module."
        update.message.reply_text(f"🧠 Generating new module: {name}...")

        prompt = f"Create a Jarvis module named '{name}' for a Telegram bot. Description: {description}. Include a DESCRIPTION and register(dp, services, scheduler) function."
        code = generate_module_code(prompt)
        path = write_module_file(name, code)
        update.message.reply_text(f"✅ Module {name} created at {path}")

        # Auto-sync with GitHub
        update.message.reply_text("🔄 Auto-syncing with GitHub...")
        success, msg = git_commit_and_push(file_path=path, commit_message=f"Added module {name}")
        update.message.reply_text(f"✅ {msg}" if success else f"❌ {msg}")

    def update_module(update, context):
        if not context.args:
            update.message.reply_text("Usage: /updatemodule <module name> <description of change>")
            return
        name = context.args[0]
        desc = " ".join(context.args[1:]) or "Improve existing module."

        fname = f"modules/{name.lower()}.py"
        if not os.path.exists(fname):
            update.message.reply_text(f"⚠️ Module {name} not found.")
            return

        update.message.reply_text(f"🧠 Updating {name} with: {desc}")
        with open(fname, "r", encoding="utf-8") as f:
            old_code = f.read()

        prompt = f"Improve this Jarvis Telegram module based on: {desc}. Keep register(dp, services, scheduler) intact.\n\n{old_code}"
        code = generate_module_code(prompt)
        with open(fname, "w", encoding="utf-8") as f:
            f.write(code)
        update.message.reply_text(f"✅ Module {name} updated.")

        # Auto-sync with GitHub
        success, msg = git_commit_and_push(file_path=fname, commit_message=f"Updated module {name}: {desc}")
        update.message.reply_text(f"✅ {msg}" if success else f"❌ {msg}")

    dp.add_handler(CommandHandler("addmodule", create_module))
    dp.add_handler(CommandHandler("updatemodule", update_module))
