AI Cloud Jarvis - Deployment README

Files included:
- jarvis_cloud_bot.py       : Main bot code (put your TELEGRAM_TOKEN as env var)
- requirements.txt         : Python dependencies
- Procfile                 : For platforms like Railway/Render (worker)
- .env.example             : Example env file
- jarvis_data.db           : SQLite DB will be created on first run (not included)

Quick local test (Linux/Windows/macOS):
1) Create a virtualenv (recommended)
   python -m venv venv
   source venv/bin/activate   # macOS/Linux
   venv\Scripts\activate    # Windows PowerShell

2) Install requirements:
   pip install -r requirements.txt

3) Set TELEGRAM_TOKEN environment variable (or edit jarvis_cloud_bot.py to paste token):
   export TELEGRAM_TOKEN='YOUR_TOKEN'    # macOS/Linux
   set TELEGRAM_TOKEN=YOUR_TOKEN         # Windows PowerShell

4) Run:
   python jarvis_cloud_bot.py

Deploy to Railway (recommended free-friendly):
1) Create a GitHub repo and push these files.
2) Sign up at https://railway.app and create a new project -> Deploy from GitHub.
3) In Railway project settings -> Variables add TELEGRAM_TOKEN with your bot token.
4) Use worker command (Procfile is provided). Railway will run the worker.

Deploy to Replit (quick test, may sleep when inactive):
1) Create new repl (Python), upload files.
2) Add secrets: TELEGRAM_TOKEN
3) Install packages via the Replit packages tool or pip.
4) Run jarvis_cloud_bot.py

Notes:
- Reminders are stored in sqlite (jarvis_data.db) in the same folder.
- Don't share TELEGRAM_TOKEN publicly.
- If you want me to push this to a GitHub repo and optionally deploy to Railway for you, tell me and I will prepare repo content and step-by-step git commands.

If you want I can also create the GitHub repo + Railway deployment instructions ready-to-run.
