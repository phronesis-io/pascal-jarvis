"""Action processor — extracts and executes [ACTION:...] markers from Claude replies.

Replaces the inline bash/python action handlers in bot.sh process_actions().
Each action type has a handler method that returns an action result string.

Usage (from bot.sh):
    result=$(JV_REPLY="$reply" ... python3 -m core.actions)
    # stdout = cleaned_reply with action results appended
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def parse_params(raw: str) -> dict[str, str]:
    """Parse 'key=val|key=val' into a dict. Shared across all handlers."""
    params = {}
    for seg in raw.split("|"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            params[k.strip()] = v.strip()
    return params


def _run_cmd(cmd: list[str], timeout: int = 15, log_file: str = "") -> str:
    """Run a shell command, return stdout. Errors → 'FAILED'."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        if log_file:
            with open(log_file, "a") as f:
                f.write(f"[action] cmd error: {cmd[0]}: {e}\n")
        return "FAILED"


class ActionProcessor:
    """Processes [ACTION:...] markers in Claude's reply text."""

    def __init__(self, jarvis_dir: str | Path, memory_dir: str | Path,
                 jobs_dir: str | Path, log_file: str = ""):
        self.jarvis_dir = Path(jarvis_dir)
        self.memory_dir = Path(memory_dir)
        self.jobs_dir = Path(jobs_dir)
        self.log_file = log_file
        self._tm = None  # lazy TaskManager

    @property
    def tm(self):
        if self._tm is None:
            from core.tasks import TaskManager
            self._tm = TaskManager(self.memory_dir)
        return self._tm

    def process(self, reply: str) -> str:
        """Extract actions, execute them, return cleaned reply with results.

        Only strips markers for actions this processor handles.
        Unknown actions (bg, jobs, job_cancel, job_output) are left intact
        for the bash layer to process.
        """
        markers = re.findall(r'\[ACTION:[^\]]*\]', reply)
        if not markers:
            return reply

        results = []
        handled_markers = []
        for marker in markers:
            body = marker[8:-1]  # strip [ACTION: and ]
            action_type = body.split("|")[0]
            params_raw = body[len(action_type) + 1:] if "|" in body else ""

            handler = getattr(self, f"_do_{action_type}", None)
            if handler:
                result = handler(params_raw)
                if result:
                    results.append(result)
                handled_markers.append(marker)

        # Only strip markers we actually handled — leave unknown ones for bash
        cleaned = reply
        for marker in handled_markers:
            cleaned = cleaned.replace(marker, "", 1)
        cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip())

        if results:
            return cleaned + "\n" + "\n".join(results)
        return cleaned

    # ── Feed / Content ──

    def _do_feed_search(self, raw: str) -> str:
        query = parse_params(raw).get("query", raw.replace("query=", ""))
        try:
            r = subprocess.run(
                [sys.executable, "-m", "plugins.eigenflux.feed_search"],
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "JV_QUERY": query},
                cwd=str(self.jarvis_dir),
            )
            return r.stdout.strip() or ""
        except Exception:
            return "搜索失败"

    def _do_watchlater(self, raw: str) -> str:
        p = parse_params(raw)
        url = p.get("url", "")
        title = p.get("title", "")
        if url:
            subprocess.run(
                [sys.executable, str(self.jarvis_dir / "tasks/watchlater_save.py"),
                 title, url, "action"],
                capture_output=True, timeout=10, cwd=str(self.jarvis_dir),
            )
        return ""  # silent

    # ── Heartbeat ──

    def _do_heartbeat(self, raw: str) -> str:
        Path("/tmp/jarvis-heartbeat-trigger").touch()
        return ""

    # ── Calendar ──

    def _do_calendar_create(self, raw: str) -> str:
        p = parse_params(raw)
        title, start, end = p.get("title", ""), p.get("start", ""), p.get("end", "")
        desc = p.get("desc", "")
        if not (title and start and end):
            return ""

        # Conflict check
        is_busy = "free"
        try:
            r = _run_cmd(["lark-cli", "calendar", "+freebusy",
                          "--as", "user", "--start", start, "--end", end])
            if r != "FAILED":
                d = json.loads(r)
                is_busy = "busy" if d.get("busy") else "free"
        except Exception:
            pass

        conflict_note = ""
        if is_busy == "busy":
            conflict_note = f"⚠️ 时间段有冲突，但仍创建了: {title} ({start} → {end})\n"

        cmd = ["lark-cli", "calendar", "+create", "--as", "user",
               "--summary", title, "--start", start, "--end", end]
        if desc:
            cmd.extend(["--description", desc])
        result = _run_cmd(cmd, timeout=15, log_file=self.log_file)

        if "FAILED" in result:
            return f"❌ 日程创建失败: {title}"
        return conflict_note + f"✅ 已创建日程: {title} ({start} → {end})"

    def _do_calendar_update(self, raw: str) -> str:
        p = parse_params(raw)
        event_id, field, value = p.get("event_id", ""), p.get("field", ""), p.get("value", "")
        if not (event_id and field and value):
            return ""

        field_map = {
            "summary": {"summary": value},
            "start": {"start_time": {"timestamp": value}},
            "end": {"end_time": {"timestamp": value}},
        }
        data = json.dumps(field_map.get(field, {"description": value}))
        result = _run_cmd([
            "lark-cli", "calendar", "events", "patch", "--as", "user",
            "--params", json.dumps({"event_id": event_id}),
            "--data", data,
        ], log_file=self.log_file)

        if "FAILED" in result:
            return f"❌ 日程更新失败: {event_id} ({field})"
        return f"✅ 已更新日程: {field} → {value}"

    def _do_calendar_delete(self, raw: str) -> str:
        p = parse_params(raw)
        event_id = p.get("event_id", "")
        title = p.get("title", event_id)
        if not event_id:
            return ""
        result = _run_cmd(["lark-cli", "calendar", "events", "delete",
                           "--as", "user", "--event-id", event_id],
                          log_file=self.log_file)
        if "FAILED" in result:
            return f"❌ 日程删除失败: {title}"
        return f"✅ 已删除日程: {title}"

    # ── Lark Tasks ──

    def _do_task_create(self, raw: str) -> str:
        p = parse_params(raw)
        title, due = p.get("title", ""), p.get("due", "")
        if not title:
            return ""
        cmd = ["lark-cli", "task", "+create", "--as", "user", "--summary", title]
        if due:
            cmd.extend(["--due", due])
        result = _run_cmd(cmd, log_file=self.log_file)
        if "FAILED" in result:
            return f"❌ 任务创建失败: {title}"
        return f"✅ 已创建任务: {title}"

    def _do_task_complete(self, raw: str) -> str:
        p = parse_params(raw)
        task_id = p.get("task_id", "")
        if not task_id:
            return ""
        result = _run_cmd(["lark-cli", "task", "+complete", "--as", "user",
                           "--task-id", task_id], log_file=self.log_file)
        if "FAILED" in result:
            return f"❌ 任务完成标记失败: {task_id}"
        return "✅ 任务已完成"

    # ── Local Task System ──

    def _do_task_capture(self, raw: str) -> str:
        p = parse_params(raw)
        title = p.get("title", "")
        if not title:
            return ""
        t = self.tm.capture(
            title=title,
            type=p.get("type", "poiesis"),
            energy=p.get("energy", "medium"),
            time_est_min=int(p.get("est", "30")),
            due=p.get("due") or None,
            source="conversation",
        )
        return ""  # silent capture

    def _do_task_commit(self, raw: str) -> str:
        p = parse_params(raw)
        tid = p.get("id", "")
        if tid:
            self.tm.commit(tid, when=p.get("when") or None)
        return ""

    def _do_task_done(self, raw: str) -> str:
        p = parse_params(raw)
        tid = p.get("id", "")
        if tid:
            self.tm.done(tid)
        return ""

    def _do_task_reject(self, raw: str) -> str:
        p = parse_params(raw)
        tid = p.get("id", "")
        if tid:
            self.tm.reject(tid, p.get("reason", ""))
        return ""

    def _do_task_defer(self, raw: str) -> str:
        p = parse_params(raw)
        tid, to = p.get("id", ""), p.get("to", "")
        if tid and to:
            self.tm.defer(tid, to)
        return ""

    # ── Praxis ──

    def _do_praxis_done(self, raw: str) -> str:
        p = parse_params(raw)
        pid = p.get("id", "")
        if pid:
            self.tm.praxis_done(pid)
        return ""

    def _do_praxis_add(self, raw: str) -> str:
        p = parse_params(raw)
        title = p.get("title", "")
        if title:
            self.tm.praxis_add(
                title=title,
                frequency=p.get("freq", "daily"),
                preferred_time=p.get("time", "08:30"),
                duration_min=int(p.get("dur", "20")),
            )
        return ""

    def _do_praxis_remove(self, raw: str) -> str:
        p = parse_params(raw)
        pid = p.get("id", "")
        if pid:
            self.tm.praxis_remove(pid)
        return ""

    # ── Intentions ──

    def _do_intent_create(self, raw: str) -> str:
        p = parse_params(raw)
        name = p.get("name", "unnamed")
        when = p.get("when", "")
        trigger_type = p.get("type", "date")

        trigger_config = {}
        if trigger_type == "date":
            trigger_config = {"datetime": when}
        elif trigger_type == "cron":
            trigger_config = {"expression": when}
        elif trigger_type == "interval":
            trigger_config = {"seconds": int(when) if when.isdigit() else 600}

        try:
            from core.intentions import create_intent
            iid = create_intent(
                name=name, trigger_type=trigger_type, trigger_config=trigger_config,
                prompt=p.get("prompt", name), purpose=p.get("purpose", ""),
                tags=[t.strip() for t in p.get("tags", "").split(",") if t.strip()],
                priority=int(p.get("priority", "5")),
                source="agent", action_type=p.get("action", "notify"),
            )
            return f'✅ Intent "{name}" created (id: {iid})'
        except Exception as e:
            return f"❌ Intent 创建失败: {e}"

    def _do_intent_cancel(self, raw: str) -> str:
        p = parse_params(raw)
        try:
            from core.intentions import cancel_intent
            ok = cancel_intent(p.get("id", ""), p.get("reason", ""))
            return "Intent cancelled" if ok else "Intent not found or already done"
        except Exception:
            return "FAILED"

    def _do_intent_list(self, raw: str) -> str:
        try:
            from core.intentions import list_intents
            intents = list_intents(status="pending", limit=20)
            if not intents:
                return "📋 Active Intents:\nNo active intents"
            lines = ["📋 Active Intents:"]
            for i in intents:
                tc = json.loads(i["trigger_config"]) if isinstance(i["trigger_config"], str) else i["trigger_config"]
                when = tc.get("datetime", tc.get("expression", tc.get("seconds", "?")))
                lines.append(f"- {i['name']} [{i['trigger_type']}:{when}] (id:{i['id']})")
            return "\n".join(lines)
        except Exception:
            return "FAILED"

    # ── Schedule ──

    def _do_schedule_task(self, raw: str) -> str:
        try:
            from dashboard.heartbeat_bridge import register_from_action
            result = register_from_action(raw)
            return f"✅ {result}" if result else "❌ 动态任务注册失败"
        except Exception:
            return "❌ 动态任务注册失败"

    # ── Jobs (bg, jobs, job_cancel, job_output handled in bash — need & and wait) ──
    # These return empty so bash fallback handles them
    # NOTE: bg, jobs, job_cancel, job_output remain in bot.sh because they require
    # bash process control (background &, wait, PID tracking)


# ── CLI entry point ──

if __name__ == "__main__":
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    sys.path.insert(0, jarvis_dir)

    reply = os.environ.get("JV_REPLY", "")
    if not reply:
        reply = sys.stdin.read()

    ap = ActionProcessor(
        jarvis_dir=jarvis_dir,
        memory_dir=os.environ.get("MEMORY_DIR", "memory"),
        jobs_dir=os.environ.get("JV_JOBS_DIR", "jobs"),
        log_file=os.environ.get("JV_LOG_FILE", ""),
    )
    print(ap.process(reply))
