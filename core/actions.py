"""Action processor — extracts and executes [ACTION:...] markers from Claude replies.

Replaces the inline bash/python action handlers in bot.sh process_actions().
Each action type has a handler method that returns an action result string.

Usage (from bot.sh):
    result=$(JV_REPLY="$reply" ... python3 -m core.actions)
    # stdout = cleaned_reply with action results appended
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def parse_params(raw: str) -> dict[str, str]:
    """Parse 'key=val|key=val' into a dict. Shared across all handlers."""
    params = {}
    for seg in raw.split("|"):
        if "=" in seg:
            k, v = seg.split("=", 1)
            params[k.strip()] = v.strip()
    return params


def _auto_category(tags_csv: str = "", name: str = "", prompt: str = "") -> str:
    """Heuristic intent category when the author omits it. Maps to the 5-category
    taxonomy in behavioral_rules.md §5.

    name+tags are the AUTHOR-INTENT signal (matched strongly); the prompt is only
    consulted for unambiguous deadline words (greedy single chars like 读/听 in a
    prompt produce false positives — a work follow-up that merely *mentions*
    reading is not a learning intent). Order matters: hard/autonomous/external are
    decided before healing so a work/follow-up row is never silently swallowed into
    the never-nag bucket. Truly ambiguous → 'none' (no follow-up, behaves as today,
    bounds blast radius). Migration re-uses this and shows a --dry-run diff to review.
    """
    strong = f"{tags_csv} {name}".lower()   # tags + name = what the author meant

    # Per-user keyword extensions (personal data = config, not code —
    # 2026-07-13 ruling): data/category_keywords_personal.json maps category →
    # extra keywords (e.g. a personal project codename → external). The file
    # is gitignored; absent = generic keywords only.
    personal: dict = {}
    try:
        _pf = Path(__file__).resolve().parent.parent / "data" / "category_keywords_personal.json"
        personal = {k: [str(w).lower() for w in v]
                    for k, v in json.loads(_pf.read_text(encoding="utf-8")).items()
                    if isinstance(v, list)}
    except (OSError, ValueError):
        personal = {}

    def s(*ks, cat: str = ""):
        extra = personal.get(cat, []) if cat else []
        return any(k in strong for k in (*ks, *extra))

    # hard from name/tags only — a prompt that merely *mentions* 到期/续费 (e.g. a
    # daily report told to "watch for expirations") is not itself a hard constraint.
    if s("票", "续费", "关煤气", "gas", "deadline", "到期", "renew", "expire",
         "expiry", cat="hard"):
        return "hard"
    if s("日报", "夜工", "夜间", "深工", "report", "复盘", "小时报", "自主",
         "autonomous", cat="autonomous"):
        return "autonomous"
    if s("external", "follow-up", "followup", "social", "meet", "饭", "约",
         "面试", "跟进", "对外", "adapter", "婚礼", "红包", "请柬",
         cat="external"):
        return "external"
    if s("health", "rehab", "healing", "reading", "learning", "康复", "疗愈",
         "哲学", "学习", "臀", "拉伸", "阅读", "读", "听", "冥想", "戒断",
         "训练", "anchor", "课", "讲", cat="healing"):
        return "healing"
    if s("calendar-prep", "prep", cat="context"):
        return "context"
    return "none"


def _run_cmd(cmd: list[str], timeout: int = 15, log_file: str = "") -> str:
    """Run a shell command, return stdout on success.

    On failure — either a nonzero exit or a Python-level exception — return
    "FAILED: <reason>". lark-cli writes its error envelope to stderr and
    leaves stdout empty on most failures (auth expiry, bad IDs, validation),
    so a caller that only inspected stdout for the literal string FAILED
    would treat that empty-but-nonzero-exit result as success.
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            reason = (r.stderr or r.stdout or "").strip().replace("\n", " ")[:200]
            if log_file:
                with open(log_file, "a") as f:
                    f.write(f"[action] cmd failed (exit {r.returncode}): {cmd[0]}: {reason}\n")
            return f"FAILED: {reason}"
        return r.stdout.strip()
    except Exception as e:
        if log_file:
            with open(log_file, "a") as f:
                f.write(f"[action] cmd error: {cmd[0]}: {e}\n")
        return f"FAILED: {e}"


