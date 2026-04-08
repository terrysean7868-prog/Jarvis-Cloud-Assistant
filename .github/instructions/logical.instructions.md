JARVIS LOGIC SPECIFICATION
1. SYSTEM PURPOSE
Jarvis is a multi-surface AI assistant that turns user requests into one of three outcomes: a direct answer, a plan, or an executable action. The system combines conversational reasoning, web research, task tracking, and optional PC-agent control so it can answer questions, research current information, manage tasks, and delegate device-side work when needed. The canonical orchestration lives in chat_orchestrator.py, jarvis_brain.py, llm_adapter.py, and app.py.

2. REQUEST TYPES
direct_action: explicit verbs like open, run, execute, launch, close, switch, screenshot, set, turn on/off, enable, disable, restart, or shutdown; detected by intent heuristics in llm_adapter.py and handled with immediate action planning when allowed.
goal_oriented: user expresses an outcome like learn, improve, fix, achieve, master, become, or roadmap; detected in llm_adapter.py and often converted into a stepwise plan with optional execution.
informational: user asks what, why, how, when, where, which, who, explain, define, overview, research, docs, or tutorial; detected in llm_adapter.py and usually answered directly, with web lookup added when the topic is current, sourced, or research-like.
ambiguous: short, underspecified, or continuation-style requests like do it, this, that, same, continue, or go ahead; detected in llm_adapter.py and usually resolved by clarification state, remembered context, or the previous task thread.
3. INTENT → RESPONSE STRATEGY
immediate execution happens when the request is a clear direct_action, a deterministic fast-path, or a safe device/task action that the backend is allowed to run right away.
explain only happens when the request is informational and no device action is explicitly requested; the model is filtered to remove unsolicited actions and usually returns a concise answer.
ask follow-up happens when the request is ambiguous, missing required device/configuration details, or the brain has a pending clarification state; the next short reply is treated as the answer to the prior question in jarvis_brain.py.
suggest next steps happens for goal_oriented and some informational requests; Jarvis returns a short plan or a practical next step instead of blocking on clarification, especially when the code infers a useful follow-up prompt from the response strategy in llm_adapter.py.
4. EXECUTION DECISION LOGIC
Execution is decided by a layered policy, not by the LLM alone. The assistant first forms an intent, then the orchestrator filters actions against cloud mode, admin-only policy, screen-capture guards, device ownership, and capability requirements in app.py and executor.py.

backend execution: used for safe server-side actions like task creation, email drafting, n8n webhooks, research support, and other non-device operations; in cloud mode these are the only actions the server is allowed to execute directly.
PC agent delegation: used for device-side work such as opening apps, typing, screen navigation, file operations on the user machine, and other local device actions; these are routed through /ws/agent and /api/device/dispatch in app.py.
blocking: used when an action is restricted by cloud mode, missing permission, missing device assignment, missing capability metadata, invalid payload shape, or admin-only policy; blocked actions return a restricted/forbidden/failed-style response rather than being executed.
cloud vs local: cloud mode disables local, file, and device execution on the server and pushes device work to the PC agent; local mode is more permissive and may execute more actions directly through executor.py.
5. DELEGATION FLOW
Request enters through the UI or API, is classified by the brain, and becomes a set of actions. In cloud mode, app.py splits safe server actions from device actions, then either executes the safe subset or delegates the device subset through the device hub.

Lifecycle:
request -> brain/LLM -> orchestrator policy -> queue/delegate -> PC agent -> result -> notification/API response.

If no device is assigned, the task is stored as awaiting_agent.
If the device exists but is offline or temporarily unavailable, the task is queued_for_agent.
If the device is connected but lacks permission or capability metadata, the task is pending_permission.
If the device is connected and eligible, the task becomes executing, then delegated when the job is sent.
The agent returns results over the websocket, the backend marks the task completed or failed, and the user receives a notification event plus a refreshed task listing.
Timeout and retry behavior:

The backend uses a short agent-response timeout for delegated work, then marks the device temporarily unavailable if the response times out.
The delegated task retry limit is finite; retries use fallback status transitions and may try alternative plan options before final failure.
Stuck delegated tasks are periodically demoted from executing or delegated to failed when they exceed the stuck threshold.
Failure cases include invalid token, invalid JSON, missing actions, invalid action payloads, offline agent, missing permissions, forbidden actions, and agent result timeout.
6. TASK LIFECYCLE STATES
These states are the delegated-task lifecycle stored in app.py. They are separate from the smaller TaskManager enum in task_manager.py.

