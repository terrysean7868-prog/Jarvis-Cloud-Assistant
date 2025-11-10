# 📁 Folder Structure Guide

## Overview

The JARVIS project uses an organized modular structure for better maintainability, scalability, and faster development.

```
jarvis-cloud-assistant/
├── 🚀 Entry Points
│   ├── app.py                         # FastAPI main application
│   ├── run_jarvis.py                  # Command-line runner
│   └── JARVIS.bat                     # Windows launcher
│
├── 📂 src/ - Main Source Code (NEW)
│   ├── __init__.py                    # Package initializer
│   │
│   ├── core/                          # AI Core Logic
│   │   ├── __init__.py
│   │   ├── llm_adapter.py            # LLM (OpenAI/Groq) adapter
│   │   ├── jarvis_brain.py           # Main AI intelligence
│   │   ├── cognitive_core.py         # Cognitive processing
│   │   └── executor.py               # Execute actions
│   │
│   ├── api/                           # API & Routes
│   │   ├── __init__.py
│   │   └── endpoints.py              # Route definitions
│   │
│   ├── internet/                      # Internet Access
│   │   ├── __init__.py
│   │   ├── web_scraper.py           # Web scraping engine
│   │   └── internet_access.py        # High-level API
│   │
│   ├── memory/                        # Memory System
│   │   ├── __init__.py
│   │   ├── bot_memory.py             # Memory management
│   │   └── conversation_store.py     # Conversation DB
│   │
│   ├── jobs/                          # Background Jobs
│   │   ├── __init__.py
│   │   ├── scheduler.py              # Job scheduler
│   │   └── tasks.py                  # Job definitions
│   │
│   ├── config/                        # Configuration
│   │   ├── __init__.py
│   │   ├── settings.py               # App settings
│   │   └── constants.py              # Constants
│   │
│   └── utils/                         # Utilities
│       ├── __init__.py
│       ├── db.py                     # Database utilities
│       ├── git_sync.py               # Git operations
│       ├── logger.py                 # Logging setup
│       └── helpers.py                # Helper functions
│
├── 📂 jarvis-frontend/               # React Frontend
│   ├── src/
│   │   ├── App.jsx                  # Main component
│   │   ├── App.css                  # Animated styles
│   │   ├── index.js                 # Entry point
│   │   └── reactor.css              # Ring animations
│   ├── public/                      # Static files
│   ├── build/                       # Production build
│   ├── package.json                 # Dependencies
│   └── package-lock.json
│
├── 📂 docs/ - Documentation (NEW)
│   ├── FEATURES.md                  # Feature list
│   ├── INTERNET_FEATURES.md         # Internet guide
│   ├── INTERNET_SETUP.md            # Setup guide
│   ├── INTERNET_COMPLETE.md         # Complete reference
│   ├── INTERNET_CHECKLIST.md        # Checklist
│   └── ARCHITECTURE.md              # System architecture
│
├── 📂 data/                         # Data Files
│   ├── training_data.py             # Training examples
│   └── seed_training_data.py        # Data seeding
│
├── 📂 utils/ (Legacy)               # Utility Modules
│   ├── db.py                        # Database helper
│   ├── auto_sync.py                 # Auto-sync
│   ├── module_generator.py          # Module gen
│   └── scheduler.py                 # Scheduler utils
│
├── 📂 modules/                      # Plugin Modules
│   ├── __init__.py
│   ├── search.py                    # Search module
│   ├── weather.py                   # Weather module
│   ├── voice.py                     # Voice module
│   ├── reminder.py                  # Reminder module
│   ├── note.py                      # Note module
│   ├── learn.py                     # Learn module
│   └── currency_converter.py        # Currency module
│
├── 📂 models/                       # ML Models
│   └── (empty - for future)
│
├── 📂 data-local/                   # Local Data (not in git)
│   └── (database files)
│
├── ⚙️ Configuration Files
│   ├── .env                         # Environment (NOT in git)
│   ├── .env.example                 # Template
│   ├── .env.template                # Backup template
│   ├── .gitignore                   # Git ignore
│   └── .replit                      # Replit config
│
├── 📦 Dependencies
│   ├── requirements.txt              # All dependencies
│   ├── requirements-core.txt        # Core only
│   ├── requirements-extras.txt      # Extra features
│   └── package.json                 # Node.js deps
│
├── 📖 Documentation
│   ├── README.md                    # Main readme (UPDATED)
│   ├── IMPLEMENTATION_SUMMARY.md    # Implementation
│   ├── INTERNET_IMPLEMENTATION_COMPLETE.md
│   ├── INTERNET_FINAL_SUMMARY.md
│   ├── OPTIMIZATION.md              # Performance
│   ├── INSTALL.md                   # Installation
│   ├── FEATURES.md                  # Features
│   └── INTERNET_CHECKLIST.md        # Checklist
│
└── 📁 Special Directories
    ├── __pycache__/                 # Python cache (ignored)
    ├── .git/                        # Git repository
    ├── .idea/                       # IDE config (ignored)
    ├── .github/                     # GitHub workflows
    ├── node_modules/                # Node packages (ignored)
    ├── venv/                        # Virtual env (ignored)
    └── venv_test/                   # Test env (ignored)
```

---

## 📂 Directory Purposes

