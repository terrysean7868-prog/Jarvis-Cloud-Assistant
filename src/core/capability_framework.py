from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


@dataclass
class ModuleDecision:
    module: str
    confidence: float
    text: str
    actions: List[JsonDict]
    clarification: Optional[JsonDict] = None

    def to_response(self) -> JsonDict:
        out: JsonDict = {
            "text": self.text,
            "actions": self.actions,
            "source": "capability-framework",
            "module": self.module,
            "intent": "direct_action" if self.actions else "clarification",
            "intent_type": "direct_action" if self.actions else "ambiguous",
            "intent_depth": "medium",
            "response_strategy": "immediate_execution" if self.actions else "ask_followup",
            "proactive_followup_added": False,
            "user_preference_influenced": False,
        }
        if isinstance(self.clarification, dict):
            out["clarification"] = self.clarification
        return out


class CapabilityFramework:
    """Deterministic capability router layered on top of existing architecture.

    Goal: keep model work simple by routing clear requests to focused modules.
    """

    def route_request(self, text: str, *, cloud_mode: bool) -> Optional[JsonDict]:
        t = (text or "").strip()
        if not t:
            return None

        # Expanded deterministic routing chain. Order matters — more specific/primary intents first.
        decision = (
            self._os_browser_basics_module(t)
            or self._model_ops_module(t)  # model_ops/finetune/training checked early (higher intent priority)
            or self._code_module(t)  # code execution/generation checked early
            or self._connectors_module(t)
            or self._automation_module(t)
            or self._file_ops_module(t, cloud_mode=cloud_mode)
            or self._dataset_module(t)  # dataset collection checked after training/code (lower priority)
            or self._multimedia_module(t)
            or self._searching_module(t)
            or self._research_module(t)
            or self._analysis_module(t)
            or self._kb_module(t)
            or self._translate_summarize_module(t)
            or self._package_module(t)
            or self._pc_tasks_module(t)
        )
        if decision is None or decision.confidence < 0.75:
            return None
        return decision.to_response()

    @staticmethod
    def _dataset_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        wants_dataset = bool(
            re.search(r"\b(dataset|datasets|data\s*collection|collect\s*data|scrape\s*data|ingest\s*data|training\s*data)\b", tl)
            or "huggingface" in tl
            or "kaggle" in tl
        )
        if not wants_dataset:
            return None

        topic_match = re.search(r"\b(?:for|about|on)\b\s+(.+)$", tl)
        topic = (topic_match.group(1).strip() if topic_match else "")
        if not topic or len(topic.split()) < 2:
            q = "What dataset topic should I collect, and from which source (HuggingFace/Kaggle/GitHub)?"
            return ModuleDecision(
                module="dataset_ingest",
                confidence=0.92,
                text=q,
                actions=[],
                clarification={
                    "kind": "dataset_ingest",
                    "question": q,
                    "original_user_text": text,
                },
            )

        sources = [
            "https://huggingface.co/datasets",
            "https://www.kaggle.com/datasets",
            "https://github.com/topics/dataset",
        ]
        return ModuleDecision(
            module="dataset_ingest",
            confidence=0.95,
            text=f"Collecting dataset references for '{topic}' and storing summaries to the database.",
            actions=[
                {
                    "type": "collect_dataset",
                    "query": topic,
                    "sources": sources,
                    "max_sources": 3,
                    "max_items": 12,
                }
            ],
        )

    @staticmethod
    def _os_browser_basics_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()

        # Browser basics
        m_browser = re.search(r"\bopen\s+(https?://\S+|www\.\S+|[a-z0-9-]+\.(?:com|org|net|io|dev))\b", tl)
        if m_browser:
            raw = m_browser.group(1).strip()
            url = raw if raw.startswith("http") else f"https://{raw}"
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.9,
                text=f"Opening {url}.",
                actions=[{"type": "open_url", "url": url}],
            )

        # System settings via device_action wrappers
        if re.search(r"\b(wifi|wi-fi)\b", tl) and re.search(r"\b(turn on|enable|on|turn off|disable|off)\b", tl):
            enabled = bool(re.search(r"\b(turn on|enable|on)\b", tl)) and not bool(re.search(r"\b(turn off|disable|off)\b", tl))
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.86,
                text=("Turning Wi-Fi on." if enabled else "Turning Wi-Fi off."),
                actions=[{"type": "device_action", "name": "set_wifi", "args": {"enabled": enabled}}],
            )

        if re.search(r"\bbluetooth\b", tl) and re.search(r"\b(turn on|enable|on|turn off|disable|off)\b", tl):
            enabled = bool(re.search(r"\b(turn on|enable|on)\b", tl)) and not bool(re.search(r"\b(turn off|disable|off)\b", tl))
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.86,
                text=("Turning Bluetooth on." if enabled else "Turning Bluetooth off."),
                actions=[{"type": "device_action", "name": "set_bluetooth", "args": {"enabled": enabled}}],
            )

        m_volume = re.search(r"\b(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\b", tl)
        if m_volume:
            value = max(0, min(100, int(m_volume.group(1))))
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.9,
                text=f"Setting volume to {value}%.",
                actions=[{"type": "device_action", "name": "set_volume", "args": {"value": value}}],
            )

        m_brightness = re.search(r"\b(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})\b", tl)
        if m_brightness:
            value = max(0, min(100, int(m_brightness.group(1))))
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.9,
                text=f"Setting brightness to {value}%.",
                actions=[{"type": "device_action", "name": "set_brightness", "args": {"value": value}}],
            )

        if re.search(r"\b(mute|unmute)\b", tl):
            muted = bool(re.search(r"\bmute\b", tl)) and not bool(re.search(r"\bunmute\b", tl))
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.84,
                text=("Muting audio." if muted else "Unmuting audio."),
                actions=[{"type": "device_action", "name": "set_mute", "args": {"muted": muted}}],
            )

        if re.search(r"\b(screenshot|capture screen|screen capture)\b", tl):
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.86,
                text="Capturing a screenshot.",
                actions=[{"type": "capture_screen"}],
            )

        return None

    # --- Additional deterministic modules added for broader assistant coverage ---
    @staticmethod
    def _connectors_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(slack|discord|teams|outlook|gmail|calendar|trello|notion)\b", tl):
            q = "Which connector and account should I use, and what should I do (send/read/draft)?"
            if re.search(r"\b(send|draft|read|schedule|create|update)\b", tl):
                return ModuleDecision(
                    module="connectors",
                    confidence=0.88,
                    text="Routing to connector handler.",
                    actions=[{"type": "connector_action", "text": text}],
                )
            return ModuleDecision(module="connectors", confidence=0.82, text=q, actions=[], clarification={"kind": "connectors", "question": q, "original_user_text": text})
        return None

    @staticmethod
    def _multimedia_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(image|photo|picture|audio|recording|transcribe|transcription|video|mp3|wav|convert to text)\b", tl):
            if re.search(r"\btranscrib|subtitle|caption\b", tl):
                return ModuleDecision(module="multimedia", confidence=0.86, text="Handling multimedia: transcribe/translate/caption.", actions=[{"type": "process_multimedia", "text": text}])
            return ModuleDecision(module="multimedia", confidence=0.82, text="Handling multimedia task.", actions=[{"type": "process_multimedia", "text": text}])
        return None

    @staticmethod
    def _automation_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(automate|script|workflow|rpa|run workflow|run automation)\b", tl):
            return ModuleDecision(module="automation", confidence=0.86, text="This looks like an automation/workflow request.", actions=[{"type": "n8n_webhook", "path": "run-workflow", "payload": {"trigger_query": text}}])
        return None

    @staticmethod
    def _code_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(run code|execute code|test code|unit test|debug|lint|format)\b", tl) or re.search(r"\bpython script|node script|npm run\b", tl):
            return ModuleDecision(module="code_execution", confidence=0.9, text="Code execution request detected.", actions=[{"type": "run_code", "command": text}])
        if re.search(r"\b(refactor|generate code|implement function|create module)\b", tl):
            return ModuleDecision(module="code_generation", confidence=0.88, text="Code generation/refactor request.", actions=[{"type": "create_code", "prompt": text}])
        return None

    @staticmethod
    def _model_ops_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(train model|fine-?tune|evaluate model|export model|deploy model|retrain)\b", tl):
            return ModuleDecision(module="model_ops", confidence=0.9, text="Model operations requested.", actions=[{"type": "model_ops", "text": text}])
        return None

    @staticmethod
    def _kb_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(knowledge base|kb|lookup in kb|faq|help center|documentation)\b", tl):
            return ModuleDecision(module="knowledge_base", confidence=0.85, text="Looking up knowledge base.", actions=[{"type": "kb_lookup", "query": text}])
        return None

    @staticmethod
    def _translate_summarize_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(summariz|tl;dr|shorten|summarise|abstract)\b", tl):
            return ModuleDecision(module="summarization", confidence=0.88, text="Summarization requested.", actions=[{"type": "summarize_text", "text": text}])
        if re.search(r"\b(translate|translate to|in spanish|in french|traduce|übersetzen)\b", tl):
            return ModuleDecision(module="translation", confidence=0.88, text="Translation requested.", actions=[{"type": "translate_text", "text": text}])
        return None

    @staticmethod
    def _package_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if re.search(r"\b(install package|pip install|npm install|update package|upgrade dependency)\b", tl):
            return ModuleDecision(module="package_manager", confidence=0.85, text="Package management action.", actions=[{"type": "package_action", "text": text}])
        return None

    @staticmethod
    def _file_ops_module(text: str, *, cloud_mode: bool) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if not re.search(r"\b(file|files|folder|directory|path|codebase)\b", tl):
            return None

        if re.search(r"\b(find|search|locate)\b", tl):
            m = re.search(r"\b(?:find|search|locate)\b(?:\s+for)?\s+(.+?)(?:\s+in\s+.+)?$", tl)
            query = (m.group(1).strip() if m else "")
            if not query or query in {"file", "files", "folder"}:
                q = "What file name, symbol, or keyword should I look for in the project?"
                return ModuleDecision(
                    module="file_ops",
                    confidence=0.9,
                    text=q,
                    actions=[],
                    clarification={
                        "kind": "file_ops",
                        "question": q,
                        "original_user_text": text,
                    },
                )
            return ModuleDecision(
                module="file_ops",
                confidence=0.9,
                text=f"Searching project files for '{query}'.",
                actions=[{"type": "find_files", "query": query, "path": "src", "in_content": True, "max_results": 30}],
            )

        m_list = re.search(r"\b(list|show)\b\s+(?:all\s+)?(?:files|folders|directories)\b(?:\s+in\s+(.+))?", tl)
        if m_list:
            target = (m_list.group(2).strip() if m_list.group(2) else "src")
            return ModuleDecision(
                module="file_ops",
                confidence=0.84,
                text=f"Listing items in {target}.",
                actions=[{"type": "list", "path": target}],
            )

        m_read = re.search(r"\b(read|show|open)\b\s+file\s+(.+)$", tl)
        if m_read:
            target = m_read.group(2).strip()
            return ModuleDecision(
                module="file_ops",
                confidence=0.84,
                text=f"Reading {target}.",
                actions=[{"type": "read", "path": target}],
            )

        if cloud_mode and re.search(r"\b(write|edit|delete|move|rename)\b", tl):
            return ModuleDecision(
                module="file_ops",
                confidence=0.9,
                text="I can inspect files in cloud mode, but direct file modification is restricted there.",
                actions=[],
            )

        return None

    @staticmethod
    def _searching_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        m = re.search(r"\b(?:search|look up|lookup|find on web|google|duckduckgo|bing)\b\s+(.+)$", tl)
        if not m:
            return None
        query = (m.group(1) or "").strip(" .!?")
        if not query:
            return None
        return ModuleDecision(
            module="searching",
            confidence=0.86,
            text=f"Searching the web for '{query}'.",
            actions=[{"type": "web_search", "query": query}],
        )

    @staticmethod
    def _research_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        if not re.search(r"\b(research|deep research|in-depth|with sources|citations?|latest|current|news)\b", tl):
            return None
        topic = re.sub(r"\b(research|deep research|in-depth|with sources|citations?)\b", "", tl).strip(" .:-")
        if len(topic) < 4:
            q = "What specific topic should I research?"
            return ModuleDecision(
                module="research",
                confidence=0.88,
                text=q,
                actions=[],
                clarification={"kind": "research", "question": q, "original_user_text": text},
            )
        return ModuleDecision(
            module="research",
            confidence=0.88,
            text=f"Starting research for '{topic}'. I will gather sources and summarize findings.",
            actions=[
                {"type": "web_search", "query": topic},
                {"type": "collect_dataset", "query": topic, "sources": ["https://huggingface.co/datasets"], "max_sources": 1, "max_items": 6},
            ],
        )

    @staticmethod
    def _analysis_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()
        wants_analysis = bool(re.search(r"\b(analyze|analysis|compare|tradeoff|evaluate|assessment)\b", tl))
        if not wants_analysis:
            return None
        if len(re.findall(r"[a-z0-9]+", tl)) < 5:
            q = "What exactly should I analyze, and what output format do you want?"
            return ModuleDecision(
                module="analysis",
                confidence=0.84,
                text=q,
                actions=[],
                clarification={"kind": "analysis", "question": q, "original_user_text": text},
            )
        return ModuleDecision(
            module="analysis",
            confidence=0.8,
            text="I can analyze this. I will gather quick evidence first for a grounded summary.",
            actions=[{"type": "web_search", "query": text.strip()}],
        )

    @staticmethod
    def _pc_tasks_module(text: str) -> Optional[ModuleDecision]:
        tl = text.lower().strip()

        m_open = re.search(r"\bopen\s+([a-z0-9 _.-]{2,40}?)(?:\s+and\s+(?:write|type|draft|compose)\b|$)", tl)
        if m_open:
            app = m_open.group(1).strip(" .")
            actions: List[JsonDict] = [{"type": "open_app", "app_name": app}]
            m_write = re.search(r"\band\s+(?:write|type|draft|compose)\b\s+(.+)$", text, flags=re.IGNORECASE)
            if not m_write:
                m_write = re.search(r"\b(?:write|type|draft|compose)\b\s+(.+)$", text, flags=re.IGNORECASE)
            if m_write:
                body = (m_write.group(1) or "").strip()
                if body:
                    actions.append({"type": "type_text", "text": body, "interval": 0.04, "before_ms": 900})
            return ModuleDecision(module="pc_tasks", confidence=0.82, text=f"Opening {app}.", actions=actions)

        m_close = re.search(r"\bclose\s+([a-z0-9 _.-]{2,40})", tl)
        if m_close:
            app = m_close.group(1).strip()
            return ModuleDecision(module="pc_tasks", confidence=0.82, text=f"Closing {app}.", actions=[{"type": "close_app", "app_name": app}])

        m_switch = re.search(r"\b(?:switch to|focus)\s+([a-z0-9 _.-]{2,40})", tl)
        if m_switch:
            app = m_switch.group(1).strip()
            return ModuleDecision(module="pc_tasks", confidence=0.82, text=f"Switching to {app}.", actions=[{"type": "switch_app", "app_name": app}])

        if re.search(r"\b(press|hit)\s+enter\b", tl):
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.8,
                text="Pressing Enter.",
                actions=[{"type": "press_key", "key": "enter", "presses": 1}],
            )

        if re.search(r"\b(scroll\s+down)\b", tl):
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.8,
                text="Scrolling down.",
                actions=[{"type": "screen_navigation", "command": "scroll", "clicks": -5}],
            )

        if re.search(r"\b(scroll\s+up)\b", tl):
            return ModuleDecision(
                module="pc_tasks",
                confidence=0.8,
                text="Scrolling up.",
                actions=[{"type": "screen_navigation", "command": "scroll", "clicks": 5}],
            )

        return None
