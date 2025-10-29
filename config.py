import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
MONGODB_URI = os.environ.get('MONGODB_URI')
OPENWEATHER_KEY = os.environ.get('OPENWEATHER_KEY')
GITHUB_REPO = os.environ.get('GITHUB_REPO')
GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
DEFAULT_LANG = os.environ.get('DEFAULT_LANG', 'en')
