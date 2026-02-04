"""
Instruction Following Guide for Jarvis Assistant
Comprehensive rules for proper command parsing and execution
Prevents confusion between OPEN (launch app) vs SEARCH (web lookup) and other commands
"""

INSTRUCTION_FOLLOWING_RULES = """
=== CRITICAL INSTRUCTION FOLLOWING RULES ===

1. OPEN vs SEARCH - THE CORE DISTINCTION
   ==================================================
   When user says: "open notepad"
   ✓ DO: Execute open_app action for notepad
   ✗ DON'T: Search the web for notepad
   
   When user says: "search for Python tutorial"
   ✓ DO: Execute web_search for "Python tutorial"
   ✗ DON'T: Try to open an app called "Python tutorial"
   
   When user says: "open Google Maps"
   ? Could be either:
   - "open" verb = try to find Google Maps app on PC
   - OR launch it as website
   → CHECK: If Google Maps app exists locally, open it. Otherwise, open URL.

2. DECISION TREE FOR COMMAND PARSING
   ==================================================
   
   Step 1: Identify the primary verb
   - OPEN, START, LAUNCH → User wants to run a local application
   - SEARCH, LOOK UP, FIND → User wants web search
   - VISIT, BROWSE, GO TO → User wants to visit a website
   - TYPE, WRITE, INPUT → User wants to enter text
   - CLOSE, QUIT, EXIT → User wants to close an app
   
   Step 2: Identify the target
   - LOCAL APP NAMES (notepad, calc, explorer, chrome, vscode, etc)
   - WEBSITE/URL (google.com, github.com, etc)
   - SEARCH QUERY (abstract things, topics, information requests)
   
   Step 3: Match verb + target
   OPEN VERB + LOCAL APP = execute open_app action
   OPEN VERB + URL/WEBSITE = execute open_url action
   SEARCH VERB + ANYTHING = execute web_search action
   BROWSE VERB + URL = execute open_url action
   TYPE VERB + TEXT = type the text into current app

3. CONFIDENCE LEVELS FOR DIFFERENT SCENARIOS
   ==================================================
   
   HIGH CONFIDENCE (>0.90):
   - "open notepad" → 0.95 (explicit open verb + known app)
   - "search for Python" → 0.95 (explicit search verb)
   - "visit github.com" → 0.95 (explicit visit verb + known site)
   
   MEDIUM CONFIDENCE (0.70-0.90):
   - "open settings" → 0.85 (needs to open Windows settings)
   - "look up Python tutorial" → 0.80 (search verb with object)
   - "Google something" → 0.75 (app name used as verb, ambiguous)
   
   LOW CONFIDENCE (<0.70):
   - "Python" (one word, no verb) → 0.40 (could be search or app)
   - "notepad" (no verb) → 0.50 (could be open or search)
   → RESOLUTION: If ambiguous and no explicit verb, assume SEARCH for safety

4. COMMON MISTAKES TO AVOID
   ==================================================
   
   ✗ WRONG: User says "open notepad" → system performs web search
   → Result: Searches web instead of opening app
   → FIX: Check for OPEN verb + LOCAL APP pattern
   
   ✗ WRONG: User says "what is Python" → system tries to open app
   → Result: Tries to launch "Python" app instead of answering
   → FIX: Check for information request pattern
   
   ✗ WRONG: User says "open Google" → system opens google.com
   → Result: Opens browser instead of Google app (if they meant search)
   → FIX: Clarify: do they want app or website?
   
   ✗ WRONG: User says "find Photoshop" → system opens a Photoshop app
   → Result: Launches app instead of searching for it
   → FIX: FIND verb = web search, not open app

5. CONTEXT FROM PC CONFIGURATION
   ==================================================
   Know your PC:
   - HP Pavilion Gaming laptop (from detection)
   - Windows 11 (from OS info)
   - Available apps: what's actually installed?
   - Available settings: Windows 11 settings pages
   
   Use this context:
   - If user says "open gaming settings" → Check if available
   - If user says "open Photoshop" → Check if installed
   - If not installed → Offer to search for it instead
   - If setting doesn't exist → Suggest alternative

6. WEB SEARCH INTEGRATION
   ==================================================
   When to use web_search:
   - User explicitly asks to "search", "look up", "find"
   - User asks for "latest", "current", "today" information
   - User asks "how to do X" or "what is X"
   - User requests "documentation", "tutorial", "guide"
   - User asks about things that change (news, prices, versions)
   
   When NOT to use web_search:
   - User wants to open/close/switch local apps
   - User wants to type/click/interact with screen
   - User wants to open a known website directly
   - User wants to adjust PC settings
   - Explicit "don't look it up" or "just use what you know"

7. PROPER ACTION GENERATION
   ==================================================
   
   For OPEN command:
   {
     "type": "open_app",
     "app_name": "exact_app_name",
     "args": []
   }
   
   For SEARCH command:
   {
     "type": "web_search",
     "query": "the search terms",
     "num_results": 5
   }
   
   For VISIT/BROWSE command:
   {
     "type": "open_url",
     "url": "https://example.com"
   }
   
   For TYPE command:
   {
     "type": "type_text",
     "text": "the text to type",
     "interval": 0.02
   }

8. INSTRUCTION FOLLOWING CHECKLIST
   ==================================================
   Before executing ANY action:
   
   [ ] Is this what the user ACTUALLY asked for?
   [ ] Did I identify the verb correctly?
   [ ] Did I identify the target correctly?
   [ ] Is the action type matching the verb?
   [ ] Have I checked PC configuration knowledge?
   [ ] Would the user be surprised by this action?
   [ ] Is there a more obvious interpretation?
   
   If ANY checkbox fails → ASK FOR CLARIFICATION

9. SPECIAL CASES
   ==================================================
   
   Case: User says "open Safari"
   → On Windows PC, Safari doesn't exist
   → Response: "Safari is not available on Windows. Would you like to open Chrome or Firefox instead?"
   
   Case: User says "open Photoshop"
   → Check if Photoshop is installed
   → If yes: open it
   → If no: "Photoshop is not installed. Would you like me to search for alternatives?"
   
   Case: User says "open Google"
   → Ambiguous: app or website?
   → Check if Google app exists locally (unlikely on Windows)
   → Default to: open google.com URL
   → OR offer both options
   
   Case: User says "look up Photoshop"
   → LOOK UP = search verb
   → Execute web_search, don't try to open app
   → Even if Photoshop is installed

10. USING WEB SCRAPER FOR CONTEXT
    ==================================================
    The system has access to web scraper for:
    - Getting Windows 11 capabilities
    - Learning about installed apps
    - Finding documentation for apps
    - Understanding PC specifications
    
    Use this to make better decisions:
    - "Do I know about this app?"
    - "What are the features available?"
    - "Is this a valid command for this PC?"
    - "What are the alternatives?"

=== IMPLEMENTATION NOTES ===

1. Decision Priority:
   - Explicit verb ALWAYS takes precedence
   - "SEARCH" verb > "OPEN" verb
   - "OPEN" verb + LOCAL APP > ambiguous target
   - Context from PC config > generic assumptions

2. Error Handling:
   - If app doesn't exist → explain clearly
   - If action fails → don't silently fallback to web search
   - If ambiguous → ask user, don't guess

3. Learning:
   - Log every decision and its confidence
   - Learn which disambiguations the user prefers
   - Improve over time with feedback

4. Transparency:
   - Always explain WHY you're taking an action
   - Show the parsed intent to user if uncertain
   - Let user correct misinterpretations
"""

