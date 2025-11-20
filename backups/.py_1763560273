import logging
from telegram import Update
from telegram.ext import Updater, CommandHandler, CallbackContext

# Configure logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Function to register a user
def register(update: Update, context: CallbackContext) -> None:
    user = update.effective_user
    if user:
        user_id = user.id
        username = user.username if user.username else "Unknown"
        logger.info(f"User registered: {username} (ID: {user_id})")
        update.message.reply_text(f"Welcome, {username}! You have been registered.")
    else:
        logger.warning("No user information available.")
        update.message.reply_text("Registration failed. Please try again.")

# Main function to start the bot
def main() -> None:
    TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
    updater = Updater(TOKEN)

    dispatcher = updater.dispatcher
    dispatcher.add_handler(CommandHandler("register", register))

    # Start the Bot
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.error(f"An error occurred: {e}")