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
        # Here you would typically save the user info to a database
        logger.info(f"User registered: ID={user_id}, Username={username}")
        update.message.reply_text(f"Welcome, {username}! You have been registered.")
    else:
        update.message.reply_text("Registration failed. Please try again.")

# Function to start the bot
def start(update: Update, context: CallbackContext) -> None:
    update.message.reply_text("Welcome to the bot! Use /register to register.")

def main() -> None:
    # Create the Updater and pass it your bot's token
    updater = Updater("YOUR_BOT_TOKEN")

    # Get the dispatcher to register handlers
    dispatcher = updater.dispatcher

    # Register command handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("register", register))

    # Start the Bot
    updater.start_polling()

    # Run the bot until you send a signal to stop
    updater.idle()

if __name__ == '__main__':
    main()