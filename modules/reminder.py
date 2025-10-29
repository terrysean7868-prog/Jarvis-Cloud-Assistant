DESCRIPTION = 'Set simple minute-based reminders'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    from datetime import datetime, timedelta
    import sqlite3
    DB = 'jarvis_data.db'
    def remind(update, context):
        raw = ' '.join(context.args)
        if '|' in raw:
            m, msg = raw.split('|',1)
        else:
            parts = raw.split()
            if not parts:
                update.message.reply_text('Usage: /remind <minutes> | <message>')
                return
            m = parts[0]; msg = ' '.join(parts[1:]) or 'Reminder'
        try:
            minutes = int(m.strip())
        except:
            update.message.reply_text('First token must be minutes integer.')
            return
        remind_at = datetime.utcnow() + timedelta(minutes=minutes)
        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute('CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, message TEXT, remind_at TEXT, sent INTEGER DEFAULT 0)')
        cur.execute('INSERT INTO reminders (chat_id,message,remind_at) VALUES (?,?,?)', (update.message.chat_id, msg.strip(), remind_at.isoformat()))
        conn.commit()
        conn.close()
        update.message.reply_text(f'Reminder set for {minutes} minutes from now.')
    dp.add_handler(CommandHandler('remind', remind))
