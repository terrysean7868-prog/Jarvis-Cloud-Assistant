DESCRIPTION = 'Get weather using OpenWeatherMap API (supports English, Hindi, Gujarati prompts)'

def register(dp, services, scheduler):
    from telegram.ext import CommandHandler
    import requests
    def weather(update, context):
        city = ' '.join(context.args)
        if not city:
            update.message.reply_text('Usage: /weather <city>')
            return
        key = services.get('openweather')
        if not key:
            update.message.reply_text('Weather API not configured.')
            return
        q = requests.utils.requote_uri(city)
        r = requests.get('http://api.openweathermap.org/data/2.5/weather?q={}&appid={}&units=metric'.format(q, key))
        if r.status_code != 200:
            update.message.reply_text('City not found or API error.')
            return
        j = r.json()
        desc = j['weather'][0]['description']
        temp = j['main']['temp']
        update.message.reply_text('Weather in {}: {}, {}°C'.format(city, desc, temp))
    dp.add_handler(CommandHandler('weather', weather))
