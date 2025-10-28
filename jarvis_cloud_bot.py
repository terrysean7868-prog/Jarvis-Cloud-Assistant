# jarvis_cloud_bot.py
# Simple cloud Jarvis (Telegram) - basic commands, reminders, notes
# Requirements: python-telegram-bot==13.15, APScheduler, sqlite3, requests

import logging
import sqlite3
import time
from datetime import datetime, timedelta

from telegram import Update, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext
from apscheduler.schedulers.background import BackgroundScheduler
import requests
import os

# ---------- CONFIG ----------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
DB_PATH = "jarvis_data.db"
# ----------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --------- DB helpers ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    message TEXT,
                    remind_at TEXT,
                    sent INTEGER DEFAULT 0
                  )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    note TEXT,
                    created_at TEXT
                  )""")
    conn.commit()
    conn.close()

def add_reminder(chat_id, message, remind_at_iso):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO reminders (chat_id,message,remind_at) VALUES (?,?,?)",
                (chat_id, message, remind_at_iso))
    conn.commit()
    conn.close()

def get_due_reminders():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_iso = datetime.utcnow().isoformat()
    cur.execute("SELECT id, chat_id, message FROM reminders WHERE sent=0 AND remind_at<=?", (now_iso,))
    rows = cur.fetchall()
    conn.close()
    return rows

def mark_sent(reminder_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE reminders SET sent=1 WHERE id=?", (reminder_id,))
    conn.commit()
    conn.close()

def save_note(chat_id, text):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO notes (chat_id,note,created_at) VALUES (?,?,?)",
                (chat_id, text, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

# --------- Reminder scheduler ----------
scheduler = BackgroundScheduler()

def reminder_job(bot_instance=None):
    # check DB for due reminders
    rows = get_due_reminders()
    for rid, chat_id, message in rows:
        try:
            if bot_instance:
                bot_instance.send_message(chat_id=chat_id, text=f"🔔 Reminder: {message}")
            mark_sent(rid)
        except Exception as e:
            logger.error("Failed sending reminder %s -> %s", rid, e)

# ---------- Command handlers ----------
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Hello Sir — Jarvis Cloud at your service.\n\n"
        "Use /help to see basic commands."
    )

def help_cmd(update: Update, context: CallbackContext):
    update.message.reply_text(
        "Basic commands available:\n"
        "/time - Get current time (UTC)\n"
        "/search <query> - Get Google search link\n"
        "/note <text> - Save a quick note\n"
        "/remind <in minutes> | <message> - Set reminder (example: /remind 10 | Stretch)\n"
        "/mynotes - List your notes (last 10)\n"
        "/help - This message"
    )

def time_cmd(update: Update, context: CallbackContext):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    update.message.reply_text(f"🕒 Current time: {now}")

def search_cmd(update: Update, context: CallbackContext):
    text = update.message.text.partition(' ')[2].strip()
    if not text:
        update.message.reply_text("Usage: /search <query>")
        return
    q = requests.utils.requote_uri(text)
    url = f"https://www.google.com/search?q={q}"
    update.message.reply_text(f"🔎 Search link: {url}")

def note_cmd(update: Update, context: CallbackContext):
    text = update.message.text.partition(' ')[2].strip()
    if not text:
        update.message.reply_text("Usage: /note <your note text>")
        return
    save_note(update.message.chat_id, text)
    update.message.reply_text("✅ Note saved.")

def mynotes_cmd(update: Update, context: CallbackContext):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT note, created_at FROM notes WHERE chat_id=? ORDER BY id DESC LIMIT 10", (update.message.chat_id,))
    rows = cur.fetchall()
    conn.close()
    if not rows:
        update.message.reply_text("No notes found.")
        return
    msg = "📝 Your recent notes:\n\n"
    for note, created in rows:
        created_local = created.replace("T", " ").split(".")[0]
        msg += f"- {note} (added {created_local} UTC)\n"
    update.message.reply_text(msg)

def remind_cmd(update: Update, context: CallbackContext):
    # format: /remind 10 | Stretch legs
    parts = update.message.text.partition(' ')[2].split('|', 1)
    if not parts or len(parts) < 1 or not parts[0].strip():
        update.message.reply_text("Usage: /remind <minutes_from_now> | <message>")
        return
    try:
        minutes = int(parts[0].strip())
    except:
        update.message.reply_text("First part must be integer minutes, e.g. /remind 10 | Stretch")
        return
    message = parts[1].strip() if len(parts) > 1 else "Reminder"
    remind_at = datetime.utcnow() + timedelta(minutes=minutes)
    add_reminder(update.message.chat_id, message, remind_at.isoformat())
    update.message.reply_text(f"✅ Reminder set for {minutes} minutes from now.")

def unknown_text(update: Update, context: CallbackContext):
    txt = update.message.text
    # small natural fallback
    update.message.reply_text("Sorry Sir, I didn't understand. Use /help to see commands.")

# ---------- Bot start ----------
def main():
    init_db()
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("time", time_cmd))
    dp.add_handler(CommandHandler("search", search_cmd))
    dp.add_handler(CommandHandler("note", note_cmd))
    dp.add_handler(CommandHandler("mynotes", mynotes_cmd))
    dp.add_handler(CommandHandler("remind", remind_cmd))
    dp.add_handler(MessageHandler(Filters.text & (~Filters.command), unknown_text))

    # start scheduler job to run every 30 seconds and send reminders
    scheduler.add_job(func=lambda: reminder_job(bot_instance=updater.bot), trigger="interval", seconds=30)
    scheduler.start()

    updater.start_polling()
    logger.info("Jarvis Cloud Bot started.")
    updater.idle()

if __name__ == "__main__":
    main()
