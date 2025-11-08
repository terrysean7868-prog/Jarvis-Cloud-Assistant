# JARVIS - AI Assistant

**Just A Rather Very Intelligent System** - A modern voice-activated AI assistant with Iron Man inspired UI.

## Features

- 🎤 **Voice Activated** - Say "Hey Jarvis" to activate
- 🤖 **AI Powered** - Uses OpenAI and Gemini APIs
- 🎨 **Modern UI** - Iron Man inspired centered interface
- 🔄 **Auto Updates** - Self-modifying and auto-syncing with GitHub
- 📢 **Voice Responses** - Speaks responses through speakers

## Quick Start (Windows)

### Option 1: Double-Click (Easiest)
Just double-click `JARVIS.bat` and follow the prompts.

### Option 2: Manual Setup

1. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   cd jarvis-frontend
   npm install
   cd ..
   ```

2. **Create `.env` file:**
   ```
   OPENAI_API_KEY=your_openai_api_key_here
   AUTO_APPLY=true
   ```

3. **Run JARVIS:**
   ```bash
   python run_jarvis.py
   ```

4. **Open Browser:**
   Navigate to `http://localhost:3000`

## Requirements

- Python 3.8+
- Node.js 16+
- OpenAI API Key (or Gemini API Key)
- Windows 10/11
- Chrome or Edge browser (for voice recognition)

## Configuration

Edit `.env` file:

```env
OPENAI_API_KEY=your_key_here
GEMINI_API_KEY=your_key_here  # Optional
LLM_PROVIDER=auto  # auto, openai, or gemini
AUTO_APPLY=true
GITHUB_REPO=https://github.com/yourusername/repo.git  # Optional
GITHUB_TOKEN=your_token  # Optional
```

## Usage

1. Open `http://localhost:3000` in Chrome or Edge
2. Allow microphone permissions
3. Say **"Hey Jarvis"** followed by your command
4. JARVIS will respond with voice and text

## Examples

- "Hey Jarvis, what's the weather?"
- "Hey Jarvis, create a hello world file"
- "Hey Jarvis, update my code"
- "Hey Jarvis, what's 2 plus 2?"

## Project Structure

```
Jarvis-Cloud-Assistant/
├── JARVIS.bat          # Main startup script (Windows)
├── run_jarvis.py       # Python startup script
├── app.py              # FastAPI backend
├── jarvis_brain.py     # AI brain logic
├── llm_adapter.py      # LLM integration
├── executor.py         # Action executor
├── git_sync.py         # GitHub sync
├── requirements.txt    # Python dependencies
└── jarvis-frontend/    # React frontend
    ├── src/
    │   ├── App.jsx     # Main React component
    │   └── App.css     # Iron Man UI styles
    └── package.json    # Node dependencies
```

## Troubleshooting

### Backend won't start
- Check if port 8000 is available
- Verify `.env` file exists with `OPENAI_API_KEY`
- Install dependencies: `pip install -r requirements.txt`

### Frontend can't connect
- Make sure backend is running
- Check browser console (F12) for errors
- Verify proxy in `package.json` is set to `http://localhost:8000`

### Voice not working
- Use Chrome or Edge browser
- Allow microphone permissions
- Check browser console for errors

## License

MIT License - Feel free to use and modify!

## Credits

Inspired by Tony Stark's JARVIS from Iron Man movies.