# Configuration
DEFAULT_CONFIDENCE_THRESHOLD = 0.75  # Don't execute actions below this confidence without clarification

KNOWN_LOCAL_APPS = {
    # Windows built-in
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "paint": "mspaint.exe",
    "wordpad": "wordpad.exe",
    "taskmgr": "taskmgr.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:",
    "control panel": "control.exe",
    
    # Common installed apps
    "chrome": "chrome.exe",
    "firefox": "firefox.exe",
    "edge": "msedge.exe",
    "microsoft edge": "msedge.exe",
    "vscode": "code.exe",
    "visual studio code": "code.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "outlook": "outlook.exe",
    "teams": "teams.exe",
    "spotify": "spotify.exe",
    "steam": "steam.exe",
    "discord": "discord.exe",
}

SEARCH_VERBS = ["search", "look up", "find", "query", "investigate", "research"]
OPEN_VERBS = ["open", "start", "launch", "run"]
BROWSE_VERBS = ["browse", "visit", "go to", "navigate to"]
CLOSE_VERBS = ["close", "quit", "exit", "shutdown"]
TYPE_VERBS = ["type", "write", "input", "compose", "enter"]

WEB_MARKERS = [
    "latest", "today", "current", "news", "documentation",
    "docs", "tutorial", "how to", "from internet", "look it up",
    "search online", "official", "api", "wikipedia", "github",
    "price", "release", "version", "2024", "2025"
]

def get_instruction_following_guide() -> str:
    """Get the full instruction following guide"""
    return INSTRUCTION_FOLLOWING_RULES