available: inferred state for a task that exists and is eligible to be claimed, scheduled, or resumed; mostly summary/UI-level.
executing: the backend has started a delegated plan or step and is actively waiting on device execution.
delegated: the job has been sent to the PC agent and is in transit or awaiting the agent’s result.
queued_for_agent: the task cannot run now because the agent is offline or the agent circuit breaker is open; it will resume later if possible.
awaiting_agent: the system knows the task should run on a device, but no device is assigned yet.
pending_permission: execution is blocked until a device capability, runtime permission, or agent metadata requirement is satisfied.
requires_configuration: inferred umbrella state for missing device assignment, missing capability report, or other setup that must be completed before dispatch.
restricted: policy blocked the task, usually because cloud mode, admin policy, or security rules forbid it.
failed: terminal error after timeout, invalid payload, forbidden result, repeated retry exhaustion, or stuck-task cleanup.
completed: terminal success after results are received and persisted.
7. PERMISSION SYSTEM
Permissions are split between backend policy and PC-agent runtime capability flags. The backend recognizes device permissions such as allow_app_control, allow_execute_command, allow_file_ops, allow_screen, and allow_self_update, and the agent applies them locally through its permission store in pc_agent.py and app.py.

Missing permissions are handled by returning pending_permission, saving the request, and surfacing a UI prompt instead of silently continuing.
If the agent is offline, the backend still saves the permission grant so it can auto-apply when the agent reconnects.
If the agent is connected, the backend dispatches an agent_set_permissions action to apply the new capability immediately.
User feedback is explicit: the frontend shows a permission modal, a device-control message, or a task status chip rather than a silent failure.
If the user is trying to affect another device, the backend rejects the request with 403 and a policy message.
8. CLOUD VS LOCAL BEHAVIOR
Cloud mode is the restricted hosted mode. It requires auth, blocks local/device/file-side execution on the server, and delegates device-side work to the PC agent. The executor and chat route enforce this in executor.py and app.py.

cloud mode: safe server actions may run, but open_app, execute_command, capture_screen, file writes, self-modification, and similar local actions are blocked on the backend.
local mode: the assistant may execute more actions directly, use local reasoning more aggressively, and can bootstrap more convenience behavior for development.
fallback logic: if cloud execution cannot happen, the system queues the task, marks it pending, or returns a restricted status with guidance.
sanitized info: cloud-safe system information is deliberately redacted and returned in a reduced form.
9. LLM USAGE LOGIC
The LLM is used as the general planner and responder, but it is not always the first thing Jarvis calls. Deterministic logic in llm_adapter.py overrides the model for simple or high-confidence cases.

when LLM is used: for general chat, complex planning, nontrivial reasoning, research synthesis, and structured action generation after deterministic fast-paths are skipped.
deterministic overrides: greeting replies, simple capability questions, direct generation drafts, goal plans, repeat-aware responses, and some action-intent shortcuts are answered without an external provider.
fallback provider logic: the adapter prefers the primary OpenAI-compatible path, can route to Groq-compatible backup, and may route to a local provider when the configuration allows it.
timeout handling: provider calls have a request budget, a per-call timeout, and a failure threshold that can open a provider cooldown circuit after repeated failures.
response normalization: the adapter strips unsolicited URLs from plain answers, filters actions out of informational replies unless execution was explicitly requested, deduplicates actions, adds routing metadata, and trims informational answers to a short form when no actions are present.
10. RESPONSE STRUCTURE
The core LLM response is a JSON object with text and actions. The chat route may add operational metadata before returning it to the caller.

