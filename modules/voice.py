DESCRIPTION = 'Convert Telegram voice messages to text using Google Speech Recognition'

def register(dp, services, scheduler):
    from telegram.ext import MessageHandler, Filters
    import os, speech_recognition as sr
    from pydub import AudioSegment

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

        except sr.UnknownValueError:
            update.message.reply_text('Sorry, I couldn\'t understand your voice clearly.')
        except Exception as e:
            update.message.reply_text(f'Voice processing error: {e}')
        finally:
            for f in (ogg_path, wav_path):
                try:
                    if os.path.exists(f): os.remove(f)
                except:
                    pass

    dp.add_handler(MessageHandler(Filters.voice, handle_voice))
