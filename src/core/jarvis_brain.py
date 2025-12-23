# src/core/jarvis_brain.py
import asyncio
import os
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any

from src.utils.db import db
from src.core.llm_adapter import LLMAdapter
from src.utils.task_manager import task_manager

# MCP support (existing integration from prior patch)
try:
    from mcp import MCPClient
except Exception:
    MCPClient = None


class JarvisBrain:
    """Enhanced brain with memory context, reasoning, and humanlike tone."""

    def __init__(self, llm: LLMAdapter, user_id="default"):
        self.llm = llm
        self.user_id = user_id
        self.memory: List[Dict[str, str]] = []
        self.last_mode = "interactive"

        # MCP client (if installed)
        if MCPClient:
            self.mcp = MCPClient(server_url="http://localhost:9090")
            self.mcp_connected = False
        else:
            self.mcp = None
            self.mcp_connected = False

        # -------------------------
        # Self-learning components
        # -------------------------
        # In-memory buffer of interactions to convert into training examples.
        # Each item: {"prompt": str, "completion": str, "meta": {...}}
        self.learning_buffer: List[Dict[str, Any]] = []
        # Minimum number of examples to generate a dataset
        self.min_examples_for_finetune = int(os.getenv("MIN_FINETUNE_EXAMPLES", "10"))
        # Safety toggle: require manual approval before any automated code change
        self.require_manual_approval = os.getenv("REQUIRE_MANUAL_APPROVAL", "true").lower() == "true"

        # Filesystem sandboxing
        self.project_root = Path(__file__).resolve().parents[2]
        # Default allowlist: keep self-modifying actions inside the repo only.
        # You can override with JARVIS_ALLOWED_PATHS (comma-separated, relative to project root).
        allowed = os.getenv("JARVIS_ALLOWED_PATHS", "src,modules,jarvis-frontend/src,docs,data").strip()
        self.allowed_roots = []
        for rel in [p.strip() for p in allowed.split(",") if p.strip()]:
            self.allowed_roots.append((self.project_root / rel).resolve())

        # Blocklist for sensitive files/dirs even if under allowed roots
        self.blocked_names = {
            ".env",
            ".env.example",
            "id_rsa",
            "id_rsa.pub",
        }
        self.blocked_dirnames = {
            ".git",
            "venv",
            "__pycache__",
            ".pytest_cache",
            "node_modules",
        }

    def is_path_allowed(self, path: str) -> bool:
        """Return True if the given path is inside the allowed sandbox.

        This is used by the action executor to prevent arbitrary file access.
        """
        try:
            if not path:
                return False

            p = Path(path)
            if not p.is_absolute():
                p = (self.project_root / p)
            rp = p.resolve()

            # Must be inside project root at all
            try:
                rp.relative_to(self.project_root)
            except Exception:
                return False

            # Block sensitive names / directories
            if rp.name in self.blocked_names:
                return False
            if any(part in self.blocked_dirnames for part in rp.parts):
                return False

            # Must be inside one of the allowed roots
            for root in self.allowed_roots:
                try:
                    rp.relative_to(root)
                    return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    # -------------------------
    # MCP helpers (non-breaking)
    # -------------------------
    async def ensure_mcp(self):
        if not self.mcp:
            return False
        if not self.mcp_connected:
            try:
                await self.mcp.connect()
                self.mcp_connected = True
                print("[MCP] Connected")
            except Exception as e:
                print(f"[MCP] Connection failed: {e}")
                self.mcp_connected = False
        return self.mcp_connected

    async def run_mcp_tool(self, tool_name, args):
        if not self.mcp:
            return f"[MCP] Not available in this environment."
        ok = await self.ensure_mcp()
        if not ok:
            return f"[MCP] Cannot run tool '{tool_name}' — server unavailable."

        try:
            result = await self.mcp.call(tool_name, args)
            return result
        except Exception as e:
            return f"[MCP] Tool error: {e}"

    # -------------------------
    # Self-learning utilities
    # -------------------------
    def _redact_for_storage(self, text: str) -> str:
        """Best-effort secret redaction before saving to DB."""
        try:
            if not text:
                return text
            import re

            out = text

            # Common API key patterns / env dumps
            out = re.sub(r"sk-[A-Za-z0-9]{20,}", "[REDACTED_API_KEY]", out)
            out = re.sub(r"(?i)(OPENAI_API_KEY|PRIMARY_API_KEY|JARVIS_JWT_SECRET|MONGODB_URI)\s*[:=]\s*[^\s\n\r]+", r"\1=[REDACTED]", out)

            # Bearer tokens
            out = re.sub(r"(?i)Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", "Bearer [REDACTED]", out)
            return out
        except Exception:
            return text

    def save_interaction_for_learning(self, user_text: str, assistant_text: str, actions: List[Dict]=None):
        """
        Add a curated example to the learning buffer.
        This collects the user prompt and the assistant reply (cleaned).
        """
        try:
            if not user_text or not assistant_text:
                return

            # Clean text minimally
            prompt = user_text.strip()
            completion = assistant_text.strip()

            # Avoid trivial or extremely short entries
            if len(prompt) < 5 or len(completion) < 5:
                return

            # Optionally attach actions metadata (useful for behavior cloning)
            example = {
                "prompt": prompt,
                "completion": completion,
                "meta": {
                    "when": datetime.utcnow().isoformat(),
                    "actions": actions or []
                }
            }

            self.learning_buffer.append(example)

            # Persist to DB (opt-out via env)
            if os.getenv("JARVIS_LEARNING_ENABLED", "true").lower() in ("1", "true", "yes", "y"):
                try:
                    db.save_learning_example(
                        user_id=self.user_id,
                        prompt=self._redact_for_storage(prompt),
                        completion=self._redact_for_storage(completion),
                        meta=example.get("meta", {}),
                        tags=[],
                    )
                except Exception as e:
                    # Non-fatal
                    db.save_system_event("learn_persist_error", str(e), "warning")

            # Keep buffer bounded in memory
            max_buf = int(os.getenv("LEARNING_BUFFER_MAX", "2000"))
            if len(self.learning_buffer) > max_buf:
                # drop oldest
                self.learning_buffer = self.learning_buffer[-max_buf:]

        except Exception as e:
            print(f"[LEARN SAVE ERROR] {e}")
            db.save_system_event("learn_save_error", str(e), "warning")

    def prepare_finetune_dataset(self, path: str):
        """
        Convert learning_buffer -> jsonl file at `path`.
        Format per OpenAI fine-tune (prompt / completion).
        Returns number of examples written.
        """
        try:
            if not self.learning_buffer:
                return 0

            # Ensure directory exists
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

            written = 0
            with open(path, "w", encoding="utf-8") as fh:
                for ex in self.learning_buffer:
                    prompt = ex.get("prompt", "")
                    # Ensure prompt closed appropriately for fine-tuning and model delim
                    prompt_text = prompt.strip() + "\n\n###\n\n"
                    completion_text = " " + ex.get("completion", "").strip() + " END"

                    obj = {"prompt": prompt_text, "completion": completion_text}
                    fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    written += 1

            return written

        except Exception as e:
            print(f"[PREPARE FINETUNE ERROR] {e}")
            db.save_system_event("prepare_finetune_error", str(e), "error")
            return 0

    async def run_self_learning_cycle(self, save_path: str = "data/finetune/jarvis_dataset.jsonl", submit: bool = False):
        """
        Prepare a fine-tune dataset from collected examples and optionally submit it.
        This is manual / on-demand — nothing runs automatically.
        - save_path: where JSONL is written
        - submit: if True, attempt to call OpenAI fine-tune endpoint (opt-in)
        Returns a report dict.
        """
        report = {"status": "started", "saved": 0, "submitted": False, "errors": []}

        try:
            # 1) If db exposes recent chats, prefer them; otherwise use in-memory buffer
            try:
                recent = []
                if hasattr(db, "load_recent_chats"):
                    recent = db.load_recent_chats(limit=1000) or []
                if recent:
                    # Convert DB chats -> candidate examples (simple heuristic)
                    for c in recent:
                        user_text = c.get("user_input") or c.get("input") or ""
                        bot_text = c.get("bot_response") or c.get("response") or ""
                        actions = None
                        # don't overwrite buffer with duplicates, but we keep them in memory as needed
                        if user_text and bot_text:
                            self.save_interaction_for_learning(user_text, bot_text, actions)
            except Exception as e:
                # Non-fatal - continue with currently collected buffer
                report["errors"].append(f"db-load-fallback:{e}")

            # 2) Validate buffer size
            cnt = len(self.learning_buffer)
            report["buffer_count"] = cnt
            if cnt < self.min_examples_for_finetune:
                report["status"] = "insufficient_examples"
                return report

            # 3) Prepare dataset
            written = self.prepare_finetune_dataset(save_path)
            report["saved"] = written
            report["status"] = "dataset_prepared"

            # 4) Optional: quick self-eval (generate few prompts from dataset with LLM and compare)
            try:
                eval_samples = min(5, max(0, written))
                eval_report = []
                for i in range(eval_samples):
                    ex = self.learning_buffer[-(i+1)]
                    prompt = ex["prompt"]
                    # Ask the LLM to produce a reply given the prompt (use mode=chat)
                    gen = await self.llm.generate_response(prompt, context="", mode="chat", capabilities=["self_eval"])
                    gen_text = gen.get("text", "").strip()
                    # Basic similarity (substring) check
                    match = ex["completion"].strip()[:80] in gen_text[:160]
                    eval_report.append({"prompt_preview": prompt[:120], "match": match, "gen": gen_text[:200]})
                report["eval"] = eval_report
            except Exception as e:
                report["errors"].append(f"eval_error:{e}")

            # 5) Optional submit to OpenAI fine-tune (explicit opt-in)
            if submit:
                # Minimal check for keys / endpoint availability
                openai_key = os.getenv("OPENAI_API_KEY") or os.getenv("PRIMARY_API_KEY")
                if not openai_key:
                    report["errors"].append("openai_key_missing")
                else:
                    # Attempt to call OpenAI fine-tune endpoint via aiohttp
                    try:
                        import aiohttp
                        # NOTE: This submission is a simple lightweight attempt.
                        # Many production steps (file upload, file_id, fine-tune creation) are omitted for brevity.
                        # For full control use the OpenAI CLI or SDK to upload file then create fine-tune.
                        report["submit_hint"] = "Use OpenAI CLI or SDK to upload JSONL and create fine-tune job. This patch does not fully orchestrate upload."
                        report["submitted"] = False
                    except Exception as e:
                        report["errors"].append(f"openai_submit_error:{e}")

            return report

        except Exception as e:
            report["status"] = "failed"
            report["errors"].append(str(e))
            db.save_system_event("self_learning_error", str(e), "error")
            return report

    # -------------------------
    # MAIN REASONING LOGIC (unchanged except for saving examples)
    # -------------------------
    async def handle_message(self, text: str, mode="chat", user_id=None):
        """Main conversational + action reasoning pipeline."""
        session_id = user_id or self.user_id
        # Keep brain identity aligned with the caller for learning/persistence.
        self.user_id = session_id
        context = "\n".join([f"{m['role']}: {m['text']}" for m in self.memory[-6:]])

        # RAG-lite: pull a few relevant past examples from DB and inject as additional context
        try:
            if os.getenv("JARVIS_LEARNING_RETRIEVE", "true").lower() in ("1", "true", "yes", "y"):
                k = int(os.getenv("JARVIS_LEARNING_RETRIEVAL_K", "3"))
                k = max(0, min(k, 8))
                if k:
                    hits = db.search_learning_examples(text, user_id=session_id, limit=k)
                    if hits:
                        max_chars = int(os.getenv("JARVIS_LEARNING_MAX_CONTEXT_CHARS", "1200"))
                        parts = []
                        for h in hits:
                            p = (h.get("prompt") or "").strip()
                            c = (h.get("completion") or "").strip()
                            if not p or not c:
                                continue
                            parts.append(f"User: {p}\nAssistant: {c}")
                        learned_block = "\n---\n".join(parts)
                        learned_block = learned_block[:max_chars]
                        if learned_block:
                            context = (context + "\n\n" if context else "") + "Learned examples (use as guidance, not verbatim):\n" + learned_block
        except Exception:
            # Never block replies on retrieval issues
            pass

        # Web knowledge: inject a few relevant internet-fetched summaries (stored in DB)
        try:
            if os.getenv("JARVIS_WEB_KNOWLEDGE_CONTEXT", "true").lower() in ("1", "true", "yes", "y"):
                k = int(os.getenv("JARVIS_WEB_KNOWLEDGE_K", "3"))
                k = max(0, min(k, 6))
                if k:
                    items = db.search_web_training(text, limit=k)
                    if items:
                        max_chars = int(os.getenv("JARVIS_WEB_KNOWLEDGE_MAX_CHARS", "1200"))
                        chunks = []
                        for it in items:
                            title = (it.get("title") or "").strip()
                            url = (it.get("url") or "").strip()
                            snippet = (it.get("snippet") or "").strip()
                            summary = (it.get("summary") or "").strip()
                            body = summary or snippet
                            if not body:
                                continue
                            header = title if title else (it.get("topic") or "").strip()
                            if url:
                                chunks.append(f"- {header}\n  {body}\n  Source: {url}")
                            else:
                                chunks.append(f"- {header}\n  {body}")
                        web_block = "\n".join(chunks)[:max_chars]
                        if web_block:
                            context = (context + "\n\n" if context else "") + "Recent web knowledge (summaries; verify if critical):\n" + web_block
        except Exception:
            pass

        try:
            # Detect self-updates
            text_lower = text.lower()
            is_self_update = any(keyword in text_lower for keyword in [
                "update", "modify", "improve", "edit", "change", "add", "create", "make", "build"
            ]) and any(keyword in text_lower for keyword in [
                "file", "module", "component", "code", "system", "bot", "jarvis"
            ])

            capabilities = ["open_url", "search", "calculate", "news", "mode_switch"]

            # allow LLM to output MCP tool actions when self update requested
            if is_self_update:
                capabilities.append("self_update")
                capabilities.append("self_add")
                capabilities.append("mcp_tools")

            # Generate LLM response
            response = await self.llm.generate_response(
                text, context=context, mode=mode, capabilities=capabilities
            )

            # If LLM returned actions, run MCP tools (if available)
            actions = response.get("actions", [])
            tool_results = []

            for action in actions:
                if action.get("type") == "mcp_tool":
                    # Cloud deployments must not perform server-side tool execution.
                    if os.getenv("JARVIS_CLOUD_MODE", "false").lower() in ("1", "true", "yes", "y"):
                        tool_results.append({
                            "tool": action.get("tool"),
                            "args": action.get("args", {}),
                            "result": "[MCP] Disabled in cloud mode."
                        })
                        continue
                    tool_name = action.get("tool")
                    args = action.get("args", {})

                    result = await self.run_mcp_tool(tool_name, args)
                    tool_results.append({
                        "tool": tool_name,
                        "args": args,
                        "result": result
                    })

            # -------------------------
            # LEARNING: save good interactions into buffer
            # -------------------------
            # Heuristic: if assistant provided a substantive reply (length) or executed actions, save it
            try:
                assistant_text = response.get("text", "")
                if assistant_text and (len(assistant_text.strip()) > 30 or actions):
                    self.save_interaction_for_learning(text, assistant_text, actions)
            except Exception as e:
                print(f"[LEARN SAVE ERR] {e}")

            # Save to memory
            self.memory.append({"role": "user", "text": text})
            self.memory.append({"role": "assistant", "text": response["text"]})

            # Auto memory trim
            if len(self.memory) > 50:
                self.memory.pop(0)

            # Log chat
            task_manager.save_wakeup_context(text, response["text"], actions)

            db.save_chat(
                user_input=text,
                bot_response=response["text"],
                session_id=session_id,
                intent="auto",
                context={"actions": actions, "mode": mode}
            )

            # Return reply and tool results
            return {
                "text": response["text"],
                "actions": actions,
                "tool_results": tool_results,
                "mode": mode,
                "source": response.get("source", "openai")
            }

        except Exception as e:
            err_msg = f"Error in reasoning: {e}"
            print(err_msg)
            db.save_system_event("brain_error", err_msg, "error")
            return {"text": "I'm having a processing issue, sir.", "actions": [], "mode": mode}