response schema: text is a string, actions is a list, and the adapter may also attach source, routing, intent_type, intent_depth, response_strategy, emotion, proactive_followup_added, user_preference_influenced, language, learning_signal, task_id, and task_ids.
action format: each action is an object with a type field and action-specific payload, such as open_url, web_search, fetch_url, open_app, execute_command, type_text, hotkey, n8n_webhook, create_task, stop_task, or device_action.
error format: HTTP APIs usually return a detail payload or a JSON object with status, message, hint, and sometimes requirement or blocked_actions fields; the frontend client also falls back to a consistent status: failed payload when transport fails.
delegation response format: cloud dispatch returns status plus task, job, device_id, plan, plan_score, and possibly agent_result or message/hint; device_job_result notifications carry job_id, device_id, source_text, and results.
11. FRONTEND BEHAVIOR
The frontend is not just a display layer; it actively reflects task state, permission state, and delegated execution outcomes. The main notification listener in App.jsx reacts to websocket events from app.py.

task states are shown through polling views like TaskManager, which refreshes tasks and delegated tasks on a timer and shows status chips for running, delegated, queued, awaiting, pending permission, completed, and failed.
delegated tasks appear in the Delegated Queue and are summarized in AgentMonitor with counts for delegated, queued, awaiting, completed, and failed.
results are updated through websocket notifications and polling together; if a device job completes, the app logs the result, speaks a completion cue, and can prompt for missing permissions when the result indicates forbidden or error states.
permission flows are surfaced through PermissionModal, which is opened when the backend reports a required permission or access requirement.
device-control actions in the UI call the dispatch endpoint and render a lifecycle hint based on the returned status.
12. ERROR HANDLING
Error handling is explicit and status-driven rather than hidden.

timeout behavior: LLM provider timeouts become fallback responses or deterministic local replies; delegated agent timeouts mark the agent temporarily unavailable and push the task back to queued_for_agent or failed.
provider failure: repeated model failures open a provider cooldown and switch the adapter to fallback paths.
agent offline: device dispatch becomes queued_for_agent or awaiting_agent instead of failing silently.
invalid actions: malformed actions, missing types, blocked action types, and non-executable payloads are rejected with 400/403-style responses and concrete hints.
auth failures: websocket auth problems use close code 1008 and the frontend treats that as a re-login condition.
database or subsystem unavailability: many routes return sanitized 503 or skipped responses rather than crashing the whole server.
13. CURRENT SYSTEM WEAKNESSES
The system uses two related but different state machines: TaskManager statuses in task_manager.py and delegated-task statuses in app.py, which can drift in meaning and confuse the UI.
available and requires_configuration are normalized and displayed, but they appear to be mostly summary/inferred states rather than strongly enforced backend transitions.
The LLM adapter has many overlapping heuristics and post-processors; a request can be classified in more than one way before the final response strategy is chosen.
Cloud delegation depends on short-lived device availability and best-effort notifications; if the agent is unstable, the user may see queue/retry churn.
NotificationHub is in-process by default, so cross-worker delivery depends on broker support and remains best-effort.
Permission grants do not automatically resume all queued work in every path; some flows still require reconnect or a manual retry.
14. SAFE IMPROVEMENTS (NO ARCHITECTURE CHANGE)
Unify the meaning of delegated and task-manager states in the UI so status chips and summaries cannot drift.
Tighten the intent heuristics for ambiguous and goal-oriented prompts to reduce accidental clarification loops.
Make permission-grant resumes more explicit so queued tasks recover predictably after approval.
Keep timeout and retry messages more specific so users can tell whether the failure was auth, capability, agent offline, or execution timeout.
Add more structured logging around provider fallback, delegated retries, and permission-blocked dispatches.
Reduce status ambiguity in the frontend by deriving labels from the returned lifecycle hint and backend summary together rather than from one field alone.

15. RECENT BEHAVIOR GUARDS
Compound intent chaining: if one user sentence contains multiple executable intents joined by natural connectors (and/then/after), Jarvis should produce a full ordered action chain rather than stopping at the first step.
Clarification continuity: when required details are missing, Jarvis should ask for the minimum missing information, persist pending clarification context, and resume the original task once details are provided.
Side-question tolerance: if the user asks something unrelated while a task is waiting for details, Jarvis should answer the side question while keeping the original pending task recoverable.
Human-facing response policy: avoid exposing provider/fallback/debug internals to users in normal response text.
Execution result interpretation: classify delegated outcomes from normalized result fields (`success`, `error`, `status`) and treat idempotent outcomes (already/not found/no change needed) as successful where appropriate.