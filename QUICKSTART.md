# Jarvis Cloud Assistant - Complete System Quickstart

## System Overview

Your Jarvis system is now configured to use your **custom fine-tuned Qwen 7B model** as the primary LLM by default. No environment variables needed for basic setup!

### Architecture

```
Frontend (UI)
    ↓
/api/chat endpoint (FastAPI)
    ↓
Chat Orchestrator
    ↓
LLM Adapter (routes to your custom model)
    ↓
Model Service (port 8010)
    ├─ Base Model: Qwen/Qwen2.5-7B-Instruct
    └─ LoRA Adapter: auto-loads from models/jarvis-lora
    ↓
OpenAI-compatible endpoint
    ↓
AI Response → UI
```

---

## Complete Workflow

### 1. Start the Model Service (Required)

This runs your custom fine-tuned model locally:

```bash
# Terminal 1: Start model service
python apps/model_service/server.py
```

**What happens:**
- Loads base Qwen 7B model from HuggingFace
- Auto-detects and loads any fine-tuned LoRA adapters from `models/jarvis-lora/`
- Exposes OpenAI-compatible API at `http://127.0.0.1:8010/v1/chat/completions`
- Logs: `[INFO] Loaded base model: Qwen/Qwen2.5-7B-Instruct`

**Health check:**
```bash
curl http://localhost:8010/health
# Expected: {"status": "ok", "loaded_model": "Qwen/Qwen2.5-7B-Instruct", "lora_adapter": null}
```

### 2. Start Jarvis Main App (Backend + Frontend)

```bash
# Terminal 2: Start Jarvis
python apps/web/app.py
```

**What happens:**
- Initializes FastAPI server on `http://localhost:8000`
- Connects to MongoDB (if available, otherwise uses in-memory buffers)
- Loads LLMAdapter configured to use your custom model
- Starts background services: learning system, training jobs, autonomy engine
- Serves React frontend at `http://localhost:8000`

**Logs to watch for:**
```
[INFO] Loaded base model: Qwen/Qwen2.5-7B-Instruct
[INFO] LLMAdapter initialized: primary=Qwen/Qwen2.5-7B-Instruct
[INFO] MongoDB connected: jarvis_db
[INFO] Jarvis Brain initialized with learning system
```

### 3. Open UI and Test

```bash
# Browser: Navigate to
http://localhost:8000
```

**Test flow:**
1. Type a message in chat
2. Observe it calling your model service
3. See intelligent response from Qwen 7B (or LoRA-fine-tuned variant)

---

## Training Workflow (Complete End-to-End)

### Step 1: Prepare Training Data

Create `data/ai_training/sft.jsonl` with examples:

```jsonl
{"user": "What is artificial intelligence?", "assistant": "AI is machine learning and neural networks..."}
{"user": "Explain LoRA", "assistant": "LoRA is Low-Rank Adaptation, a parameter-efficient..."}
{"user": "How do I train a model?", "assistant": "You prepare data, then run the training script..."}
```

**Requirements:**
- At least 10 examples (recommended: 50-200)
- Keys can be: `user`/`assistant` OR `input`/`output` OR `prompt`/`response`
- UTF-8 encoded JSONL format

### Step 2: Train LoRA Adapter

```bash
python scripts/train_self_hosted_lora.py \
  --dataset data/ai_training/sft.jsonl \
  --base-model Qwen/Qwen2.5-7B-Instruct \
  --output-dir models/jarvis-lora \
  --epochs 3 \
  --batch-size 1
```

**What happens:**
- Loads base Qwen model
- Creates LoRA adapter (Low-Rank, ~10MB)
- Trains on your data for 3 epochs
- Saves to `models/jarvis-lora/adapter_config.json` + weights
- Logs training progress and loss

**Expected time:** 2-10 minutes (depending on GPU and data size)

### Step 3: Automatically Load Fine-Tuned Model

The model service **automatically** detects and loads your LoRA adapter on startup:

```bash
# Restart model service (if running)
# Kill current: Ctrl+C
python apps/model_service/server.py
```

**Health check after loading:**
```bash
curl http://localhost:8010/health
# Expected: {"status": "ok", "loaded_model": "Qwen/...", "lora_adapter": "models/jarvis-lora"}
```

### Step 4: Use Your Fine-Tuned Model

Chat normally - your model now uses the fine-tuned weights!

```
User: "What is LoRA?"
Model Response: [Uses your training examples, personalized to your data]
```