class ActionProcessor:
    """Processes [ACTION:...] markers in Claude's reply text."""

    def __init__(self, jarvis_dir: str | Path, memory_dir: str | Path,
                 jobs_dir: str | Path, log_file: str = "",
                 heartbeat_trigger_path: str | Path = "/tmp/jarvis-heartbeat-trigger",
                 owner_authenticated: bool = False):
        self.jarvis_dir = Path(jarvis_dir)
        self.memory_dir = Path(memory_dir)
        self.jobs_dir = Path(jobs_dir)
        self.log_file = log_file
        self.heartbeat_trigger_path = Path(heartbeat_trigger_path)
        self.owner_authenticated = bool(owner_authenticated)
        self._tm = None  # lazy TaskManager
        self._primary_calendar_id: str | None = None  # lazy, cached for this instance

    @property
    def tm(self):
        if self._tm is None:
            from core.tasks import TaskManager
            self._tm = TaskManager(self.memory_dir)
        return self._tm

    def process(self, reply: str, execute: bool = True) -> str:
        """Extract actions, execute them, return cleaned reply with results.

        Only strips markers for actions this processor handles.
        Unknown actions (bg, jobs, job_cancel, job_output) are left intact
        for the bash layer to process.

        execute=False (REQ-102, group chat): strip EVERY marker (including
        the bash-layer ones) and execute NOTHING — a non-owner group member
        must not be able to drive calendar writes / broadcasts / jobs through
        the reply channel. A short notice replaces the markers so the reply
        doesn't silently pretend the action happened.
        """
        markers = re.findall(r'\[ACTION:[^\]]*\]', reply)
        if not markers:
            return reply
        if not execute:
            cleaned = re.sub(r'\[ACTION:[^\]]*\]', '', reply).strip()
            return (cleaned + "\n\n（⚙️ 动作类指令仅限主人触发，这里只答疑不执行）").strip()

        results = []
        authoritative_results = []
        handled_markers = []
        owner_actions = {
            "delegation_confirm",
            "delegation_cancel",
            "delegation_retry",
            "iteration_approve",
            "iteration_reject",
        }
        receipt_actions = owner_actions | {
            "eigenflux_friend",
            "eigenflux_message",
        }
        for marker in markers:
            body = marker[8:-1]  # strip [ACTION: and ]
            action_type = body.split("|")[0]
            params_raw = body[len(action_type) + 1:] if "|" in body else ""

            if not re.fullmatch(r'[a-z][a-z0-9_]{0,30}', action_type):
                continue

            handler = getattr(self, f"_do_{action_type}", None)
            if handler:
                if action_type in owner_actions and not self.owner_authenticated:
                    authoritative_results.append(
                        "❌ 这个决定只能通过已认证的奏折按钮或控制台完成，"
                        "模型输出没有获得主人授权。"
                    )
                    handled_markers.append(marker)
                    continue
                try:
                    result = handler(params_raw)
                except Exception as exc:
                    if action_type not in receipt_actions:
                        raise
                    authoritative_results.append(f"❌ 动作未生效：{exc}")
                    handled_markers.append(marker)
                    continue
                if result:
                    if action_type in receipt_actions:
                        authoritative_results.append(result)
                    else:
                        results.append(result)
                handled_markers.append(marker)

        # Only strip markers we actually handled — leave unknown ones for bash
        cleaned = reply
        for marker in handled_markers:
            cleaned = cleaned.replace(marker, "", 1)
        cleaned = "\n".join(line for line in cleaned.splitlines() if line.strip())

        # A verified external action owns its completion wording. Suppress the
        # model-authored wrapper entirely so "已发送" cannot survive beside a
        # failed/verifying receipt.
        if authoritative_results:
            return "\n".join(authoritative_results + results)
        if results:
            return cleaned + "\n" + "\n".join(results)
        return cleaned

    def _require_owner_callback(self) -> None:
        if not self.owner_authenticated:
            raise RuntimeError(
                "owner decision requires an authenticated Item/dashboard callback"
            )

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

    def _pending_broadcast_path(self, pending_id: str) -> Path | None:
        pending_id = str(pending_id or "").strip()
        if not re.fullmatch(r"\d+_\d+", pending_id):
            return None
        return self.jarvis_dir / "eigenflux" / "pending_publish" / f"{pending_id}.json"

    def _do_eigenflux_publish(self, raw: str) -> str:
        """Publish one specifically approved pending EigenFlux broadcast."""
        path = self._pending_broadcast_path(parse_params(raw).get("id", ""))
        if path is None or not path.exists():
            return "没有找到这条待广播内容（可能已经处理过了）"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            content = str(data.get("content", "")).strip()
            notes = json.dumps(data.get("notes") or {}, ensure_ascii=False)
            if not content:
                return "广播内容为空，未发送"
            cmd = ["eigenflux", "publish", "--content", content,
                   "--notes", notes, "--accept-reply", "-f", "json"]
            if data.get("url"):
                cmd.extend(["--url", str(data["url"])])
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=30, cwd=str(self.jarvis_dir))
            if result.returncode != 0:
                return f"广播失败，内容仍保留待重试：{(result.stderr or '').strip()[:160]}"
            path.unlink(missing_ok=True)
            self._stamp_publish_state(content, data.get("notes") or {})
            return "✅ 已广播"
        except Exception as e:
            return f"广播失败，内容仍保留待重试：{e}"

    def _stamp_publish_state(self, content: str, notes: dict) -> None:
        """Record a successful publish in publish_state.json.

        Nothing has stamped this file since the confirmation flow replaced
        direct publishing (last stamp 5/29) — so the 2h drafting cooldown in
        eigenflux_publish_pre.sh always passed and the "recent topics, do NOT
        repeat" list went stale. Best-effort: a stamp failure never fails the
        publish itself.
        """
        state_file = self.jarvis_dir / "eigenflux" / "publish_state.json"
        if not isinstance(notes, dict):
            notes = {}
        try:
            state = {}
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
            now = int(time.time())
            state["last_publish_epoch"] = now
            recent = state.get("recent", [])
            recent.append({
                "epoch": now,
                "summary": str((notes or {}).get("summary", ""))[:160],
                "content_preview": content[:120],
            })
            state["recent"] = recent[-30:]
            from core.safety import atomic_write
            atomic_write(state_file, json.dumps(state, ensure_ascii=False))
        except Exception as e:
            print(f"[actions] publish_state stamp failed: {e}", file=sys.stderr)

    def _do_eigenflux_cancel_publish(self, raw: str) -> str:
        """Cancel one specifically selected pending broadcast."""
        path = self._pending_broadcast_path(parse_params(raw).get("id", ""))
        if path is None or not path.exists():
            return "这条广播已经处理过了"
        path.unlink(missing_ok=True)
        return "已取消广播"

    def _do_eigenflux_friend(self, raw: str) -> str:
        """Accept/reject one card-bound request through the verified CLI path."""
        from core.eigenflux_friends import execute_friend_action

        result, failed = execute_friend_action(
            parse_params(raw), root=self.jarvis_dir
        )
        if failed:
            raise RuntimeError(result)
        return result

    def _do_eigenflux_message(self, raw: str) -> str:
        """Send a friend DM through deterministic resolution and read-back.

        ``content_b64`` keeps arbitrary message bodies out of the marker's
        pipe-delimited parameter grammar.  The handler's result, not model
        prose, is the user-visible completion receipt.
        """
        from core.eigenflux_messages import EigenFluxMessenger

        params = parse_params(raw)
        recipient = params.get("recipient", "")
        encoded = params.get("content_b64", "")
        if not recipient or not encoded:
            return "❌ EigenFlux 消息缺少收件人或正文，未发送"
        try:
            content = base64.b64decode(
                encoded.encode("ascii"), validate=True
            ).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return "❌ EigenFlux 消息正文编码无效，未发送"
        try:
            receipt = EigenFluxMessenger(root=self.jarvis_dir).send(
                recipient,
                content,
                repeat_token=params.get("repeat_token", ""),
            )
        except Exception as exc:
            return f"❌ EigenFlux 消息未发送：{exc}"
        try:
            from core.delegation_connectors import (
                project_eigenflux_message_receipt,
            )

            project_eigenflux_message_receipt(
                receipt,
                root=self.jarvis_dir,
            )
        except Exception as exc:
            print(
                f"[actions] delegation receipt projection failed: {exc}",
                file=sys.stderr,
            )
        return receipt.human_text()

    def _do_delegation_confirm(self, raw: str) -> str:
        """Confirm one versioned high-risk Delegation from its unique Item."""
        from core.delegations import DelegationStore
        from core.delegation_reconcile import sync_attention_item

        self._require_owner_callback()
        params = parse_params(raw)
        try:
            store = DelegationStore(root=self.jarvis_dir)
            detail = store.confirm(
                params.get("id", ""),
                expected_version=int(params.get("version", "0")),
                principal_id=params.get("principal", ""),
            )
            sync_attention_item(detail, store=store, send=False)
        except Exception as exc:
            raise RuntimeError(f"委托确认未生效：{exc}") from exc
        return "已确认。系统会按同一契约执行并核验，不需要重复点击。"

    def _do_delegation_cancel(self, raw: str) -> str:
        """Cancel one versioned Delegation and converge its attention Item."""
        from core.delegations import DelegationStore
        from core.delegation_reconcile import sync_attention_item

        self._require_owner_callback()
        params = parse_params(raw)
        try:
            store = DelegationStore(root=self.jarvis_dir)
            detail = store.terminal(
                params.get("id", ""),
                expected_version=int(params.get("version", "0")),
                status="cancelled",
                reason_code="owner_cancelled",
                actor_id="owner",
            )
            sync_attention_item(detail, store=store, send=False)
        except Exception as exc:
            raise RuntimeError(f"委托取消未生效：{exc}") from exc
        return "已取消这个委托。"

    def _do_delegation_retry(self, raw: str) -> str:
        """Resume a versioned Delegation from an explicit recovery decision."""
        from core.delegations import DelegationStore
        from core.delegation_reconcile import sync_attention_item

        self._require_owner_callback()
        params = parse_params(raw)
        try:
            store = DelegationStore(root=self.jarvis_dir)
            detail = store.retry(
                params.get("id", ""),
                expected_version=int(params.get("version", "0")),
                actor_id="owner",
            )
            sync_attention_item(detail, store=store, send=False)
        except Exception as exc:
            raise RuntimeError(f"委托核验没有恢复：{exc}") from exc
        return "已恢复权威核验；外部结果确认前仍不会标记完成。"

    def _do_iteration_approve(self, raw: str) -> str:
        """Approve one evidence-backed L3 proposal and queue it in Taskline."""
        from core.iteration_loop import IterationStore, sync_proposal_item

        self._require_owner_callback()
        proposal_id = parse_params(raw).get("id", "")
        try:
            store = IterationStore(root=self.jarvis_dir)
            proposal = store.review(
                proposal_id,
                approved=True,
                actor="owner",
                queue=True,
            )
            sync_proposal_item(proposal, store=store, send=False)
        except Exception as exc:
            raise RuntimeError(f"改进项没有进入研发队列：{exc}") from exc
        return f"已进入研发队列（Taskline {proposal['taskline_id'][:8]}）。"

    def _do_iteration_reject(self, raw: str) -> str:
        """Reject one L3 proposal without producing an engineering task."""
        from core.iteration_loop import IterationStore, sync_proposal_item

        self._require_owner_callback()
        proposal_id = parse_params(raw).get("id", "")
        try:
            store = IterationStore(root=self.jarvis_dir)
            proposal = store.review(
                proposal_id,
                approved=False,
                actor="owner",
                reason="owner_rejected",
                queue=False,
            )
            sync_proposal_item(proposal, store=store, send=False)
        except Exception as exc:
            raise RuntimeError(f"改进项没有关闭：{exc}") from exc
        return "已记录：这个改进项不进入研发队列。"

    # ── Heartbeat ──

    def _do_heartbeat(self, raw: str) -> str:
        # The trigger file CONTENT names the task to force (REQ-37). A bare
        # [ACTION:heartbeat] used to force the ENTIRE 32-task roster — 32
        # full-roster storms on 6/12 alone, weekly tasks re-running within
        # hours, batch cap deferring 18-19 tasks per cycle. Default to
        # intention-check (the only task a chat reply plausibly needs fresh);
        # [ACTION:heartbeat|task=<name>] scopes explicitly; task=all keeps
        # the legacy full-roster behavior (10-min cooldown enforced in the
        # loop).
        p = parse_params(raw)
        task = p.get("task", "") or "intention-check"
        # APPEND one line per force (red-team fix): the old write_text both
        # (a) truncated-then-wrote → torn reads ('' → full-roster storm), and
        # (b) last-writer-wins → a concurrent admin 'Run Now' or chat trigger
        # was silently dropped. An O_APPEND single small write is atomic and
        # loss-free; heartbeat_loop drains every line per tick.
        with self.heartbeat_trigger_path.open("a") as f:
            f.write(task + "\n")
        return ""

    # ── Calendar ──

    def _get_primary_calendar_id(self) -> str:
        """Resolve the user's primary calendar ID, cached for this instance.

        The [ACTION:calendar_update|...] / [ACTION:calendar_delete|...]
        marker contract (core/prompt.py) only ever gives the model event_id —
        it was never asked for calendar_id, so `events patch`/`events
        delete` (unlike `+create`/`+update`, which default calendar-id to
        "primary" internally) fail every call with "missing required path
        parameter: calendar_id" unless we resolve it ourselves. Returns ""
        on failure; callers must treat that as a failed action, not silently
        omit the flag.
        """
        if self._primary_calendar_id is not None:
            return self._primary_calendar_id
        result = _run_cmd(["lark-cli", "calendar", "calendars", "primary",
                           "--as", "user", "--format", "json"],
                          log_file=self.log_file)
        if result.startswith("FAILED"):
            return ""
        try:
            calendars = json.loads(result).get("data", {}).get("calendars") or []
            cal_id = str(calendars[0].get("calendar", {}).get("calendar_id", "")) if calendars else ""
        except (ValueError, IndexError, AttributeError):
            cal_id = ""
        self._primary_calendar_id = cal_id
        return cal_id

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
            if not r.startswith("FAILED"):
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

        if result.startswith("FAILED"):
            return f"❌ 日程创建失败: {title}（{result[8:].strip() or '未知错误'}）"
        return conflict_note + f"✅ 已创建日程: {title} ({start} → {end})"

    def _do_calendar_update(self, raw: str) -> str:
        p = parse_params(raw)
        event_id, field, value = p.get("event_id", ""), p.get("field", ""), p.get("value", "")
        if not (event_id and field and value):
            return ""
        # Optional escape hatch for a non-primary calendar; the documented
        # marker contract never asks the model for this, so it defaults to
        # the resolved primary calendar (see _get_primary_calendar_id).
        calendar_id = p.get("calendar_id", "") or self._get_primary_calendar_id()
        if not calendar_id:
            return f"❌ 日程更新失败: {event_id} ({field})（无法解析主日历 ID）"

        field_map = {
            "summary": {"summary": value},
            "start": {"start_time": {"timestamp": value}},
            "end": {"end_time": {"timestamp": value}},
        }
        data = json.dumps(field_map.get(field, {"description": value}))
        result = _run_cmd([
            "lark-cli", "calendar", "events", "patch", "--as", "user",
            "--calendar-id", calendar_id,
            "--params", json.dumps({"event_id": event_id}),
            "--data", data,
        ], log_file=self.log_file)

        if result.startswith("FAILED"):
            return f"❌ 日程更新失败: {event_id} ({field})（{result[8:].strip() or '未知错误'}）"
        return f"✅ 已更新日程: {field} → {value}"

    def _do_calendar_delete(self, raw: str) -> str:
        p = parse_params(raw)
        event_id = p.get("event_id", "")
        title = p.get("title", event_id)
        if not event_id:
            return ""
        calendar_id = p.get("calendar_id", "") or self._get_primary_calendar_id()
        if not calendar_id:
            return f"❌ 日程删除失败: {title}（无法解析主日历 ID）"
        result = _run_cmd(["lark-cli", "calendar", "events", "delete",
                           "--as", "user", "--calendar-id", calendar_id,
                           "--event-id", event_id],
                          log_file=self.log_file)
        if result.startswith("FAILED"):
            return f"❌ 日程删除失败: {title}（{result[8:].strip() or '未知错误'}）"
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
        if result.startswith("FAILED"):
            return f"❌ 任务创建失败: {title}（{result[8:].strip() or '未知错误'}）"
        return f"✅ 已创建任务: {title}"

    def _do_task_complete(self, raw: str) -> str:
        p = parse_params(raw)
        task_id = p.get("task_id", "")
        if not task_id:
            return ""
        result = _run_cmd(["lark-cli", "task", "+complete", "--as", "user",
                           "--task-id", task_id], log_file=self.log_file)
        if result.startswith("FAILED"):
            return f"❌ 任务完成标记失败: {task_id}（{result[8:].strip() or '未知错误'}）"
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

        # category drives closure intensity. Auto-classify when the author omits
        # it (healing wins ambiguous health/learning ties; ambiguous → none =
        # no follow-up, behaves as today, bounds blast radius).
        category = (p.get("category", "").strip()
                    or _auto_category(p.get("tags", ""), name, p.get("prompt", "")))

        try:
            from core.intent_lifecycle import create_intent
            iid = create_intent(
                name=name, trigger_type=trigger_type, trigger_config=trigger_config,
                prompt=p.get("prompt", name), purpose=p.get("purpose", ""),
                tags=[t.strip() for t in p.get("tags", "").split(",") if t.strip()],
                priority=int(p.get("priority", "5")) if p.get("priority", "5").isdigit() else 5,
                source="agent", action_type=p.get("action", "notify"),
                category=category, input_ctx=p.get("input", ""),
                decision=p.get("decision", ""), closure_question=p.get("close", ""),
                context={"conv_key": os.environ.get("JV_CONV_KEY", "")},
            )
            return f'✅ Intent "{name}" created (id: {iid}, cat: {category})'
        except Exception as e:
            return f"❌ Intent 创建失败: {e}"

    def _do_intent_close(self, raw: str) -> str:
        """Record a closure result on an awaiting intent. Both a marker and the
        synchronous `do intent_close` CLI verb (§7 verify-the-write-path).

        result= may contain spaces. The `do` CLI joins argv with '|' before the
        handler sees it, so we rebuild result from its segment to the end and map
        '|'→' '. result MUST be the last key. (Marker path: result is a single
        spaces-preserving segment, so the same logic is a no-op there.)
        """
        p = parse_params(raw)
        iid = str(p.get("id", "")).strip()
        if not iid:
            return ""
        outcome = p.get("outcome", "done").strip()
        # via must appear BEFORE result= in the params (everything after
        # result= is folded into the result text below). Memorial closure
        # buttons pass via=button so closure telemetry keeps distinguishing
        # one-tap from CLI/marker closures.
        via = str(p.get("via", "")).strip() or "cli"
        result = ""
        segs = raw.split("|")
        for i, seg in enumerate(segs):
            if seg.strip().startswith("result="):
                result = "|".join(segs[i:]).split("=", 1)[1].replace("|", " ").strip()
                break
        try:
            from core.intent_closure import record_closure
            ok = record_closure(iid, outcome=outcome, result=result, via=via)
            return "Closure recorded" if ok else "Intent not found or already closed"
        except Exception:
            return "FAILED"

    def _do_intent_cancel(self, raw: str) -> str:
        p = parse_params(raw)
        try:
            from core.intent_lifecycle import cancel_intent
            ok = cancel_intent(p.get("id", ""), p.get("reason", ""))
            return "Intent cancelled" if ok else "Intent not found or already done"
        except Exception:
            return "FAILED"

    def _do_intent_list(self, raw: str) -> str:
        try:
            from core.intent_lifecycle import list_intents
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

    ap = ActionProcessor(
        jarvis_dir=jarvis_dir,
        memory_dir=os.environ.get("MEMORY_DIR", "memory"),
        jobs_dir=os.environ.get("JV_JOBS_DIR", "jobs"),
        log_file=os.environ.get("JV_LOG_FILE", ""),
    )

    # ── Synchronous single-action mode (for in-turn Bash tool calls) ──
    #   python3 -m core.actions do <action_type> [key=val ...]
    # Runs exactly ONE action through the SAME _do_* handler the marker path
    # uses (single source of truth) and prints its result immediately, so the
    # agent can VERIFY the outcome in the same turn instead of firing a
    # fire-and-forget [ACTION:...] marker it can never observe.
    #   e.g. python3 -m core.actions do intent_cancel id=int_xxx reason=junk
    #        python3 -m core.actions do calendar_create title=Sync start=... end=...
    if len(sys.argv) >= 3 and sys.argv[1] == "do":
        action_type = sys.argv[2]
        params_raw = "|".join(sys.argv[3:])  # "id=x reason=y" → "id=x|reason=y"
        handler = getattr(ap, f"_do_{action_type}", None)
        if handler is None:
            print(f"ERROR: unknown action '{action_type}'", file=sys.stderr)
            sys.exit(2)
        result = handler(params_raw)
        print(result if result else f"OK: {action_type} ran (handler returned no text)")
        sys.exit(0)

    # ── Legacy marker-processing mode (bot.sh post-hook) ──
    reply = os.environ.get("JV_REPLY", "")
    if not reply:
        reply = sys.stdin.read()
    print(ap.process(reply))
