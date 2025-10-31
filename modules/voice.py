DESCRIPTION = 'Convert Telegram voice messages to text using Google Speech Recognition and AI intent detection (create/update modules, auto-sync)'

def register(dp, services, scheduler):
    from telegram.ext import MessageHandler, CommandHandler, Filters
    import os, speech_recognition as sr
    from pydub import AudioSegment
    import requests
    import logging

    from utils.auto_sync import git_commit_and_push  # Reuse your existing auto_sync logic

    logger = logging.getLogger('jarvis.voice')
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

    # --------------------------------------------------
    # 🔁 Command: /autosync — manually commit and push
    # --------------------------------------------------
    def auto_sync_cmd(update, context):
        update.message.reply_text("🔄 Syncing latest code changes to GitHub...")
        success, message = git_commit_and_push(
            file_path=".", commit_message="Jarvis auto-sync triggered via /autosync"
        )
        update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")

    dp.add_handler(CommandHandler("autosync", auto_sync_cmd))

    # --------------------------------------------------
    # 🧠 Helper: Trigger module creation/update
    # --------------------------------------------------
    def trigger_github_event(update, event_type, module_name):
        token = services.get("github_token")
        repo = services.get("github_repo")
        if token and repo:
            url = f"https://api.github.com/repos/{repo}/dispatches"
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.everest-preview+json",
            }
            payload = {
                "event_type": event_type,
                "client_payload": {"module": module_name},
            }
            r = requests.post(url, headers=headers, json=payload)
            if r.status_code in (200, 204):
                update.message.reply_text(
                    f"🧠 AI detected: {event_type.replace('-', ' ')} request for module `{module_name}`\n✅ GitHub automation triggered."
                )
            else:
                update.message.reply_text(
                    f"❌ Failed to trigger GitHub automation (HTTP {r.status_code})"
                )
        else:
            update.message.reply_text("⚠️ GitHub repo or token not configured.")

    # --------------------------------------------------
    # 🎙️ Handle voice input
    # --------------------------------------------------
    def handle_voice(update, context):
        user = update.message.from_user.first_name or "Sir"
        recognizer = sr.Recognizer()
        ogg_path, wav_path = "voice.ogg", "voice.wav"
        try:
            # Step 1 — download voice
            file = update.message.voice.get_file()
            file.download(ogg_path)
            AudioSegment.from_file(ogg_path).export(wav_path, format="wav")

            # Step 2 — transcribe voice
            with sr.AudioFile(wav_path) as src:
                audio = recognizer.record(src)
                text = recognizer.recognize_google(audio, language="en-IN")

            update.message.reply_text(f"{user}, you said: {text}")

            # Step 3 — use OpenAI to interpret the intent
            if OPENAI_API_KEY:
                import openai
                openai.api_key = OPENAI_API_KEY

                prompt = (
                    f"The user said: '{text}'.\n"
                    "Determine if this means:\n"
                    "1. 'create a new module <name>' → respond exactly 'create <name>'\n"
                    "2. 'update module <name>' → respond exactly 'update <name>'\n"
                    "If neither, respond 'none'. Do not add explanations."
                )

                resp = openai.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                )
                ai_resp = resp.choices[0].message["content"].strip().lower()
                logger.info(f"AI interpreted voice: {ai_resp}")

                # Step 4 — act on AI command
                if ai_resp.startswith("create "):
                    module_name = ai_resp.replace("create ", "").strip()
                    trigger_github_event(update, "generate-module", module_name)

                elif ai_resp.startswith("update "):
                    module_name = ai_resp.replace("update ", "").strip()
                    trigger_github_event(update, "update-module", module_name)

                elif ai_resp == "none":
                    update.message.reply_text("👍 Okay, no module operation requested.")
                else:
                    update.message.reply_text(f"🤔 AI response: {ai_resp}")

            else:
                update.message.reply_text(
                    "⚠️ OPENAI_API_KEY not set — skipping AI interpretation."
                )

        except sr.UnknownValueError:
            update.message.reply_text("Sorry, I couldn't understand your voice clearly.")
        except Exception as e:
            update.message.reply_text(f"Voice processing error: {e}")
        finally:
            for f in (ogg_path, wav_path):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except:
                    pass

    # Register the voice handler
    dp.add_handler(MessageHandler(Filters.voice, handle_voice))
