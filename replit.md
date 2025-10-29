# Jarvis Cloud Assistant

## Overview
Jarvis Cloud Assistant is a Telegram bot that provides various AI-powered assistant functionalities through a modular plugin system. Users interact with the bot through Telegram messages to access different features.

## Project Architecture
- **Language**: Python 3.11
- **Framework**: python-telegram-bot 13.15
- **Type**: Backend service (Telegram bot)
- **Entry Point**: `jarvis_service.py`

### Structure
```
.
├── jarvis_service.py     # Main bot service
├── config.py            # Environment configuration
├── modules/             # Plugin modules
│   ├── note.py         # Save notes (MongoDB or SQLite)
│   ├── reminder.py     # Set reminders
│   ├── search.py       # Web search
│   ├── voice.py        # Voice-to-text conversion
│   └── weather.py      # Weather information
├── utils/
│   ├── db.py           # MongoDB utilities
│   └── scheduler.py    # Background job scheduler
└── requirements.txt    # Python dependencies
```

## Required Environment Variables
- **TELEGRAM_TOKEN** (Required): Telegram Bot API token from @BotFather
- **MONGODB_URI** (Optional): MongoDB connection string for cloud storage
- **OPENWEATHER_KEY** (Optional): OpenWeatherMap API key for weather module
- **GITHUB_TOKEN** (Optional): GitHub personal access token for auto-updates
- **GITHUB_REPO** (Optional): GitHub repository (format: username/repo)
- **DEFAULT_LANG** (Optional): Default language code (default: 'en')

## Features
- **Notes**: Save notes to MongoDB (if configured) or local SQLite database
- **Voice Recognition**: Convert Telegram voice messages to text using Google Speech Recognition
- **Reminders**: Schedule reminder notifications
- **Search**: Web search functionality
- **Weather**: Get weather information (requires OPENWEATHER_KEY)
- **Modular Architecture**: Dynamically loads plugin modules from the modules/ directory
- **Auto-update Support**: Can trigger GitHub Actions to add new modules (requires GitHub tokens)

## Recent Changes
- 2025-10-29: Initial setup for Replit environment
  - Installed Python 3.11 and all dependencies
  - Fixed dependency conflicts (APScheduler 3.6.3 for python-telegram-bot compatibility)
  - Fixed ffmpeg-python version to 0.2.0
  - Added .gitignore for Python projects
  - Created documentation (replit.md)
  - Configured workflow to run the bot service
