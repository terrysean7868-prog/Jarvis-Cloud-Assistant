# Jarvis Configuration Guide

This guide covers advanced configuration for Jarvis Cloud Assistant, including self-hosted LLM setup, model fine-tuning, MCP server security, and learning system configuration.

---

## Table of Contents

1. [Self-Hosted LLM](#self-hosted-llm)
2. [LoRA Fine-Tuning](#lora-fine-tuning)
3. [MCP Server Security](#mcp-server-security)
4. [Learning System](#learning-system)
5. [N8N Automation](#n8n-automation)
6. [Environment Variables](#environment-variables)

---

## Self-Hosted LLM

### Overview

Jarvis can run with a self-hosted LLM service instead of relying on OpenAI/Groq. By default, this feature is **disabled** to prevent startup errors. You must explicitly enable it and run a local LLM server.

### Setup Steps

#### 1. Install Ollama (Recommended)

- **macOS**: Download from [ollama.ai](https://ollama.ai)
- **Linux**: `curl https://ollama.ai/install.sh | sh`
- **Windows**: Download from [ollama.ai](https://ollama.ai)

#### 2. Start an Ollama Model

```bash
# Pull and run a model
ollama run qwen:7b-instruct

# Or use another model
ollama run llama2
ollama run neural-chat
```

The model will be available at `http://localhost:11434`

#### 3. Start Jarvis Model Service

The Jarvis model service provides an OpenAI-compatible endpoint at `http://127.0.0.1:8010`:

```bash
# Install model service dependencies
pip install -r requirements/model_service.txt

# Start the service
python apps/model_service/server.py
```

#### 4. Enable Self-Hosted LLM in Jarvis

Edit `src/config/runtime_defaults.py`:

```python
SELF_HOSTED_LLM_ENABLED: bool = True  # Enable self-hosted model
SELF_HOSTED_LLM_ENDPOINT: str = "http://127.0.0.1:8010/v1/chat/completions"
SELF_HOSTED_LLM_MODEL: str = "Qwen/Qwen2.5-7B-Instruct"  # Your model ID
```

#### 5. Verify it Works

```bash
# Start Jarvis (FastAPI + web app)
python apps/web/app.py

# In browser: http://localhost:8000
# Send a message to test the self-hosted LLM
```

Check the console logs to verify the model service is being used:
```
[INFO] Loaded base model: Qwen/Qwen2.5-7B-Instruct
```

### Fallback Behavior

If the self-hosted LLM service is unavailable, Jarvis automatically falls back to OpenAI/Groq. This ensures robustness in production.

To monitor fallback behavior, check:
- `src/core/llm_adapter.py:115-124` (fallback logic)
- Console logs for warnings about service availability

---

## LoRA Fine-Tuning

### Overview

LoRA (Low-Rank Adaptation) allows you to fine-tune models on custom data without massive compute resources. Jarvis supports two training scripts:

1. **`train_self_hosted_lora.py`** – Recommended for production training
2. **`train_model_job.py`** – Legacy training pipeline

### Prerequisites

```bash
# Install training dependencies
pip install -r requirements/model_service.txt
```

### Training with train_self_hosted_lora.py

#### 1. Prepare Training Data

Create JSONL file with user/assistant pairs:

```bash
# File: data/ai_training/sft.jsonl
{"user": "What is AI?", "assistant": "AI is artificial intelligence..."}
{"user": "Explain LoRA", "assistant": "LoRA is Low-Rank Adaptation..."}
```

Keys can be: `user`/`assistant`, `input`/`output`, or `prompt`/`response`

#### 2. Run Training

```bash
python scripts/train_self_hosted_lora.py \
  --dataset data/ai_training/sft.jsonl \
  --base-model Qwen/Qwen2.5-7B-Instruct \
  --output-dir models/jarvis-lora \
  --epochs 1 \
  --batch-size 1
```

**Options**:
- `--dataset`: Path to JSONL training data (required)
- `--base-model`: Base model ID (default: `Qwen/Qwen2.5-7B-Instruct`)
- `--output-dir`: Where to save LoRA weights (default: `models/jarvis-lora`)
- `--epochs`: Number of training epochs (default: 1)
- `--batch-size`: Batch size for training (default: 1, increase if GPU memory allows)

#### 3. Load Trained Model

After training completes, the adapter is automatically loaded on next model service startup. Verify by checking the `/health` endpoint:

```bash
curl http://localhost:8010/health
```

Response should show:
```json
{
  "status": "ok",
  "loaded_model": "Qwen/Qwen2.5-7B-Instruct",
  "lora_adapter": "models/jarvis-lora"
}
```

#### 4. Manual LoRA Loading

To load a different LoRA adapter at runtime:

```bash
curl -X POST http://localhost:8010/v1/load-lora-weights \
  -H "Content-Type: application/json" \
  -d '{"adapter_name": "models/jarvis-lora", "merge": false}'
```

**Parameters**:
- `adapter_name`: Path to LoRA adapter directory
- `merge` (optional): If true, merges LoRA weights into base model (one-way operation)

### Auto-Loading on Startup

The model service automatically checks for LoRA adapters in these locations:

1. `models/jarvis-lora` (train_self_hosted_lora.py default)
2. `models/jarvis_custom` (train_model_job.py default)

If found, the adapter is automatically loaded on service startup.

### Minimum Data Requirements

- **Minimum 10 examples** for meaningful training
- **Recommended 50-200 examples** for production use
- Larger datasets (1000+) provide better results

### Training Tips

1. **Start small**: Train with `--batch-size 1` on first attempt
2. **Monitor memory**: Increase batch size if GPU memory available
3. **Test incrementally**: Train with 10 examples first, evaluate, then expand
4. **Preserve base model**: LoRA doesn't modify the base model, so you can always revert

---

## MCP Server Security

### Overview

The MCP (Model Context Protocol) server runs on port 9090 and provides tools for file operations, git commands, and shell execution. It now requires JWT authentication for security.

### Security Features

✅ **Command Injection Prevention**: Uses `subprocess.run()` with list arguments, not shell interpolation
✅ **JWT Authentication**: All requests require valid JWT token from main app
✅ **Path Sandboxing**: File operations restricted to project directory
✅ **Audit Logging**: All operations logged for security review
✅ **Command Whitelisting**: Shell commands limited to safe operations (pip, npm, python, etc.)

### Starting MCP Server

```bash
# From project root
python mcp_server/server.py
```

Server starts on `http://0.0.0.0:9090`

### Sending Authenticated Requests

All requests to MCP server require JWT bearer token:

```bash
# Get JWT token from main Jarvis app
curl -X POST http://localhost:8000/auth/login \
  -d "username=user&password=pass"
# Returns: {"access_token": "..."}

# Use token to call MCP tools
curl -X POST http://localhost:9090/tools/read_file \
  -H "Authorization: Bearer <your_jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{"path": "README.md"}'
```

### Available Tools

#### File Operations
- `read_file(path)` - Read file contents
- `write_file(path, content)` - Create/overwrite file
- `patch_file(path, search, replace)` - Search and replace
- `list_files(directory)` - List directory
- `delete_file(path)` - Delete file
- `copy_file(source, destination)` - Copy file
- `create_directory(path)` - Create directory
- `delete_directory(path)` - Remove directory

#### Git Operations
- `git_commit(msg)` - Commit all changes with message
- `git_push()` - Push to origin/main

#### Shell Commands
- `run_command(cmd)` - Run whitelisted commands only
  - **Allowed**: pip, npm, python, node, echo, cat, ls, pwd, find
  - **Blocked**: rm, dd, poweroff, and other dangerous commands

### Security Best Practices

1. **Protect JWT_SECRET**: Use strong secret in `JARVIS_JWT_SECRET`
2. **Restrict Network Access**: Run MCP on localhost or behind VPN
3. **Monitor Audit Logs**: Check logs for suspicious git/file operations
4. **Disable if Unused**: Stop MCP server if not using automated tools
5. **Update Regularly**: Keep peft/transformers/torch updated

### Troubleshooting

**401 Unauthorized**:
```
Cause: Missing or invalid JWT token
Fix: Ensure Authorization header has valid token from main app
```

**Path outside project directory**:
```
Cause: Attempting to access files outside sandbox
Fix: All file paths must be inside project directory
```

**Command not in whitelist**:
```
Cause: Attempting to run non-whitelisted command
Fix: Edit allowed_prefixes in mcp_server/tools/run_tools.py to expand allowed commands
```

---

## Learning System

### Overview

Jarvis can learn from user interactions and automatically fine-tune models. The learning system stores examples in MongoDB and prepares JSONL datasets for training.

### Configuration

Edit `src/config/runtime_defaults.py`:

```python
LEARNING_ENABLED: bool = True              # Enable learning
LEARNING_BUFFER_MAX: int = 2000            # Max in-memory examples
MIN_FINETUNE_EXAMPLES: int = 10            # Min examples to trigger training
LEARNING_RETRIEVE: bool = True             # Retrieve learned examples
LOCAL_REASONER_PREWARM_INTERVAL_SECONDS: int = 86400  # Daily refresh
```

### How It Works

1. **Capture**: Each user interaction (prompt + response) is captured
2. **Redact**: Secrets and API keys are automatically redacted
3. **Store**: Examples stored in MongoDB (`learning_buffer` collection)
4. **Prepare**: Periodically converted to JSONL format for training
5. **Train**: Triggered when min examples threshold reached

### Preparing Training Data

Convert learned interactions to JSONL:

```python
from src.core.jarvis_brain import JarvisBrain

brain = JarvisBrain(llm)
dataset = brain.prepare_finetune_dataset()
# Saves to: data/ai_training/sft_from_learning.jsonl
```

Then train:

```bash
python scripts/train_self_hosted_lora.py \
  --dataset data/ai_training/sft_from_learning.jsonl
```

### Disabling Learning

In production or for privacy, disable learning:

```python
LEARNING_ENABLED: bool = False
```

---

## N8N Automation

### Overview

Jarvis can trigger N8N workflows for automation and RPA. This requires N8N server setup and credentials.

### Configuration

Edit `src/config/secrets.py`:

```python
def n8n_secrets() -> dict:
    return {
        "base_url": os.getenv("JARVIS_N8N_WEBHOOK_BASE", ""),
        "token": os.getenv("JARVIS_N8N_WEBHOOK_TOKEN", ""),
        "secret": os.getenv("JARVIS_N8N_WEBHOOK_SECRET", ""),
    }
```

Add to `.env`:

```bash
JARVIS_N8N_WEBHOOK_BASE=https://n8n.example.com
JARVIS_N8N_WEBHOOK_TOKEN=your_n8n_api_token
JARVIS_N8N_WEBHOOK_SECRET=webhook_secret_key
```

### Calling N8N Workflows

In chat, users can trigger workflows:

```
"Run the data export workflow"
"Trigger the notification pipeline"
```

The executor will call the appropriate N8N webhook and wait for completion.

### Workflow Integration

N8N workflows receive:
- User ID
- Request description
- Context variables

See `src/core/executor.py:237-324` for webhook invocation details.

---

## Environment Variables

All environment variables are defined in `.env.template`. Below is a reference:

### LLM Configuration

| Variable | Purpose | Default |
|----------|---------|---------|
| `OPENAI_API_KEY` | OpenAI API key | Required |
| `GROQ_API_KEY` | Groq backup provider | Optional |
| `SELF_HOSTED_LLM_API_KEY` | Local LLM auth | Optional |

### Database

| Variable | Purpose | Default |
|----------|---------|---------|
| `MONGODB_URI` | MongoDB connection | Required |
| `MONGODB_DB_NAME` | Database name | `jarvis` |
| `JARVIS_REDIS_URL` | Redis cache | Optional |

### Security

| Variable | Purpose | Default |
|----------|---------|---------|
| `JARVIS_JWT_SECRET` | JWT signing key | Required |
| `JARVIS_JWT_ISSUER` | JWT issuer | Required |

### Integrations

| Variable | Purpose | Default |
|----------|---------|---------|
| `GITHUB_TOKEN` | GitHub API token | Optional |
| `TELEGRAM_TOKEN` | Telegram bot token | Optional |
| `GEMINI_API_KEY` | Google AI API | Optional |

### MCP Server

| Variable | Purpose | Default |
|----------|---------|---------|
| `JARVIS_JWT_SECRET` | Used for MCP auth | (shared with main app) |

### N8N Workflows

| Variable | Purpose | Default |
|----------|---------|---------|
| `JARVIS_N8N_WEBHOOK_BASE` | N8N server URL | Optional |
| `JARVIS_N8N_WEBHOOK_TOKEN` | N8N API token | Optional |
| `JARVIS_N8N_WEBHOOK_SECRET` | Webhook secret | Optional |

---

## Troubleshooting

### Model Service Won't Start

**Error**: `transformers/torch are not installed`

```bash
pip install -r requirements/model_service.txt
```

### LLM Endpoint Unreachable

**Error**: `ConnectionError: http://127.0.0.1:8010`

```bash
# Check if model service is running
curl http://localhost:8010/health

# Start model service if needed
python apps/model_service/server.py
```

### LoRA Adapter Not Loading

**Check**:
1. Adapter exists at specified path
2. `adapter_config.json` is present
3. Model service logs for errors

```bash
ls -la models/jarvis-lora/adapter_config.json
```

### MCP Authentication Fails

**Error**: `401 Unauthorized`

```bash
# Verify JWT secret matches main app
echo $JARVIS_JWT_SECRET

# Test with valid token
curl -X POST http://localhost:9090/tools/read_file \
  -H "Authorization: Bearer <valid_token>"
```

---

## See Also

- [Model Service API](./MODEL_SERVICE.md)
- [LoRA Training Guide](./LORA_TRAINING.md)
- [MCP Server API](./MCP_SERVER.md)
- [Learning System Architecture](./LEARNING_SYSTEM.md)
