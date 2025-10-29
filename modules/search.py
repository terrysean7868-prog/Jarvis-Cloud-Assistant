DESCRIPTION = 'Provide Google search link for a query'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    import requests
    def search(update, context):
        q = ' '.join(context.args)
        if not q:
            update.message.reply_text('Usage: /search <query)')
            return
        url = 'https://www.google.com/search?q={}'.format(requests.utils.requote_uri(q))
        update.message.reply_text('🔎 {}'.format(url))
    dp.add_handler(CommandHandler('search', search))