### `src/` - Main Application Code
The reorganized source code following best practices:
- **core/** - LLM, brain, execution logic
- **api/** - FastAPI routes and endpoints
- **internet/** - Web scraping and search
- **memory/** - Conversation and context storage
- **jobs/** - Background scheduler and tasks
- **config/** - Settings and constants
- **utils/** - Shared utility functions

**Benefits:**
- ✅ Clear separation of concerns
- ✅ Easier to locate functionality
- ✅ Better code reusability
- ✅ Improved testing and mocking
- ✅ Scalable architecture

### `docs/` - Documentation
Comprehensive documentation for all features:
- Feature descriptions
- Setup instructions
- Complete API reference
- Architecture documentation
- Troubleshooting guides

**Benefits:**
- ✅ Centralized documentation
- ✅ Easy to find help
- ✅ Organized by topic
- ✅ Always up-to-date

### `jarvis-frontend/` - React UI
Modern responsive frontend with animations:
- Animated dotted rings (3 concentric circles)
- Responsive design (mobile, tablet, desktop)
- Real-time status indicators
- URL opening capabilities

### `modules/` - Plugin System
Extensible plugin architecture:
- Weather module
- Search module
- Voice module
- Note module
- And more...

### Legacy Directories
- `utils/` - Original utility modules (kept for compatibility)
- `models/` - ML models (empty, reserved for future)
- `data/` - Training data and examples

---

## 🔄 Import Structure

### New Import Pattern
```python
# Core modules
from src.core.llm_adapter import LLMAdapter
from src.core.jarvis_brain import JarvisBrain
from src.core.executor import ActionExecutor

# Internet access
from src.internet.web_scraper import WebScraper
from src.internet.internet_access import InternetAccess

# Memory
from src.memory.bot_memory import BotMemory

# Jobs
from src.jobs.scheduler import JobScheduler

# Utilities
from src.utils.db import Database
from src.utils.git_sync import git_sync
from src.utils.logger import setup_logging
```

### Backward Compatibility
Old imports still work:
```python
# Old way (still works)
from llm_adapter import LLMAdapter
from internet import InternetAccess
```

---

## ✨ Benefits of New Structure

### 1. **Better Organization**
- Logical grouping of related code
- Easy to find what you need
- Clear module responsibilities

### 2. **Faster Development**
- Reduced import complexity
- Better IDE support
- Faster code navigation

### 3. **Improved Maintainability**
- Clear file purposes
- Easier refactoring
- Better code reuse

### 4. **Scalability**
- Easy to add new features
- New modules can follow same pattern
- Room for growth

### 5. **Team Collaboration**
- Clear structure for new developers
- Reduced merge conflicts
- Better documentation

### 6. **Performance**
- Better module caching
- Optimized imports
- Faster startup time

---

## 🚀 Migration Guide

### For Developers
If you're updating code:

1. **Update imports** in your files
```python
# Old
from llm_adapter import LLMAdapter

# New
from src.core.llm_adapter import LLMAdapter
```

2. **Move files** to appropriate folders (already done)

3. **Update tests** to use new import paths

### For Deployment
1. Pull latest code
2. Install dependencies: `pip install -r requirements.txt`
3. Configuration stays the same (`.env`)
4. Application runs the same way: `python app.py`

### For Frontend
1. No changes needed
2. Frontend runs independently
3. Same API endpoints

---

## 📊 Statistics

```
Total Source Files:     30+ Python files
Organized Modules:      7 main packages
Documentation:          6 comprehensive guides
API Endpoints:          6+ endpoints
Background Jobs:        5 scheduled tasks
Frontend Components:    5+ React components
Utility Functions:      20+ helpers
```

---

## 🎯 Folder Growth Plan

### Phase 1 (Current - v3.5.0)
- ✅ Core reorganization
- ✅ Documentation centralization
- ✅ Package structure setup

### Phase 2 (v4.0.0)
- [ ] Add `tests/` directory with pytest
- [ ] Add `examples/` with integration examples
- [ ] Add `scripts/` for automation
- [ ] Add `migrations/` for database migrations

### Phase 3 (v5.0.0)
- [ ] Microservices split
- [ ] Docker containers
- [ ] Kubernetes deployments
- [ ] Advanced CI/CD

---

## 🔧 File Addition Guidelines

### Adding New Modules
1. Determine category: core, api, internet, etc.
2. Create file in appropriate `src/` subdirectory
3. Add docstring explaining purpose
4. Include type hints
5. Add error handling
6. Update relevant `__init__.py`
7. Update imports in dependent files
8. Add documentation in `/docs`
9. Commit with proper message

### Adding New Features
1. Create module following above pattern
2. Add corresponding endpoint in `api/`
3. Add background job if needed in `jobs/`
4. Add tests in `tests/`
5. Document in `/docs`
6. Update `README.md`

---

## 📝 File Naming Conventions

```
Python Files:
  snake_case.py          # Modules
  MyClass               # Classes inside
  my_function()         # Functions
  MY_CONSTANT           # Constants

Directories:
  lowercase_with_underscore/

Documentation:
  CAPS_WITH_UNDERSCORES.md
  CamelCase.md

Config:
  .env                  # Sensitive (not in git)
  .env.example          # Template (in git)
  .env.template         # Backup template
```

---

## 🎉 Complete Structure Benefits

✅ **Organized** - Logical file grouping  
✅ **Scalable** - Easy to add new features  
✅ **Maintainable** - Clear responsibilities  
✅ **Professional** - Industry-standard layout  
✅ **Documented** - Comprehensive guides  
✅ **Fast** - Quick development and deployment  
✅ **Team-Friendly** - Onboarding easier  

---

**Folder Structure Version:** 1.0  
**Updated:** November 10, 2025  
**Status:** ✅ Production Ready

🚀 **Ready for scalable growth!** 🚀

