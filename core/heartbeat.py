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
from pathlib import Path

from .memory import load_tiered_memory
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
        "memory-daily": 3600,
        "memory-weekly": 3600,
        "memory-monthly": 3600,
        "memory-consolidate": 600,
    }

    # Memory pipeline tasks — only one per cycle to prevent races
    PIPELINE_TASKS = {"memory-hourly", "memory-daily", "memory-weekly", "memory-monthly"}

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
                timeout=30,
                cwd=str(self.jarvis_dir),
            )
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"[heartbeat] Script {script_path} error: {e}", file=sys.stderr)
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
            return result.stdout.strip()
        except (subprocess.TimeoutExpired, Exception) as e:
            print(f"[heartbeat] Claude error: {e}", file=sys.stderr)
            return ""

    def run_cycle(self, force: bool = False, only_task: str = ""):
        """Run one heartbeat cycle. Returns user-facing message or empty string."""
        tasks = parse_heartbeat(self.heartbeat_file)
        state = self.load_state()
        now = int(time.time())

        # Determine which tasks are due
        due_tasks = []
        pipeline_picked = False
        for task in tasks:
            if only_task and task["name"] != only_task:
                continue
            last_run = state.get(task["name"], {}).get("last_run", 0)
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

        if not due_tasks:
            return ""

        # Run pre-scripts
        task_data = {}
        runnable = []
        skipped = []
        for task in due_tasks:
            if task["pre"]:
                data = self.run_script(task["pre"])
                if not data:
                    retry_delay = self.EMPTY_RETRY_DELAYS.get(task["name"], task["interval"])
                    state[task["name"]] = {"last_run": now - task["interval"] + retry_delay}
                    skipped.append(task["name"])
                    continue
                task_data[task["name"]] = data
            else:
                task_data[task["name"]] = ""
            runnable.append(task)

        if not runnable:
            if force:
                self.save_state(state)
                print(f"[heartbeat] {self._beat_status(due_tasks, skipped, runnable, tasks)}",
                      file=sys.stderr)
            else:
                self.save_state(state)
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
        print(f"[heartbeat] Calling Claude with {n} tasks...", file=sys.stderr)
        raw = self.claude_call(prompt)

        if not raw or "HEARTBEAT_OK" in raw:
            for task in runnable:
                state[task["name"]] = {"last_run": now}
            self.save_state(state)
            # Log status to stderr (goes to jarvis.log) — NOT returned to Lark
            print(f"[heartbeat] {self._beat_status(due_tasks, skipped, runnable, tasks)} → OK",
                  file=sys.stderr)
            return ""

        # Route responses through post-scripts
        user_messages = []
        if n == 1:
            task = runnable[0]
            if task["post"]:
                post_output = self.run_script(task["post"], stdin_data=raw)
                if post_output:
                    user_messages.append(post_output)
            else:
                user_messages.append(raw)
        else:
            cleaned = re.sub(r'^```json?\s*', '', raw.strip())
            cleaned = re.sub(r'```\s*$', '', cleaned.strip())
            try:
                envelope = json.loads(cleaned)
                task_responses = envelope.get("tasks", {})
                for task in runnable:
                    resp = task_responses.get(task["name"])
                    if resp is None:
                        continue
                    resp_str = json.dumps(resp) if isinstance(resp, dict) else str(resp)
                    if task["post"]:
                        post_output = self.run_script(task["post"], stdin_data=resp_str)
                        if post_output:
                            user_messages.append(post_output)
                    else:
                        text = resp if isinstance(resp, str) else resp_str
                        if text.strip() and "HEARTBEAT_OK" not in text:
                            user_messages.append(text)
                top_msg = envelope.get("user_message", "")
                if top_msg and top_msg.strip():
                    user_messages.append(top_msg)
            except json.JSONDecodeError:
                user_messages.append(raw)

        # Update state
        for task in runnable:
            state[task["name"]] = {"last_run": now}
        self.save_state(state)

        combined = "\n\n---\n\n".join(m for m in user_messages if m.strip())
        beat = self._beat_status(due_tasks, skipped, runnable, tasks)
        # Status line → log only. User message → return to Lark.
        if combined.strip():
            print(f"[heartbeat] {beat} → delivered", file=sys.stderr)
            return combined
        print(f"[heartbeat] {beat} → OK (no user content)", file=sys.stderr)
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
