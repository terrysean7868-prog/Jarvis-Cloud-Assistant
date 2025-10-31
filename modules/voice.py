# modules/voice.py
DESCRIPTION = 'Convert Telegram voice messages to text and execute AI module commands'

def register(dp, services, scheduler):
    from telegram.ext import MessageHandler, Filters
    import os, re, speech_recognition as sr
    from pydub import AudioSegment
    from modules import auto_update

    def handle_voice(update, context):
        user = update.message.from_user.first_name or 'Sir'
        recognizer = sr.Recognizer()
        ogg_path, wav_path = "voice.ogg", "voice.wav"

        try:
            file = update.message.voice.get_file()
            file.download(ogg_path)
            AudioSegment.from_file(ogg_path).export(wav_path, format="wav")
            with sr.AudioFile(wav_path) as src:
                audio = recognizer.record(src)
                text = recognizer.recognize_google(audio, language="en-IN")

            update.message.reply_text(f"{user}, you said: {text}")
            lower = text.lower().strip()

            if re.search(r"(create|add).*module", lower):
                mod = re.sub(r".*module", "", lower).strip()
                auto_update.create_module_from_voice(update, mod)
                return
            elif re.search(r"(update|modify|improve).*module", lower):
                mod = re.sub(r".*module", "", lower).strip()
                auto_update.update_module_from_voice(update, mod)
                return
            else:
                update.message.reply_text("⚙️ Could not interpret your voice command.")

        except sr.UnknownValueError:
            update.message.reply_text("Sorry, I couldn't understand your voice clearly.")
        except Exception as e:
            update.message.reply_text(f"Voice error: {e}")
        finally:
            for f in (ogg_path, wav_path):
                if os.path.exists(f):
                    os.remove(f)

    dp.add_handler(MessageHandler(Filters.voice, handle_voice))