---

## System Configuration (Already Set Up)

**File: `src/config/runtime_defaults.py`**

```python
# Your custom model is PRIMARY (no env vars needed!)
SELF_HOSTED_LLM_ENABLED: bool = True
SELF_HOSTED_LLM_ENDPOINT: str = "http://127.0.0.1:8010/v1/chat/completions"
SELF_HOSTED_LLM_MODEL: str = "Qwen/Qwen2.5-7B-Instruct"
```

**LLM Routing Logic (automatic):**
1. Check if model service is running on port 8010 ✓
2. If yes → use custom model (primary)
3. If no → fallback to OpenAI gpt-4o-mini (requires OPENAI_API_KEY)
4. If OpenAI unavailable → fallback to Groq (requires GROQ_API_KEY)

---

## Optional: Advanced Configuration

### Add OpenAI Backup (Optional)

Create `.env` file in project root:

```bash
OPENAI_API_KEY=sk-...your-key...
GROQ_API_KEY=gsk-...your-key...
```

This enables fallback if model service is unavailable.

### MCP Server (Automated Tools)

Start in separate terminal for automated file/git operations:

```bash
python mcp_server/server.py
```

Port: `9090` (requires JWT auth token)

### Enable Learning System

The system automatically learns from conversations:

```python
# Already enabled in runtime_defaults.py
LEARNING_ENABLED: bool = True
LEARNING_BUFFER_MAX: int = 2000
MIN_FINETUNE_EXAMPLES: int = 10
```

Learned examples auto-prepare for training when 10+ examples collected.

---

## Troubleshooting

### Issue: "Connection refused on 127.0.0.1:8010"

**Cause:** Model service not running

**Fix:**
```bash
python apps/model_service/server.py
# Should see: Loaded base model: Qwen/Qwen2.5-7B-Instruct
```

### Issue: "CUDA out of memory"

**Cause:** GPU doesn't have enough VRAM

**Fix:**
- Reduce batch-size: `--batch-size 1` (default)
- Use CPU: Modify `apps/model_service/server.py` line 69 to force float32

### Issue: "LoRA adapter not found"

**Cause:** Training script failed or wrong output path

**Fix:**
```bash
ls -la models/jarvis-lora/
# Should have: adapter_config.json, adapter_model.bin
```

### Issue: Chat responses are generic

**Cause:** LoRA not loaded or training needs more examples

**Fix:**
1. Verify health endpoint shows LoRA loaded
2. Add more training examples (50+)
3. Re-train: `python scripts/train_self_hosted_lora.py --epochs 5`

---

## Command Reference

| Command | Purpose |
|---------|---------|
| `python apps/model_service/server.py` | Start custom model service |
| `python apps/web/app.py` | Start main Jarvis app + UI |
| `python scripts/train_self_hosted_lora.py --dataset data/ai_training/sft.jsonl` | Fine-tune model |
| `curl http://localhost:8010/health` | Check model service health |
| `curl http://localhost:8000/health` | Check main app health |
| `python mcp_server/server.py` | Start MCP automation server |

---

## File Structure

```
Jarvis/
├── apps/
│   ├── model_service/
│   │   └── server.py          # Your custom model API
│   └── web/
│       └── app.py             # Main Jarvis backend + frontend
├── frontend/
│   └── src/
│       └── App.jsx            # React UI
├── scripts/
│   └── train_self_hosted_lora.py  # LoRA training
├── models/
│   └── jarvis-lora/           # Fine-tuned weights (auto-loaded)
├── data/
│   └── ai_training/
│       └── sft.jsonl          # Your training data
└── src/
    ├── config/
    │   └── runtime_defaults.py    # Pre-configured for your system
    └── core/
        ├── llm_adapter.py     # LLM routing logic
        └── jarvis_brain.py    # Main intelligence
```

---

## System is Now Intelligent & Complete

✓ Custom model (Qwen 7B) as primary  
✓ Auto-loading LoRA fine-tuning  
✓ Complete training workflow  
✓ No environment variables required  
✓ Automatic fallback to OpenAI/Groq if needed  
✓ Learning system enabled  
✓ MCP automation ready  
✓ Frontend UI fully functional  

**Start using it now:**

```bash
# Terminal 1
python apps/model_service/server.py

# Terminal 2
python apps/web/app.py

# Browser
http://localhost:8000
```

Enjoy your intelligent Jarvis assistant!
