# modules/voice.py
DESCRIPTION = 'Convert Telegram voice messages to text and execute AI commands (module generation, updates, etc.)'

def register(dp, services, scheduler):
    from telegram.ext import MessageHandler, Filters
    import os, re, speech_recognition as sr
    from pydub import AudioSegment
    from modules import auto_update

    def handle_voice(update, context):
        user = update.message.from_user.first_name or 'Sir'
        recognizer = sr.Recognizer()
        ogg_path = 'voice.ogg'
        wav_path = 'voice.wav'

        try:
            file = update.message.voice.get_file()
            file.download(ogg_path)
            AudioSegment.from_file(ogg_path).export(wav_path, format='wav')

            with sr.AudioFile(wav_path) as src:
                audio = recognizer.record(src)
                text = recognizer.recognize_google(audio, language='en-IN')

            update.message.reply_text(f"{user}, you said: {text}")

            # --- Interpret commands ---
            lower = text.lower().strip()

            # Pattern 1: create module
            m = re.search(r'(?:create|add)(?: a)? module ([a-zA-Z0-9_ -]+)', lower)
            if m:
                mod_name = m.group(1).strip()
                update.message.reply_text(f"🧠 AI detected new module request: {mod_name}")
                auto_update.create_module_from_voice(update, mod_name)
                return

            # Pattern 2: update module
            m = re.search(r'(?:update|modify|improve)(?: a)? module ([a-zA-Z0-9_ -]+)', lower)
            if m:
                mod_name = m.group(1).strip()
                update.message.reply_text(f"🧠 AI detected update request for module: {mod_name}")
                auto_update.update_module_from_voice(update, mod_name)
                return

            update.message.reply_text("⚙️ Unable to interpret voice command clearly.")

        except sr.UnknownValueError:
            update.message.reply_text("Sorry, I couldn’t understand your voice clearly.")
        except Exception as e:
            update.message.reply_text(f"Voice processing error: {e}")
        finally:
            for f in (ogg_path, wav_path):
                try:
                    if os.path.exists(f):
                        os.remove(f)
                except:
                    pass

    dp.add_handler(MessageHandler(Filters.voice, handle_voice))
