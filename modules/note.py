# module/note.py
DESCRIPTION = 'Save quick notes to MongoDB or sqlite'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    import sqlite3, os
    from datetime import datetime
    DB = 'jarvis_data.db'
    def note(update, context):
        text = ' '.join(context.args)
        if not text:
            update.message.reply_text('Usage: /note <text>')
            return
        uri = services.get('mongodb_uri')
        if uri:
            try:
                from pymongo import MongoClient
                client = MongoClient(uri, serverSelectionTimeoutMS=2000)
                db = client.jarvis
                db.notes.insert_one({'chat_id': update.message.chat_id, 'note': text, 'created_at': datetime.utcnow().isoformat()})
                update.message.reply_text('Note saved to cloud memory.')
                return
            except Exception:
                pass
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, note TEXT, created_at TEXT)')
        cur.execute('INSERT INTO notes (chat_id,note,created_at) VALUES (?,?,datetime("now"))', (update.message.chat_id, text))
        conn.commit()
        conn.close()
        update.message.reply_text('Note saved locally.')
    dp.add_handler(CommandHandler('note', note))
