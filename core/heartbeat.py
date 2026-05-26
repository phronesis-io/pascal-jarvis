"""Heartbeat runner — parse HEARTBEAT.md, schedule tasks, orchestrate Claude.

The heartbeat is the core loop that drives Jarvis. Every N seconds it checks
which tasks are due, runs their pre-scripts to gather data, batches them into
a single Claude call, and routes responses through post-scripts.
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .log import log as _structured_log
from .memory import load_tiered_memory
from .task_protocol import TaskState
from .timeutil import now_local_str


def parse_interval(s: str) -> int:
    """Parse '10m', '2h', '1d' to seconds."""
    s = s.strip()
    m = re.match(r"(\d+)\s*(s|m|h|d)", s)
    if not m:
        return 600
    val, unit = int(m.group(1)), m.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_heartbeat(path: str | Path) -> list[dict]:
    """Parse HEARTBEAT.md into task definitions."""
    text = Path(path).read_text(encoding="utf-8")
    tasks = []
    current = None

    for line in text.splitlines():
        if line.startswith("### "):
            if current:
                if "_in_prompt" in current:
                    del current["_in_prompt"]
                tasks.append(current)
            current = {"name": line[4:].strip(), "interval": 600,
                        "pre": "", "post": "", "prompt": ""}
        elif current:
            if line.startswith("- interval:"):
                current["interval"] = parse_interval(line.split(":", 1)[1])
            elif line.startswith("- pre:"):
                current["pre"] = line.split(":", 1)[1].strip()
            elif line.startswith("- post:"):
                current["post"] = line.split(":", 1)[1].strip()
            elif line.startswith("- prompt:"):
                rest = line.split(":", 1)[1].strip()
                if rest == "|":
                    current["_in_prompt"] = True
                else:
                    current["prompt"] = rest
            elif current.get("_in_prompt"):
                if line.startswith("- ") or line.startswith("### "):
                    del current["_in_prompt"]
                else:
                    current["prompt"] += line.lstrip() + "\n"

    if current:
        if "_in_prompt" in current:
            del current["_in_prompt"]
        tasks.append(current)

    return tasks


class HeartbeatRunner:
    """Drives the task scheduling loop."""

    # Tasks that should retry sooner when pre-script returns empty
    EMPTY_RETRY_DELAYS = {
        "checkin": 300,
        "daily-plan": 1800,       # retry every 30min until window hits
        "daily-reflect": 1800,    # retry every 30min until window hits
        "personal-site": 3600,
        "memory-daily": 3600,
        "memory-weekly": 3600,
        "memory-monthly": 3600,
        "memory-consolidate": 600,
    }

    # Memory pipeline tasks — only one per cycle to prevent races
    PIPELINE_TASKS = {"memory-hourly", "memory-daily", "memory-weekly", "memory-monthly"}

    # Tasks exempt from batch cap — they run on every cycle regardless.
    # These are critical infrastructure tasks whose pre→post pipeline doesn't
    # need Claude reasoning, but must stay fresh for other tasks to work.
    # - calendar-sync: feeds checkin, daily-plan, free-time-nudge
    # - memory-hourly: feeds daily→weekly→monthly memory pipeline
    # - activity-log: feeds daily-reflect
    # - cross-session-sync: feeds memory consolidation
    PRIORITY_TASKS = {"calendar-sync", "memory-hourly", "activity-log", "cross-session-sync"}

    # Max tasks to batch into a single Claude call.
    # Prevents timeout when many tasks are due simultaneously (e.g. after restart).
    # Remaining tasks will be picked up in the next cycle.
    # NOTE: PRIORITY_TASKS bypass this cap entirely.
    MAX_BATCH_SIZE = 4

    # Minimum interval between force-triggered runs of the same task, in seconds.
    # Prevents rapid Lark session rotations from hammering memory-hourly.
    FORCE_COOLDOWN_SECONDS = 60

    def __init__(self, jarvis_dir: str | Path, heartbeat_file: str | Path,
                 state_file: str | Path, memory_dir: str | Path,
                 model: str = "sonnet", persona: str = "Jarvis",
                 work_dir: str | Path | None = None):
        self.jarvis_dir = Path(jarvis_dir)
        self.heartbeat_file = Path(heartbeat_file)
        self.state_file = Path(state_file)
        self.memory_dir = Path(memory_dir)
        self.model = model
        self.persona = persona
        self.work_dir = Path(work_dir) if work_dir else self.jarvis_dir
        self._cid = ""  # cycle_id, set per run_cycle invocation
        self._tasks_cache = None   # cached parse result
        self._tasks_mtime = 0.0    # mtime when cache was built

    def _log(self, msg: str, **kwargs):
        """Structured log with cycle_id for correlation."""
        _structured_log("heartbeat", msg, cycle=self._cid, **kwargs)

    def _load_tasks(self) -> list[dict]:
        """Parse HEARTBEAT.md with mtime-based cache (avoid re-parsing every 10s)."""
        try:
            mtime = self.heartbeat_file.stat().st_mtime
        except OSError:
            return self._tasks_cache or []
        if self._tasks_cache is not None and mtime == self._tasks_mtime:
            return self._tasks_cache
        self._tasks_cache = parse_heartbeat(self.heartbeat_file)
        self._tasks_mtime = mtime
        return self._tasks_cache

    def load_state(self) -> dict:
        if self.state_file.exists():
            return json.loads(self.state_file.read_text())
        return {}

    def save_state(self, state: dict):
        """Atomic write: temp + rename prevents corrupted state on crash."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2))
        os.replace(tmp, self.state_file)

    def run_script(self, script_path: str, stdin_data: str = "") -> str:
        """Run a pre/post script, return stdout."""
        full_path = self.jarvis_dir / script_path
        if not full_path.exists():
            return ""
        try:
            # Use python3 for .py files that aren't executable
            if full_path.suffix == ".py" and not os.access(full_path, os.X_OK):
                cmd = ["python3", str(full_path)]
            else:
                cmd = [str(full_path)]
            result = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True, text=True,
                timeout=60,
                cwd=str(self.jarvis_dir),
            )
            if result.returncode != 0 and result.stderr.strip():
                self._log(f"Script {script_path} stderr: {result.stderr.strip()[:200]}")
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            self._log(f"Script {script_path} timed out (60s)")
            return ""
        except Exception as e:
            self._log(f"Script {script_path} error: {e}")
            return ""

    def claude_call(self, prompt: str) -> str:
        """Call Claude with memory injection, no session persistence."""
        memory = load_tiered_memory(self.memory_dir)
        now_ts = now_local_str("%Y-%m-%d %H:%M %A")
        system_prompt = f"""You are {self.persona}, a personal AI assistant and life mentor.
Current time: {now_ts}
You have access to the user's memory below. Use it to personalize your responses.

{memory}"""

        cmd = [
            "claude",
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--system-prompt", system_prompt,
            "--disable-slash-commands",
            "-p", prompt,
        ]
        if self.model:
            cmd.extend(["--model", self.model])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=300, stdin=subprocess.DEVNULL,
                cwd=str(self.work_dir),
            )
            if result.returncode != 0:
                self._log(f"Claude exited with code {result.returncode}")
                if result.stderr.strip():
                    self._log(f"Claude stderr: {result.stderr.strip()[:300]}")
                # Exit 143 = killed by SIGTERM (128+15). This is an infrastructure
                # event (restart/shutdown), not a task failure. Return a sentinel
                # so run_cycle doesn't punish tasks via circuit breaker.
                if result.returncode in (137, 143):  # SIGKILL=137, SIGTERM=143
                    return "__KILLED__"
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            self._log("Claude call timed out (300s)")
            return ""
        except FileNotFoundError:
            self._log("Claude CLI not found — is it installed?")
            return ""
        except Exception as e:
            self._log(f"Claude error: {e}")
            return ""

    def _check_dynamic_tasks(self) -> list[str]:
        """Check SQLite-based dynamic tasks. Returns user-facing messages."""
        try:
            from dashboard.heartbeat_bridge import check_dynamic_tasks
            result = check_dynamic_tasks()
            if not result:
                return []
            import json as _json
            data = _json.loads(result)
            messages = []
            for task in data.get("tasks", []):
                action_type = task.get("action_type", "")
                config = task.get("action_config", {})
                if action_type == "notify":
                    msg = config.get("message", "")
                    if msg:
                        messages.append(msg)
                # Other action types handled by extensions
            return messages
        except ImportError:
            return []
        except Exception as e:
            self._log(f"Dynamic task check error: {e}")
            return []

    def run_cycle(self, force: bool = False, only_task: str = ""):
        """Run one heartbeat cycle. Returns user-facing message or empty string."""
        cycle_id = uuid.uuid4().hex[:8]
        self._cid = cycle_id  # used by _log

        tasks = self._load_tasks()
        state = self.load_state()
        now = int(time.time())

        # Determine which tasks are due
        due_tasks = []
        circuit_tripped = []  # tasks skipped due to open circuit
        pipeline_picked = False
        for task in tasks:
            if only_task and task["name"] != only_task:
                continue
            ts = TaskState.from_dict(state.get(task["name"], {}))
            last_run = ts.last_run
            # Circuit breaker: skip tasks that have been auto-disabled
            # PRIORITY_TASKS are exempt — they're infrastructure that must stay running
            if ts.circuit.is_open and task["name"] not in self.PRIORITY_TASKS:
                remaining = ts.circuit.remaining_disable_seconds
                circuit_tripped.append((task["name"], remaining))
                continue
            # Apply cooldown even when forced, to prevent rapid repeats
            # (e.g. multiple Lark session rotations in quick succession).
            if force and (now - last_run) < self.FORCE_COOLDOWN_SECONDS:
                continue
            if force or (now - last_run >= task["interval"]):
                if task["name"] in self.PIPELINE_TASKS:
                    if pipeline_picked:
                        continue
                    pipeline_picked = True
                due_tasks.append(task)

        if circuit_tripped:
            self._log(f"Circuit open: {[(n, f'{s}s') for n, s in circuit_tripped]}")

        if not due_tasks:
            return ""

        # Separate priority tasks (exempt from batch cap) from regular tasks
        priority = [t for t in due_tasks if t["name"] in self.PRIORITY_TASKS]
        regular = [t for t in due_tasks if t["name"] not in self.PRIORITY_TASKS]

        # Cap batch size BEFORE pre-scripts to avoid side-effect waste.
        # Sort by staleness (longest since last run first) to prevent starvation.
        deferred = []
        if len(regular) > self.MAX_BATCH_SIZE:
            regular.sort(key=lambda t: state.get(t["name"], {}).get("last_run", 0))
            deferred = regular[self.MAX_BATCH_SIZE:]
            regular = regular[:self.MAX_BATCH_SIZE]
            self._log(f"Batch capped at {self.MAX_BATCH_SIZE}, "
                      f"deferred {len(deferred)}: {[t['name'] for t in deferred]}")

        due_tasks = priority + regular

        # Run pre-scripts (record failures in circuit breaker)
        task_data = {}
        runnable = []
        skipped = []
        for task in due_tasks:
            if task["pre"]:
                data = self.run_script(task["pre"])
                if not data:
                    retry_delay = self.EMPTY_RETRY_DELAYS.get(task["name"], task["interval"])
                    ts = TaskState.from_dict(state.get(task["name"], {}))
                    ts.last_run = now - task["interval"] + retry_delay
                    state[task["name"]] = ts.to_dict()
                    skipped.append(task["name"])
                    continue
                task_data[task["name"]] = data
            else:
                task_data[task["name"]] = ""
            runnable.append(task)

        if not runnable:
            if force:
                self.save_state(state)
                self._log(self._beat_status(due_tasks, skipped, runnable, tasks))
            else:
                self.save_state(state)
            return ""

        # ── Tier 0: PRIORITY_TASKS bypass Claude entirely ──────────────
        # Their pre-script output goes directly to post-script.
        # This saves Claude API calls and eliminates timeout risk.
        tier0 = [t for t in runnable if t["name"] in self.PRIORITY_TASKS]
        tier2 = [t for t in runnable if t["name"] not in self.PRIORITY_TASKS]
        user_messages = []
        producing_tasks = []

        for task in tier0:
            pre_data = task_data.get(task["name"], "")
            if task["post"] and pre_data:
                post_output = self.run_script(task["post"], stdin_data=pre_data)
                if post_output:
                    user_messages.append(post_output)
                    producing_tasks.append(task["name"])
            # Update state — task ran successfully
            ts = TaskState.from_dict(state.get(task["name"], {}))
            ts.last_run = now
            ts.circuit.record_success()
            state[task["name"]] = ts.to_dict()

        if tier0:
            self._log(f"Tier 0 direct: {[t['name'] for t in tier0]}")

        # ── Tier 2: regular tasks go through Claude ────────────────────
        runnable = tier2
        if not runnable:
            # Only Tier 0 tasks ran — save state and return any output
            self.save_state(state)
            combined = "\n\n---\n\n".join(m for m in user_messages if m.strip())
            if combined.strip():
                if producing_tasks:
                    try:
                        source_file = self.jarvis_dir / ".heartbeat_last_source"
                        source_file.write_text(",".join(producing_tasks))
                    except Exception:
                        pass
                self._log(f"{self._beat_status(due_tasks, skipped, tier0, tasks)} → delivered (tier0 only)")
                return combined
            self._log(f"{self._beat_status(due_tasks, skipped, tier0, tasks)} → OK (tier0 only)")
            return ""

        # Build combined prompt
        n = len(runnable)
        parts = [f"[HEARTBEAT — {n} task{'s' if n > 1 else ''} due]"]
        parts.append("Process each task below. For each, return the requested format.")
        parts.append("If NOTHING across ALL tasks needs user attention, reply with exactly: HEARTBEAT_OK")
        parts.append("")

        for task in runnable:
            parts.append(f"=== TASK: {task['name']} ===")
            parts.append(task["prompt"].strip())
            data = task_data.get(task["name"], "")
            if data:
                parts.append(f"\nDATA:\n{data}")
            parts.append("")

        parts.append("=== END TASKS ===")
        if n > 1:
            parts.append('Return JSON: {"tasks":{"<task-name>": <per-task response>}, "user_message":"<combined markdown or empty>"}')
        else:
            parts.append("Return the task's requested format directly.")
        parts.append("Or if nothing needs attention: HEARTBEAT_OK")

        prompt = "\n".join(parts)

        # Call Claude
        self._log(f"Calling Claude with {n} tasks...")
        raw = self.claude_call(prompt)

        if raw == "__KILLED__":
            # Claude was killed by SIGTERM/SIGKILL (restart/shutdown).
            # This is NOT a task failure — don't punish via circuit breaker.
            # Just update last_run so tasks don't immediately re-fire.
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                ts.last_run = now
                state[task["name"]] = ts.to_dict()
            self.save_state(state)
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → killed (no penalty)")
            return ""

        if not raw:
            # Claude call failed (timeout, network, etc.) — record failure
            tripped_names = []
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                ts.last_run = now
                tripped = ts.circuit.record_failure()
                # PRIORITY_TASKS: reset circuit immediately — they must never be disabled
                if task["name"] in self.PRIORITY_TASKS and ts.circuit.is_open:
                    ts.circuit.disabled_until = 0
                elif tripped:
                    tripped_names.append(task["name"])
                state[task["name"]] = ts.to_dict()
            self.save_state(state)
            if tripped_names:
                self._log(f"Circuit TRIPPED for: {tripped_names}")
                return f"⚠️ 以下任务连续失败已自动暂停: {', '.join(tripped_names)}。系统会在冷却后自动恢复。"
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → Claude failed")
            return ""

        if raw.strip() == "HEARTBEAT_OK":
            # Only treat as idle if the ENTIRE response is exactly HEARTBEAT_OK.
            # A multi-task JSON envelope containing "HEARTBEAT_OK" as a per-task
            # value must NOT be discarded — other tasks may have real content.
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                ts.last_run = now
                ts.circuit.record_success()
                state[task["name"]] = ts.to_dict()
            self.save_state(state)
            # Log status to stderr (goes to jarvis.log) — NOT returned to Lark
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → OK")
            return ""

        # Route responses through post-scripts
        # (user_messages and producing_tasks may already have Tier 0 results)
        if n == 1:
            task = runnable[0]
            if task["post"]:
                post_output = self.run_script(task["post"], stdin_data=raw)
                if post_output:
                    user_messages.append(post_output)
                    producing_tasks.append(task["name"])
            else:
                user_messages.append(raw)
                producing_tasks.append(task["name"])
        else:
            # Multi-task: Claude returns JSON envelope. Extract robustly.
            cleaned = re.sub(r'^```json?\s*', '', raw.strip())
            cleaned = re.sub(r'```\s*$', '', cleaned.strip())
            # Try to find JSON object if there's preamble text
            json_start = cleaned.find('{')
            json_end = cleaned.rfind('}')
            if json_start >= 0 and json_end > json_start:
                cleaned = cleaned[json_start:json_end + 1]
            try:
                envelope = json.loads(cleaned)
                task_responses = envelope.get("tasks", {})
                for task in runnable:
                    resp = task_responses.get(task["name"])
                    if resp is None:
                        continue
                    resp_str = json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp)
                    if task["post"]:
                        post_output = self.run_script(task["post"], stdin_data=resp_str)
                        if post_output:
                            user_messages.append(post_output)
                            producing_tasks.append(task["name"])
                    # Tasks without post-scripts: only show string responses, never raw JSON
                    elif isinstance(resp, str) and resp.strip() and "HEARTBEAT_OK" not in resp:
                        user_messages.append(resp)
                        producing_tasks.append(task["name"])
                top_msg = envelope.get("user_message", "")
                if top_msg and top_msg.strip():
                    user_messages.append(top_msg)
            except json.JSONDecodeError:
                # NEVER dump raw JSON to user — log for debugging and skip
                self._log(f"JSON parse failed for {n}-task response, skipping output")
                self._log(f"Raw response (first 300 chars): {raw[:300]}")

        # Update state (preserve circuit breaker data, record success)
        for task in runnable:
            ts = TaskState.from_dict(state.get(task["name"], {}))
            ts.last_run = now
            ts.circuit.record_success()
            state[task["name"]] = ts.to_dict()
        self.save_state(state)

        # Also check dynamic tasks from SQLite scheduler
        dynamic_msgs = self._check_dynamic_tasks()
        user_messages.extend(dynamic_msgs)

        # Separate card JSON from plain text — they use different Lark send paths
        cards = [m for m in user_messages if m.strip().startswith('{"config":')]
        texts = []
        for m in user_messages:
            m = m.strip()
            if not m or m.startswith('{"config":'):
                continue
            # Safety net: never send raw JSON to user — strip JSON-looking content
            if (m.startswith('{') and m.endswith('}')) or (m.startswith('[') and m.endswith(']')):
                try:
                    json.loads(m)
                    # It's valid JSON that isn't a card — log and skip
                    self._log(f"Blocked raw JSON from reaching user: {m[:100]}...")
                    continue
                except json.JSONDecodeError:
                    pass
            # Also block strings containing a JSON object that makes up >50% of the content
            # (catches cases like "Here's the result: {"intents": ...}")
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', m)
            if json_match and len(json_match.group()) > len(m) * 0.5:
                try:
                    json.loads(json_match.group())
                    self._log(f"Blocked embedded JSON from reaching user: {m[:100]}...")
                    # Extract any non-JSON text and keep it
                    text_part = m[:json_match.start()].strip()
                    if text_part:
                        texts.append(text_part)
                    continue
                except json.JSONDecodeError:
                    pass
            texts.append(m)

        combined_parts = []
        for card in cards:
            combined_parts.append(f"CARD:{card.strip()}")
        if texts:
            combined_parts.append("\n\n---\n\n".join(texts))

        combined = "\n".join(combined_parts) if combined_parts else ""
        beat = self._beat_status(due_tasks, skipped, runnable, tasks)

        # Status line → log only. User message → return to Lark.
        if combined.strip():
            # Write producing sources ONLY when we actually deliver content.
            # Prevents stale source files from being read by a later cycle.
            if producing_tasks:
                try:
                    source_file = self.jarvis_dir / ".heartbeat_last_source"
                    source_file.write_text(",".join(producing_tasks))
                except Exception:
                    pass
            self._log(f"{beat} → delivered")
            return combined
        self._log(f"{beat} → OK (no user content)")
        return ""

    def _beat_status(self, due_tasks, skipped, runnable, all_tasks) -> str:
        ts = now_local_str("%H:%M")
        parts = [f"[{ts}]"]
        for t in due_tasks:
            name = t["name"].replace("eigenflux-", "")
            if t in runnable:
                parts.append(f"> {name}")
            elif t["name"] in skipped:
                parts.append(f". {name} (empty)")
            else:
                parts.append(f". {name}")
        not_due = len(all_tasks) - len(due_tasks)
        if not_due > 0:
            parts.append(f"| {not_due} not due")
        return " | ".join(parts)
