from __future__ import annotations

import os
import re
import ipaddress
import urllib.parse
import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from src.config import runtime_defaults as rd


JsonDict = Dict[str, Any]


@dataclass
class ChatOrchestrator:
    """Orchestrates the main chat pipeline without adding new HTTP routes.

    This keeps `app.py` thin and makes the system easier to scale:
    - Brain decides response + proposed actions
    - Policy layer filters actions (cloud/local, admin-only, screen-capture guard)
    - Tool layer executes immediate web actions inline (2-pass web pipeline)
    - Deferred actions run in background tasks

    The orchestrator is intentionally dependency-injected via callables to avoid
    circular imports with `app.py`.
    """

    brain: Any
    executor: Any

    cloud_mode: bool
    admin_only_action_types: set[str]

    # Policy callbacks
    user_explicitly_requested_screen_capture: Callable[[str], bool]
    can_control_device: Callable[[dict], bool]
    is_remote_device_action: Callable[[dict], bool]

    # Web continuation callbacks
    build_web_context_from_action_results: Callable[[List[dict]], str]
    persist_web_context_items: Callable[..., None]
    web_lookup_found: Callable[[List[dict]], bool]
    continue_user_using_web_context: Callable[[str, str, str, bool], Awaitable[Optional[JsonDict]]]

    # Optional helpers to preserve app.py behavior parity
    fallback_answer_from_web_results: Optional[Callable[[str, List[dict], bool], str]] = None
    user_explicitly_requested_research_open: Optional[Callable[[str], bool]] = None
    pick_best_source_url: Optional[Callable[[List[dict]], Optional[str]]] = None
    notify: Optional[Callable[[str, JsonDict], Awaitable[None]]] = None

    def _is_research_request(self, text: str) -> bool:
        tl = (text or "").strip().lower()
        if not tl:
            return False

        # If the user is asking whether research is complete, treat it as a status question,
        # not a new research request.
        if re.search(
            r"\b(?:did|do|have|has|are|is)\s+(?:you\s+)?(?:already\s+)?(?:complete|completed|finish|finished|done)\s+(?:the\s+)?research\b",
            tl,
        ) or re.search(r"\bresearch\s+(?:status|progress|update)\b", tl):
            return False

        return bool(
            re.search(
                r"\b(research|do\s+research|make\s+research|deep\s+research|in-?depth|detailed\s+research|"
                r"with\s+sources|with\s+citations|with\s+links|sources?|citations?|cite|links?|"
                r"analyze|analysis|summarize|summary|compare)\b",
                tl,
            )
        )

    @staticmethod
    def _extract_research_topic_label(text: str) -> str:
        """Extract a compact 'topic' label from a research request for UX/logging."""
        raw = (text or "").strip()
        if not raw:
            return ""
        # Prefer 'research about X' or similar.
        m = re.search(
            r"(?i)\b(?:do\s+research|make\s+research|perform\s+research|deep\s+research|research)\b\s*(?:about|on|regarding)?\s*(.+)$",
            raw,
        )
        if m:
            topic = (m.group(1) or "").strip().strip(" .-\t")
            if topic:
                return topic[:180]
        # Fallback: trim overly long prose.
        return raw[:180]

    def _research_depth(self, text: str) -> str:
        tl = (text or "").strip().lower()
        if not tl:
            return "quick"
        if re.search(r"\b(deep\s+research|in-?depth|detailed\s+research|full\s+research|thorough)\b", tl):
            return "deep"
        # Default: quick research unless explicitly requesting research (then go deeper)
        if "research" in tl:
            return "deep"
        return "quick"

    @staticmethod
    def _pick_fetch_urls_from_search_results(action_results: List[dict], *, max_urls: int) -> List[str]:
        prefer = (
            "wikipedia.org",
            "docs.",
            "developer.",
            "github.com",
            "nodejs.org",
            "python.org",
            "openai.com",
            "microsoft.com",
            "mozilla.org",
        )

        urls: List[str] = []
        for r in action_results or []:
            if not isinstance(r, dict):
                continue
            if (r.get("status") or "").lower() != "success":
                continue
            action = (r.get("action") or r.get("action_type") or "").lower()
            if action not in {"web_search", "search"}:
                continue
            for item in (r.get("results") or [])[:8]:
                if not isinstance(item, dict):
                    continue
                u = str(item.get("url") or "").strip()
                if u:
                    urls.append(u)

        # De-dupe
        seen = set()
        uniq: List[str] = []
        for u in urls:
            lu = u.lower()
            if lu in seen:
                continue
            seen.add(lu)
            uniq.append(u)

        if not uniq:
            return []

        ranked: List[str] = []
        for p in prefer:
            for u in uniq:
                if p in u.lower() and u not in ranked:
                    ranked.append(u)
                    if len(ranked) >= max_urls:
                        return ranked
        for u in uniq:
            if u not in ranked:
                ranked.append(u)
                if len(ranked) >= max_urls:
                    break
        return ranked

    async def _run_research_job(
        self,
        *,
        task_id: str,
        user_text: str,
        mode: str,
        acting_user: str,
        immediate_actions: List[dict],
    ) -> None:
        """Background research job: web_search -> optional fetch_url -> synthesize."""
        try:
            from src.utils.task_manager import task_manager, TaskStatus
        except Exception:
            task_manager = None
            TaskStatus = None

        def _cancel_requested() -> bool:
            try:
                return bool(task_manager and getattr(task_manager, "is_cancel_requested", None) and task_manager.is_cancel_requested(task_id))
            except Exception:
                return False

        async def _publish_cancelled() -> None:
            try:
                if self.notify is not None:
                    await self.notify(
                        (acting_user or ""),
                        {
                            "type": "research_cancelled",
                            "task_id": task_id,
                            "topic": user_text,
                        },
                    )
            except Exception:
                pass

        def _mark_cancelled() -> None:
            if task_manager and TaskStatus:
                try:
                    task_manager.update_task(
                        task_id,
                        status=TaskStatus.STOPPED.value,
                        append_result={"type": "research_cancelled", "topic": user_text},
                        meta_update={"notified": False},
                    )
                except Exception:
                    pass

        try:
            if task_manager and TaskStatus:
                task_manager.update_task(task_id, status=TaskStatus.IN_PROGRESS.value)
        except Exception:
            pass

        if _cancel_requested():
            _mark_cancelled()
            await _publish_cancelled()
            return

        response: JsonDict = {"text": "", "actions": []}
        try:
            tool_results = await self.executor.process_actions(immediate_actions, acting_user)

            if _cancel_requested():
                _mark_cancelled()
                await _publish_cancelled()
                return

            # Deep research: fetch top sources for richer context.
            depth = self._research_depth(user_text)
            if depth == "deep":
                max_fetch = int(rd.RESEARCH_FETCH_URLS_DEEP)
                max_fetch = max(0, min(max_fetch, 3))

                if max_fetch:
                    urls = self._pick_fetch_urls_from_search_results(tool_results, max_urls=max_fetch)
                    if urls:
                        if _cancel_requested():
                            _mark_cancelled()
                            await _publish_cancelled()
                            return
                        more = await self.executor.process_actions(
                            [{"type": "fetch_url", "url": u} for u in urls],
                            acting_user,
                        )
                        if isinstance(more, list) and more:
                            tool_results.extend(more)

                        if _cancel_requested():
                            _mark_cancelled()
                            await _publish_cancelled()
                            return

            web_ctx = self.build_web_context_from_action_results(tool_results)
            found = self.web_lookup_found(tool_results)

            if _cancel_requested():
                _mark_cancelled()
                await _publish_cancelled()
                return

            offline_analysis = bool(rd.OFFLINE_ANALYSIS)
            offline_only = bool(rd.OFFLINE_ONLY)

            if (offline_only or offline_analysis) and self.fallback_answer_from_web_results is not None:
                response["text"] = self.fallback_answer_from_web_results(user_text, tool_results, found)
                response["actions"] = []
            else:
                continued = await self.continue_user_using_web_context(user_text, web_ctx, mode=str(mode or "chat"), found=found)
                if continued:
                    response["text"] = (continued.get("text") or "").strip()
                    response["actions"] = continued.get("actions") or []
                elif self.fallback_answer_from_web_results is not None:
                    response["text"] = self.fallback_answer_from_web_results(user_text, tool_results, found)
                    response["actions"] = []

            # Clean up any boilerplate/meta/provider-failure text before emitting a summary.
            try:
                response["text"] = self._clean_research_summary_text(response.get("text") or "")
            except Exception:
                pass

            # If the model returns a trivial/low-quality summary, use the grounded offline synthesis.
            try:
                if (
                    found
                    and self.fallback_answer_from_web_results is not None
                    and self._is_low_quality_research_answer(response.get("text") or "")
                ):
                    response["text"] = self.fallback_answer_from_web_results(user_text, tool_results, found)
                    response["actions"] = []
            except Exception:
                pass

            # Optionally add open_url after research if requested.
            try:
                if (
                    found
                    and self.user_explicitly_requested_research_open is not None
                    and self.pick_best_source_url is not None
                    and self.user_explicitly_requested_research_open(user_text)
                ):
                    best = self.pick_best_source_url(tool_results)
                    if best and not any(isinstance(a, dict) and a.get("type") == "open_url" for a in (response.get("actions") or [])):
                        response["actions"] = list(response.get("actions") or []) + [{"type": "open_url", "url": best}]
            except Exception:
                pass

            summary_text = (response.get("text") or "").strip()
            if not summary_text:
                summary_text = "Research completed, but I couldn't synthesize a final summary."

            # Realtime notification (best-effort)
            try:
                if self.notify is not None:
                    await self.notify(
                        (acting_user or ""),
                        {
                            "type": "research_complete",
                            "task_id": task_id,
                            "topic": user_text,
                            "summary": summary_text,
                        },
                    )
            except Exception:
                pass

            if task_manager and TaskStatus:
                task_manager.update_task(
                    task_id,
                    status=TaskStatus.COMPLETED.value,
                    append_result={
                        "type": "research_result",
                        "topic": user_text,
                        "summary": summary_text,
                        "when": "done",
                    },
                    meta_update={"notified": False},
                )
        except Exception as e:
            if task_manager and TaskStatus:
                try:
                    task_manager.update_task(
                        task_id,
                        status=TaskStatus.FAILED.value,
                        append_result={"type": "research_error", "topic": user_text, "error": str(e)},
                        meta_update={"notified": False},
                    )
                except Exception:
                    pass

            try:
                if self.notify is not None:
                    await self.notify(
                        (acting_user or ""),
                        {
                            "type": "research_failed",
                            "task_id": task_id,
                            "topic": user_text,
                            "error": str(e),
                        },
                    )
            except Exception:
                pass

    async def _schedule_async_research_if_needed(
        self,
        *,
        user_text: str,
        mode: str,
        acting_user: str,
        actions: List[dict],
        background_tasks: Any,
        user_id: Optional[str],
    ) -> Optional[Tuple[JsonDict, List[dict]]]:
        """If this looks like a research request, start it in background and return an immediate ack."""
        immediate_actions, deferred_actions = self._split_immediate_actions(actions)
        if not immediate_actions:
            return None

        wants_async = bool(rd.RESEARCH_ASYNC_DEFAULT)

        if not wants_async:
            return None

        # Only async when user explicitly asked for research/analysis.
        if not self._is_research_request(user_text):
            return None

        try:
            from src.utils.task_manager import task_manager, TaskStatus
        except Exception:
            task_manager = None
            TaskStatus = None

        # Create a task entry for progress + later retrieval.
        task_id = None
        if task_manager and TaskStatus:
            try:
                topic_label = self._extract_research_topic_label(user_text) or user_text
                task_id = task_manager.create_task(
                    description=f"Research: {topic_label[:120]}",
                    steps=[{"action": "web_research", "description": "Run web_search + analysis", "params": {}}],
                    priority=5,
                    meta={
                        "task_type": "research",
                        "user_id": user_id or acting_user,
                        "topic": topic_label,
                        "notified": False,
                    },
                )
                task_manager.update_task(task_id, status=TaskStatus.IN_PROGRESS.value)
            except Exception:
                task_id = None

        # Kick off background research.
        try:
            if task_id:
                background_tasks.add_task(
                    self._run_research_job,
                    task_id=task_id,
                    user_text=user_text,
                    mode=mode,
                    acting_user=acting_user,
                    immediate_actions=immediate_actions,
                )
        except Exception:
            # If we can't schedule, fall back to inline behavior.
            return None

        ack: JsonDict = {
            "text": (
                f"Started research on: {user_text}.\n\n"
                f"You can continue with other tasks. I’ll share the research summary once it completes."
                + (f"\n\n(Task id: {task_id})" if task_id else "")
            ),
            "actions": [],
            "mode": mode,
            "source": "research-async",
        }

        # Execute any non-web actions normally (if present).
        if deferred_actions and not self.cloud_mode:
            try:
                background_tasks.add_task(self.executor.process_actions, deferred_actions, (acting_user or "user"))
            except Exception:
                pass

        return ack, []

    def _extract_http_urls(self, text: str, *, max_urls: int) -> List[str]:
        t = (text or "").strip()
        if not t:
            return []
        # Conservative URL extraction: http(s) only.
        # Stop at whitespace or common closing delimiters.
        urls = re.findall(r"https?://[^\s)\]]+", t, flags=re.IGNORECASE)
        if not urls:
            return []
        # De-dupe while preserving order
        seen = set()
        out: List[str] = []
        def _is_safe_public_host(url: str) -> bool:
            try:
                parsed = urllib.parse.urlparse(url)
                host = (parsed.hostname or "").strip().lower()
                if not host:
                    return False

                # Block obvious local targets.
                if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
                    return False
                if host.endswith(".local"):
                    return False

                # Block raw IPs that are private/link-local/loopback/etc.
                try:
                    ip = ipaddress.ip_address(host)
                    if (
                        ip.is_private
                        or ip.is_loopback
                        or ip.is_link_local
                        or ip.is_multicast
                        or ip.is_unspecified
                        or ip.is_reserved
                    ):
                        return False
                except ValueError:
                    # Not an IP literal; allow (DNS resolution safety should be handled downstream).
                    pass

                return True
            except Exception:
                return False

        for u in urls:
            u = (u or "").strip().rstrip(".,;\"'\")")
            if not u:
                continue
            if not _is_safe_public_host(u):
                continue
            lu = u.lower()
            if lu in seen:
                continue
            seen.add(lu)
            out.append(u)
            if len(out) >= max_urls:
                break
        return out

    def _should_auto_fetch_links(self, text: str) -> bool:
        """Whether to auto-fetch links contained in the user's message.

        Controlled via:
        - JARVIS_AUTO_FETCH_LINKS: enables generic link auto-fetch
        - JARVIS_AUTO_FETCH_LINKS_MODE: 'on_request' (default) or 'always'
        - JARVIS_AUTO_FETCH_WIKIPEDIA_LINKS: legacy toggle for wiki-only
        """
        t = (text or "").strip().lower()
        if not t:
            return False

        # Default behavior: enabled (no env required).
        mode = str(rd.AUTO_FETCH_LINKS_MODE).strip().lower()
        if mode == "always":
            return True

        # Default: only when user asked to analyze/summarize/read a link.
        triggers = (
            "summarize",
            "summary",
            "analyze",
            "analysis",
            "explain",
            "read this",
            "read the link",
            "from this link",
            "from this url",
            "use this link",
            "research",
            "check this",
        )
        return any(k in t for k in triggers)

    def _split_immediate_actions(self, actions: List[dict]) -> Tuple[List[dict], List[dict]]:
        immediate_types = {"web_search", "fetch_url", "search"}
        immediate_actions = [a for a in actions if (a or {}).get("type") in immediate_types]
        deferred_actions = [a for a in actions if (a or {}).get("type") not in immediate_types]
        return immediate_actions, deferred_actions

    @staticmethod
    def _inline_non_web_action_types() -> set[str]:
        raw = str(getattr(rd, "INLINE_NON_WEB_ACTION_TYPES_CSV", "") or "").strip()
        if not raw:
            return set()
        out: set[str] = set()
        for part in raw.split(","):
            p = (part or "").strip().lower()
            if p:
                out.add(p)
        return out

    @staticmethod
    def _summarize_inline_action_results(results: List[dict]) -> str:
        if not isinstance(results, list) or not results:
            return ""

        success_statuses = {
            "success",
            "opened",
            "written",
            "edited",
            "deleted",
            "copied",
        }
        fail_statuses = {"error", "forbidden", "approval_required", "unknown_action"}

        ok = 0
        fail = 0
        other = 0
        lines: List[str] = []

        for item in results[:6]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            action_name = str(item.get("action_type") or item.get("action") or "action").strip() or "action"

            if status in success_statuses:
                ok += 1
            elif status in fail_statuses:
                fail += 1
            else:
                other += 1

            detail = (
                str(item.get("message") or "").strip()
                or str(item.get("error") or "").strip()
                or str(item.get("path") or "").strip()
                or str(item.get("query") or "").strip()
                or str(item.get("url") or "").strip()
            )

            if status:
                if detail:
                    lines.append(f"- {action_name}: {status} ({detail})")
                else:
                    lines.append(f"- {action_name}: {status}")

        headline = f"Execution update: {ok} succeeded"
        if fail:
            headline += f", {fail} failed"
        if other:
            headline += f", {other} with other status"
        headline += "."

        preview = "\n".join(lines[:3])
        return (headline + ("\n" + preview if preview else "")).strip()

    async def _run_inline_non_web_actions(
        self,
        *,
        response: JsonDict,
        actions: List[dict],
        acting_user: str,
    ) -> List[dict]:
        if not actions:
            return actions

        inline_types = self._inline_non_web_action_types()
        if not inline_types:
            return actions

        inline_actions = [a for a in actions if str((a or {}).get("type") or "").strip().lower() in inline_types]
        remaining_actions = [a for a in actions if str((a or {}).get("type") or "").strip().lower() not in inline_types]

        if not inline_actions:
            return actions

        try:
            results = await self.executor.process_actions(inline_actions, acting_user)

            if bool(rd.RETURN_ACTION_RESULTS):
                current = response.get("action_results")
                if isinstance(current, list):
                    current.extend(results if isinstance(results, list) else [])
                    response["action_results"] = current
                else:
                    response["action_results"] = results

            if bool(getattr(rd, "APPEND_INLINE_ACTION_SUMMARY", True)):
                summary = self._summarize_inline_action_results(results if isinstance(results, list) else [])
                if summary:
                    base = (response.get("text") or "").strip()
                    response["text"] = (base + "\n\n" + summary).strip() if base else summary

            response["actions"] = remaining_actions
            return remaining_actions
        except Exception as e:
            base = (response.get("text") or "").strip()
            note = f"(Inline action execution failed: {e})"
            response["text"] = (base + "\n\n" + note).strip() if base else note
            return actions

    def _filter_capture_screen(self, user_text: str, actions: List[dict]) -> List[dict]:
        if not actions:
            return actions
        if self.user_explicitly_requested_screen_capture(user_text):
            return actions
        return [a for a in actions if (a or {}).get("type") not in ("capture_screen",)]

    def _enforce_permissions(self, principal: dict, role: str, response: JsonDict, actions: List[dict]) -> List[dict]:
        if not actions:
            return actions

        if self.cloud_mode:
            if not self.can_control_device(principal):
                blocked = [a for a in actions if self.is_remote_device_action(a)]
                allowed = [a for a in actions if not self.is_remote_device_action(a)]
                if blocked:
                    response["text"] = (response.get("text") or "") + "\n\n(Device actions are not permitted for this account.)"
                return allowed
            return actions

        # Local mode: admin-only actions require admin role.
        if (role or "").strip().lower() != "admin":
            blocked = [a for a in actions if (a or {}).get("type") in self.admin_only_action_types]
            allowed = [a for a in actions if (a or {}).get("type") not in self.admin_only_action_types]
            if blocked:
                response["text"] = (response.get("text") or "") + "\n\n(Some actions require admin privileges and were skipped.)"
            return allowed

        return actions

    @staticmethod
    def _is_low_quality_research_answer(text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True

        tl = t.lower()

        # Too short to be useful.
        if len(t) < 80:
            return True

        # Very short generic acknowledgements.
        if re.fullmatch(r"\s*ok(?:ay)?[.!]?\s*", tl):
            return True
        if (tl.startswith("ok") or tl.startswith("sure")) and len(t) < 160:
            return True

        # Boilerplate / meta statements.
        bad_markers = (
            "i used the web context",
            "used the web context",
            "based on the web context",
            "i'm thinking",
            "im thinking",
            "hang tight",
            "working on it",
            # Provider failure fallback should not be presented as a research summary.
            "ai provider unavailable",
            "provider unavailable",
            "couldn't generate a response right now",
            "could not generate a response right now",
            "primary_api_key",
            "openai_api_key",
        )
        if any(m in tl for m in bad_markers):
            return True

        return False

    @staticmethod
    def _clean_research_summary_text(text: str) -> str:
        """Remove known boilerplate that can leak into research summaries."""
        t = (text or "").strip()
        if not t:
            return ""

        # Strip the LLM adapter provider-failure text if it got concatenated.
        marker = "I couldn't generate a response right now"
        idx = t.find(marker)
        if idx >= 0:
            t = t[:idx].strip()

        # Strip common web-context meta disclaimer.
        t = t.replace("(I used the web context, but won't run another web search loop here.)", "").strip()
        t = t.replace("I used the web context, but won't run another web search loop here.", "").strip()
        t = t.replace("I used the web context but won't run another web search loop here.", "").strip()

        return t

    async def _run_inline_web_pipeline(
        self,
        *,
        user_text: str,
        mode: str,
        response: JsonDict,
        actions: List[dict],
        acting_user: str,
    ) -> List[dict]:
        if not actions:
            return actions

        immediate_actions, deferred_actions = self._split_immediate_actions(actions)
        if not immediate_actions:
            return actions

        continued_actions = None
        try:
            tool_results = await self.executor.process_actions(immediate_actions, acting_user)
            if bool(rd.RETURN_ACTION_RESULTS):
                response["action_results"] = tool_results

            found = self.web_lookup_found(tool_results)

            # Optional offline drilldown: for queries that need a specific "current/latest" value,
            # fetch the top primary source page and let the offline engine extract concrete data points.
            try:
                offline_analysis = bool(rd.OFFLINE_ANALYSIS)
                offline_only = bool(rd.OFFLINE_ONLY)

                def _needs_offline_drilldown(prompt: str) -> bool:
                    tl = (prompt or "").strip().lower()
                    if not tl:
                        return False
                    return bool(
                        re.search(r"\b(latest|current|as\s+of\s+today|as\s+of\s+now|today)\b", tl)
                        and re.search(
                            r"\b(version|release|price|rate|market\s+cap|marketcap|cap|value|"
                            r"release\s+date|released\s+on|when\s+was|announced|published|"
                            r"eol|end\s+of\s+life|end\-of\-life|supported\s+until|support\s+ends|"
                            r"compatible|compatibility|requirements?|minimum|supported\s+versions?)\b",
                            tl,
                        )
                    )

                def _pick_best_fetch_url(action_results: List[dict]) -> Optional[str]:
                    prefer = (
                        "nodejs.org",
                        "github.com",
                        "docs.",
                        "developer.",
                        "support.",
                        "learn.",
                        "openai.com",
                        "microsoft.com",
                        "mozilla.org",
                        "python.org",
                        "wikipedia.org",
                        "w3schools.com",
                    )
                    urls: List[str] = []
                    for r in action_results or []:
                        if not isinstance(r, dict):
                            continue
                        if (r.get("status") or "").lower() != "success":
                            continue
                        action = (r.get("action") or r.get("action_type") or "").lower()
                        if action not in {"web_search", "search"}:
                            continue
                        for item in (r.get("results") or [])[:5]:
                            if not isinstance(item, dict):
                                continue
                            u = str(item.get("url") or "").strip()
                            if u:
                                urls.append(u)
                    if not urls:
                        return None
                    for p in prefer:
                        for u in urls:
                            if p in u.lower():
                                return u
                    return urls[0]

                if found and (offline_only or offline_analysis) and _needs_offline_drilldown(user_text):
                    fetch_url = _pick_best_fetch_url(tool_results)
                    if fetch_url:
                        more = await self.executor.process_actions(
                            [{"type": "fetch_url", "url": fetch_url}],
                            acting_user,
                        )
                        if isinstance(more, list) and more:
                            tool_results.extend(more)
                            if bool(rd.RETURN_ACTION_RESULTS):
                                response["action_results"] = tool_results
            except Exception:
                pass

            web_ctx = self.build_web_context_from_action_results(tool_results)
            web_mode = str(rd.WEB_RESULTS_MODE).lower()
            if web_mode in ("append", "both"):
                # Keep response text as-is; UI can render action_results.
                response["text"] = (response.get("text") or "")
            else:
                try:
                    # Persist for future retrieval.
                    self.persist_web_context_items(topic=user_text, action_results=tool_results)
                except Exception:
                    pass

                offline_analysis = bool(rd.OFFLINE_ANALYSIS)
                offline_only = bool(rd.OFFLINE_ONLY)

                # If OpenAI is rate-limited (or intentionally disabled), avoid calling it and
                # synthesize from web results locally.
                if (offline_only or offline_analysis) and self.fallback_answer_from_web_results is not None:
                    response["text"] = self.fallback_answer_from_web_results(user_text, tool_results, found)
                    continued_actions = []
                else:
                    continued = await self.continue_user_using_web_context(user_text, web_ctx, mode=mode, found=found)
                    if continued:
                        response["text"] = (continued.get("text") or response.get("text") or "")
                        continued_actions = continued.get("actions") or []
                    elif self.fallback_answer_from_web_results is not None:
                        response["text"] = self.fallback_answer_from_web_results(user_text, tool_results, found)
                        continued_actions = []

                # Clean up any boilerplate/meta/provider-failure text before evaluating quality.
                try:
                    response["text"] = self._clean_research_summary_text(response.get("text") or "")
                except Exception:
                    pass

                # If LLM produced a low-quality answer, replace it with grounded synthesis.
                try:
                    if (
                        found
                        and self.fallback_answer_from_web_results is not None
                        and self._is_low_quality_research_answer(response.get("text") or "")
                    ):
                        response["text"] = self.fallback_answer_from_web_results(user_text, tool_results, found)
                        continued_actions = []
                except Exception:
                    pass

                # If the user explicitly asked for research + opening the source, add an open_url action
                # AFTER we have web-backed text (2-pass pipeline).
                try:
                    if (
                        found
                        and self.user_explicitly_requested_research_open is not None
                        and self.pick_best_source_url is not None
                        and self.user_explicitly_requested_research_open(user_text)
                    ):
                        best = self.pick_best_source_url(tool_results)
                        if best:
                            if not isinstance(continued_actions, list):
                                continued_actions = []
                            if not any(isinstance(a, dict) and a.get("type") == "open_url" for a in continued_actions):
                                continued_actions = list(continued_actions) + [{"type": "open_url", "url": best}]
                except Exception:
                    pass
        except Exception as e:
            response["text"] = (response.get("text") or "") + f"\n\n(Web lookup failed: {e})"

        actions_out = deferred_actions
        if isinstance(continued_actions, list) and continued_actions:
            actions_out = continued_actions
        response["actions"] = actions_out
        return actions_out

    async def run_chat(
        self,
        *,
        text: str,
        mode: str,
        principal: dict,
        role: str,
        acting_user: str,
        background_tasks: Any,
        user_id: Optional[str] = None,
    ) -> Tuple[JsonDict, List[dict]]:
        """Run the end-to-end chat pipeline and return (response, actions)."""
        response: JsonDict = await self.brain.handle_message(text, mode=mode, user_id=user_id)
        actions: List[dict] = response.get("actions", []) or []

        # If the user provided URLs, optionally fetch them immediately so we can answer with
        # grounded context even if the model didn't request tools.
        try:
            if self._should_auto_fetch_links(text):
                max_urls = int(rd.AUTO_FETCH_LINKS_MAX)
                max_urls = max(1, min(max_urls, 5))

                auto_wiki_only = bool(rd.AUTO_FETCH_WIKIPEDIA_LINKS_ONLY)

                urls = self._extract_http_urls(text, max_urls=max_urls)
                if auto_wiki_only:
                    urls = [u for u in urls if "wikipedia.org/" in u.lower()]

                has_web_action = any(
                    isinstance(a, dict) and (a.get("type") or "").strip().lower() in {"web_search", "fetch_url", "search"}
                    for a in (actions or [])
                )
                if urls and not has_web_action:
                    actions = list(actions) + [{"type": "fetch_url", "url": u} for u in urls]
                    response["actions"] = actions
        except Exception:
            pass

        # Guard against accidental screen capture.
        actions = self._filter_capture_screen(text, actions)
        response["actions"] = actions

        # Permissions layer.
        actions = self._enforce_permissions(principal, role, response, actions)
        response["actions"] = actions

        # Inline execute web actions so user actually sees the answer.
        async_out = await self._schedule_async_research_if_needed(
            user_text=text,
            mode=str(mode or "chat"),
            acting_user=(acting_user or "user"),
            actions=actions,
            background_tasks=background_tasks,
            user_id=user_id,
        )
        if async_out is not None:
            return async_out

        actions = await self._run_inline_web_pipeline(
            user_text=text,
            mode=str(mode or "chat"),
            response=response,
            actions=actions,
            acting_user=(acting_user or "user"),
        )

        # Run selected safe non-web actions inline to improve immediate UX
        # (e.g., create_task/check_errors/generate_email), then defer the rest.
        actions = await self._run_inline_non_web_actions(
            response=response,
            actions=actions,
            acting_user=(acting_user or "user"),
        )

        # Defer remaining actions.
        if actions and not self.cloud_mode:
            try:
                background_tasks.add_task(self.executor.process_actions, actions, (acting_user or "user"))
            except Exception:
                # If background_tasks is unavailable, best-effort ignore.
                pass

        return response, actions
