"""Heartbeat runner — parse HEARTBEAT.md, schedule tasks, orchestrate Claude.

The heartbeat is the core loop that drives Jarvis. Every N seconds it checks
which tasks are due, runs their pre-scripts to gather data, batches them into
a single Claude call, and routes responses through post-scripts.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .claude_bin import resolve_claude_bin
from .jsonl import append_jsonl
from .log import log as _structured_log
from .memory import load_tiered_memory
from .prompt_experiments import choose_variant, inject_variant
from .safety import parse_json_response
from .sched_events import emit as sched_emit
from .task_protocol import TaskState
from .timeutil import now_local_str


# REQ-80: failed task_finish events used to carry no error information at all
# (866 failed events, zero diagnosable). The excerpt below rides along on the
# emit; it must never leak credentials into sched_events.jsonl (persistent,
# survives /tmp cleanup) and must never raise.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+\S+"
    r"|\b(?:token|secret|api_key|password)\b\s*[=:]\s*\S+)",
    re.IGNORECASE,
)


def _error_excerpt(text: str, limit: int = 500) -> str:
    """First non-empty line of an error surface, secrets redacted, truncated.

    Never raises — this runs inside scheduler emit paths where a logging
    helper must not be able to break scheduling.
    """
    try:
        if not text:
            return ""
        line = next((ln.strip() for ln in str(text).splitlines()
                     if ln.strip()), "")
        return _SECRET_RE.sub("[redacted]", line)[:limit]
    except Exception:
        return ""


def parse_interval(s: str) -> int:
    """Parse '10m', '2h', '1d' to seconds."""
    s = s.strip()
    m = re.match(r"(\d+)\s*(s|m|h|d)", s)
    if not m:
        return 600
    val, unit = int(m.group(1)), m.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _is_dangling_placeholder(text: str) -> bool:
    """True if a heartbeat teases a hook but never delivers the payload.

    The 2026-06-02 EigenFlux 撞名 card ended on a bare '...' line: the
    generator wrote a hook ("…just found a same-name project:") and the body
    after it was never filled in. Such "wolf!" messages create anxiety with
    zero information, so the send gate drops them rather than deliver a
    half-baked card.

    Detection is deliberately narrow to avoid false positives: the LAST
    non-empty line must consist solely of an ellipsis (ASCII '...', the '…'
    char, or fullwidth '．．．'). A real message almost never ends on a line
    that is nothing but dots.
    """
    last_line = next(
        (ln.strip() for ln in reversed(text.splitlines()) if ln.strip()), ""
    )
    return bool(re.fullmatch(r"\.{3,}|…+|．{3,}", last_line))


def _has_idle_sentinel(text: str) -> bool:
    """True if HEARTBEAT_OK appears as a standalone line in the message.

    The exact-match check (`raw.strip() == "HEARTBEAT_OK"`) only fires when the
    ENTIRE response is the sentinel. But the model sometimes writes its reasoning
    ("this is test noise, not worth notifying Pascal") and THEN emits
    HEARTBEAT_OK on its own line — the reasoning leaks into output and the
    half-message gets delivered as a card (2026-06-08: a 🎯 Intent card reached
    Pascal despite ending in HEARTBEAT_OK). When the sentinel is present the
    model has decided nothing needs attention; the surrounding text is leaked
    scratch work, so the whole message is dropped.

    Standalone-line match (not bare substring) avoids dropping a legitimate
    message that merely mentions the token in prose.
    """
    return any(ln.strip() == "HEARTBEAT_OK" for ln in text.splitlines())


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
                        "pre": "", "post": "", "prompt": "",
                        "heavy": False, "timeout": None}
        elif current:
            if line.startswith("- interval:"):
                current["interval"] = parse_interval(line.split(":", 1)[1])
            elif line.startswith("- pre:"):
                current["pre"] = line.split(":", 1)[1].strip()
            elif line.startswith("- post:"):
                current["post"] = line.split(":", 1)[1].strip()
            elif line.startswith("- heavy:"):
                current["heavy"] = line.split(":", 1)[1].strip().lower() in ("true", "yes", "1")
            elif line.startswith("- timeout:"):
                try:
                    current["timeout"] = int(line.split(":", 1)[1].strip())
                except ValueError:
                    current["timeout"] = None
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
        "memory-consolidate": 600,
    }

    # Memory pipeline tasks — only one per cycle to prevent races
    PIPELINE_TASKS = {"memory-hourly", "memory-daily", "memory-weekly"}

    # Tasks exempt from batch cap — they run on every cycle regardless.
    # These are infrastructure tasks that must stay fresh for others to work.
    # intention-check (REQ-32): the only task whose pre-script makes stateful
    # user-facing commitments; batch-cap starvation was losing whole cron
    # occurrences (configured 60s, observed median gap 31min). Its pre is a
    # sub-second sqlite pass (296/304 runs empty), so exemption costs nothing.
    PRIORITY_TASKS = {"calendar-sync", "memory-hourly", "activity-log", "cross-session-sync",
                       "eigenflux-friends", "intention-check"}

    # Tier 0: tasks that bypass Claude entirely (pre→post direct pipe).
    # ONLY for tasks where the pre-script already produces the final output
    # and the post-script just writes it to a file. Tasks that need Claude
    # to summarize/reason/index MUST NOT be here.
    TIER0_TASKS = {"calendar-sync"}  # pre-script produces formatted calendar

    # Permanently silent housekeeping tasks (behavioral_rules.md: "daily-plan /
    # self-diagnostic / thinking-review 视为 autonomous 内务：长期零响应，
    # 永久静默、绝不 surface"). Their pre/post scripts still run — state files
    # and JSONL logs are the product — but their output NEVER reaches the user:
    # not as a direct send, not via the batch/night queue. Enforced here at the
    # exact task→message pairing point (see _collect_output) plus a delivery-
    # layer backstop in core/heartbeat_loop.py (SILENT_SOURCES). To silence
    # another task, add its name here — no logic changes needed.
    SILENT_TASKS = {"daily-plan", "self-diagnostic", "thinking-review"}

    # Tasks whose PRE-script mutates state that the POST-script must always
    # get a chance to reconcile (REQ-30). intention-check's pre marks intents
    # 'triggered' and writes the inflight manifest; if the Claude call dies
    # (HEARTBEAT_OK / empty / killed / unparseable envelope), the post is
    # still invoked with stdin='__NO_ENVELOPE__' so the manifest resolves
    # deterministically instead of stranding intents until they expire.
    ACK_REQUIRED_TASKS = {"intention-check"}

    # Max tasks to batch into a single Claude call.
    # Prevents timeout when many tasks are due simultaneously (e.g. after restart).
    # Remaining tasks will be picked up in the next cycle.
    # NOTE: PRIORITY_TASKS bypass this cap entirely.
    MAX_BATCH_SIZE = 4

    # REQ-79.1: a failed SHARED Claude call (the whole batch died on timeout /
    # network / nonzero exit before producing a byte) is infrastructure
    # trouble, not any one task's fault — charging it to per-task circuit
    # breakers tripped innocent tasks (7/1: batch=6 all failed at 21:37→23:57,
    # 3 circuits tripped the same second; 7/2: a DNS outage put checkin into
    # circuit_open). Instead a shared streak lives in
    # state["__shared_call__"] = {consecutive_failures, last_failure,
    # backoff_until} and, at SHARED_FAIL_THRESHOLD consecutive failed calls,
    # the whole non-Tier0 roster backs off (doubling 5min → 60min cap) rather
    # than re-dialing a dead API every cycle. DELIBERATELY a separate key from
    # __envelope_parse__: envelope breakage will drive batch-clamping
    # (REQ-79.2) while call failure drives backoff — sharing one counter would
    # let an API outage falsely trigger the clamp. The 3600s cap sits far
    # below the 6h cron-staleness window, so backoff can never rot an
    # occurrence into a stale-skip.
    SHARED_FAIL_THRESHOLD = 3   # consecutive failed shared calls before backoff
    SHARED_BACKOFF_BASE = 300   # first backoff: 5 min
    SHARED_BACKOFF_MAX = 3600   # cap: 60 min (<< 6h CRON_STALENESS)

    # Minimum interval between force-triggered runs of the same task, in seconds.
    # Prevents rapid Lark session rotations from hammering memory-hourly.
    FORCE_COOLDOWN_SECONDS = 60

    # Heavy tasks (deep research, multi-repo audit, night deep work) run SOLO —
    # their own Claude call with an extended timeout — instead of being crammed
    # into the shared multi-task JSON envelope. DeerFlow-style fan-out isolation
    # applied to the heartbeat: (1) a slow/failed heavy task can't poison the
    # lightweight batch's combined envelope, and (2) the batch's 300s budget
    # can't starve a task that needs to spawn subagents and wait. Opt in per
    # task with `- heavy: true` in HEARTBEAT.md; tune the budget with
    # `- timeout: <seconds>`. At most HEAVY_MAX_PER_CYCLE run per cycle (stalest
    # first) so a long heavy call never starves the resident loop — unrun heavy
    # tasks keep their state untouched and are picked up next cycle.
    HEAVY_DEFAULT_TIMEOUT = 900
    HEAVY_MAX_PER_CYCLE = 1

    def __init__(self, jarvis_dir: str | Path, heartbeat_file: str | Path,
                 state_file: str | Path, memory_dir: str | Path,
                 model: str = "opus", persona: str = "Jarvis",
                 work_dir: str | Path | None = None,
                 idle_judge: bool = True, claude_timeout: int = 300,
                 claude_bin: str = ""):
        self.jarvis_dir = Path(jarvis_dir)
        self.heartbeat_file = Path(heartbeat_file)
        self.state_file = Path(state_file)
        self.memory_dir = Path(memory_dir)
        self.model = model
        self.persona = persona
        self.work_dir = Path(work_dir) if work_dir else self.jarvis_dir
        # Max seconds for a single heartbeat Claude call. Configurable so tasks
        # can fan out subagents and wait; see config claude.heartbeat_timeout.
        self.claude_timeout = claude_timeout
        # Cheap-model idle-noise second net. On in prod; tests pass False to
        # avoid real network calls. See _judge_is_idle_noise.
        self.idle_judge = idle_judge
        # Resolve the `claude` binary ONCE here, not at every call. Bare "claude"
        # on PATH is fragile under launchd (minimal PATH omits ~/.local/bin) —
        # the 2026-06-15 brain-death incident. See core/claude_bin.py.
        self._claude_bin = resolve_claude_bin(claude_bin)
        self._cid = ""  # cycle_id, set per run_cycle invocation
        self._tasks_cache = None   # cached parse result
        self._tasks_mtime = 0.0    # mtime when cache was built
        self._cycle_prompt_variants: dict[str, dict[str, str]] = {}
        # True iff the LAST claude_call ended in TimeoutExpired — lets the
        # cycle distinguish task_timeout from a generic failed call when
        # writing the scheduler event log (both return "" from claude_call).
        self._call_timed_out = False
        # REQ-80: error surface of the LAST claude_call (stderr/stdout first
        # line, or the exception text). "" on success/kill. Consumed by the
        # failed/parse_failed emit sites via _error_excerpt().
        self._last_call_error = ""

    def _log(self, msg: str, **kwargs):
        """Structured log with cycle_id for correlation."""
        _structured_log("heartbeat", msg, cycle=self._cid, **kwargs)

    def _event(self, event: str, task: str = "", **fields):
        """Scheduler event-log entry, correlated by cycle_id (run_id).

        sched_emit never raises by contract — a logging failure must not
        affect the scheduling path.
        """
        sched_emit(self.jarvis_dir, event, task=task, run_id=self._cid, **fields)

    def _ack_failed_posts(self, runnable: list) -> None:
        """Run ACK-required tasks' post-scripts with '__NO_ENVELOPE__' (REQ-30).

        Called on every path where Claude's reply never reaches the normal
        post routing (HEARTBEAT_OK early return, empty/failed call, killed,
        envelope parse failure). For intention-check this resolves the
        inflight manifest deterministically — retry or expire-with-breach —
        instead of stranding intents in 'triggered' until they silently die.
        Output is intentionally discarded (state reconciliation, not content).
        """
        for task in runnable:
            if task["name"] in self.ACK_REQUIRED_TASKS and task.get("post"):
                try:
                    self.run_script(task["post"], stdin_data="__NO_ENVELOPE__")
                    self._event("task_skip", task=task["name"],
                                reason="no_envelope_acked")
                except Exception as e:
                    self._log(f"ACK post for {task['name']} failed: {e}",
                              level="warn")

    def _run_solo_task(self, task: dict, task_data: dict, state: dict, now: float,
                       user_messages: list, producing_tasks: list) -> None:
        """Run one heavy task in its own isolated Claude call (DeerFlow fan-out).

        Mirrors the single-task path (direct response → post, no JSON envelope)
        but with the task's own extended timeout, so deep-research / multi-repo
        audit / night-deep-work tasks can spawn subagents and wait without
        sharing the batch's budget or its combined-envelope failure mode. Owns
        this task's full state transition (ok/idle/failed/timeout/killed) so the
        downstream batch path never sees it.
        """
        name = task["name"]
        timeout = task.get("timeout") or self.HEAVY_DEFAULT_TIMEOUT
        variant = choose_variant(self.memory_dir, name, now=now)
        parts = [f"[HEARTBEAT — heavy task: {name}]"]
        parts.append(inject_variant(task["prompt"].strip(), variant))
        data = task_data.get(name, "")
        if data:
            parts.append(f"\nDATA:\n{data}")
        parts.append("\nReturn the task's requested format directly. "
                     "If nothing needs attention, reply with exactly: HEARTBEAT_OK")
        prompt = "\n".join(parts)

        self._log(f"Calling Claude SOLO for heavy task {name} (timeout {timeout}s)...")
        self._event("task_spawn", task=name, heavy=True)
        t0 = time.time()
        raw = self.claude_call(prompt, timeout=timeout)
        dur = round(time.time() - t0, 2)

        ts = TaskState.from_dict(state.get(name, {}))
        ts.last_run = now

        if raw == "__KILLED__":
            # Restart/shutdown — not a task failure, no circuit penalty.
            ts.last_status = "killed"
            state[name] = ts.to_dict()
            self._event("task_finish", task=name, status="killed",
                        duration_s=dur, heavy=True)
            self._ack_failed_posts([task])
            return

        if not raw:
            # Timeout / network / nonzero — record failure with breaker.
            # Fast-retry like the batch path (REQ: heavy tasks were pushing
            # last_run to now on every failure, so a single transient network
            # blip on a 24h-interval task like pgc-improvement meant a full
            # day's silence instead of a few minutes — see 2026-07-02 incident,
            # ConnectionRefused/401 during a flaky-network window stalled
            # pgc-improvement/engagement-analyze/eigenflux-preinstall for days).
            retry_delay = min(300, task["interval"])
            ts.last_run = now - task["interval"] + retry_delay
            ts.last_status = "timeout" if self._call_timed_out else "failed"
            tripped = ts.circuit.record_failure()
            if name in self.PRIORITY_TASKS and ts.circuit.is_open:
                ts.circuit.disabled_until = 0
                tripped = False
            state[name] = ts.to_dict()
            if self._call_timed_out:
                self._event("task_timeout", task=name, duration_s=dur,
                            timeout_s=timeout, heavy=True)
            else:
                self._event("task_finish", task=name, status="failed",
                            duration_s=dur, heavy=True,
                            error=_error_excerpt(self._last_call_error))
            self._ack_failed_posts([task])
            if tripped:
                self._log(f"Circuit TRIPPED for heavy task: {name}", level="warn")
                self._event("circuit_tripped", task=name)
            return

        if raw.strip() == "HEARTBEAT_OK":
            ts.last_success = now
            ts.last_status = "idle"
            ts.circuit.record_success()
            state[name] = ts.to_dict()
            self._event("task_finish", task=name, status="idle",
                        duration_s=dur, heavy=True)
            self._ack_failed_posts([task])
            return

        # Success — route through post-script exactly like the n==1 path.
        if task["post"]:
            post_output = self.run_script(task["post"], stdin_data=raw)
            if post_output:
                self._collect_output(name, post_output, user_messages, producing_tasks)
        elif _has_idle_sentinel(raw):
            self._log(f"Heavy task idle (HEARTBEAT_OK in body): {name}")
        else:
            self._collect_output(name, raw, user_messages, producing_tasks)

        ts.last_success = now
        ts.last_status = "ok"
        ts.circuit.record_success()
        state[name] = ts.to_dict()
        self._event("task_finish", task=name, status="ok", duration_s=dur, heavy=True)

    def _collect_output(self, task_name: str, message: str,
                        user_messages: list, producing_tasks: list):
        """Stage a task's output for delivery — unless the task is silent.

        This is the ONLY place with an exact task→message pairing (downstream
        the cycle output is one merged string plus a comma-separated source
        sidecar), so SILENT_TASKS enforcement lives here: the output is logged
        and dropped, never appended for delivery.
        """
        if task_name in self.SILENT_TASKS:
            self._log(f"Silent task {task_name}: output suppressed "
                      f"(log-only, never delivered): {message[:80]!r}")
            self._event("task_skip", task=task_name, reason="silent_output")
            # "Logs are the product": daily-plan persists its own JSONL via its
            # post-script, but thinking-review / self-diagnostic have no log of
            # their own — without this archive their full output would vanish
            # (jarvis.log keeps only the 80-char prefix above). Capped rolling
            # file; called only under the cycle flock, so append_jsonl's
            # read-modify-write is safe.
            try:
                append_jsonl(self.jarvis_dir / "silent_outputs.jsonl",
                             {"ts": now_local_str("%Y-%m-%d %H:%M"),
                              "task": task_name, "text": message},
                             keep_last=100)
            except Exception as e:
                self._log(f"silent_outputs archive failed: {e}")
            return
        user_messages.append(message)
        producing_tasks.append(task_name)

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

    def load_interval_overrides(self) -> dict:
        """Auto-tuned intervals from engagement-analyze (W3.1).

        Kept in a sidecar file instead of heartbeat_state.json: the post-hook
        runs as a child process mid-cycle, so anything it wrote into the state
        file was clobbered when run_cycle saved its own in-memory copy at the
        end of the cycle — which is why two months of "reduce frequency"
        suggestions never took effect.
        """
        f = self.jarvis_dir / "interval_overrides.json"
        try:
            data = json.loads(f.read_text())
            return {k: int(v) for k, v in data.items() if int(v) > 0}
        except (OSError, ValueError, TypeError):
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
        self._last_script_outcome = "ok"
        if not full_path.exists():
            self._last_script_outcome = "missing"
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
            # stderr is logged regardless of exit code (REQ-35): post-scripts
            # exit 0 by design while reporting failures on stderr — under the
            # old returncode guard the exact failures killing half the fired
            # intents produced zero log lines.
            if result.stderr.strip():
                self._log(f"Script {script_path} stderr: {result.stderr.strip()[:300]}",
                          level="warn" if result.returncode != 0 else "info")
            if result.returncode != 0:
                self._last_script_outcome = "nonzero"
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            # Distinguishable from "legitimately nothing to do" (REQ-51): the
            # caller feeds pre_timeout/pre_error into the circuit breaker and
            # the truth watermark, so a chronically failing pre-script can
            # never look like a healthy quiet channel again.
            self._last_script_outcome = "timeout"
            self._log(f"Script {script_path} timed out (60s)", level="warn")
            return ""
        except Exception as e:
            self._last_script_outcome = "error"
            self._log(f"Script {script_path} error: {e}", level="warn")
            return ""

    def _openai_fallback_call(self, system_prompt: str, prompt: str) -> str:
        """Last-resort heartbeat responder when Claude-compatible routes are quota-blocked."""
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if os.environ.get("OPENAI_FALLBACK_ENABLED", "true") != "true" or not api_key:
            return ""
        try:
            from core.openai_fallback import call_openai, extract_text

            model = (
                os.environ.get("OPENAI_FALLBACK_MODEL")
                or os.environ.get("OPENAI_MODEL")
                or "gpt-5.2"
            )
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            timeout = int(os.environ.get("OPENAI_FALLBACK_TIMEOUT", "120"))
            max_output_tokens = int(os.environ.get("OPENAI_FALLBACK_MAX_OUTPUT_TOKENS", "4096"))
            user_agent = os.environ.get("OPENAI_USER_AGENT", "")
            instructions = (
                "You are Jarvis running a heartbeat cycle through an OpenAI-compatible "
                "fallback because every Claude-compatible provider hit an account/model "
                "limit. You cannot use local tools here. Use only the task prompt and "
                "DATA already included by pre-scripts. Return the exact requested "
                "heartbeat format, JSON envelope, or HEARTBEAT_OK."
            )
            if system_prompt.strip():
                instructions = f"{instructions}\n\nPrimary heartbeat system prompt:\n{system_prompt}"
            payload = {
                "model": model,
                "instructions": instructions,
                "input": prompt,
                "max_output_tokens": max_output_tokens,
            }
            self._log(f"Calling OpenAI heartbeat fallback model={model}")
            text = extract_text(call_openai(payload, api_key, base_url, timeout, user_agent))
            if text:
                self._log(f"OpenAI heartbeat fallback succeeded ({len(text)} chars)")
            return text.strip()
        except Exception as e:
            self._log(f"OpenAI heartbeat fallback failed: {e}", level="warn")
            return ""

    def claude_call(self, prompt: str, timeout: int | None = None) -> str:
        """Call Claude with memory injection, no session persistence.

        timeout: override the per-call subprocess budget (seconds). Heavy tasks
        that fan out subagents pass a longer budget; None uses self.claude_timeout.
        """
        call_timeout = timeout or self.claude_timeout
        memory = load_tiered_memory(self.memory_dir)
        now_ts = now_local_str("%Y-%m-%d %H:%M %A")
        system_prompt = f"""You are {self.persona}, a personal AI assistant and life mentor.
Current time: {now_ts}
You have access to the user's memory below. Use it to personalize your responses.

## Acting
- For heavy or parallelizable work, you may spawn subagents with the Task/Agent
  tool — they block and return results to you, so you can fan out, wait, and
  synthesize within this run.
- Before claiming a Jarvis action is done, verify it via Bash with the synchronous
  CLIs (run from JARVIS_DIR), then report the observed result:
    python3 -m core.intentions list|due|awaiting|get <id>|cancel <id>|close <id> [outcome] [result...]|delete <id>|stats|purge <status>
    python3 -m core.actions do <type> key=val ...   (e.g. do intent_close id=<parent> outcome=done result=<一句>)

{memory}"""

        self._call_timed_out = False
        self._last_call_error = ""
        attempted: set[str] = set()
        model = self.model
        use_backup = False
        backup_tried = False
        # Sticky provider gate (2026-07-07 spend-limit incident): when the
        # primary account is known-exhausted, start on the backup provider
        # instead of re-probing primary every cycle (2 doomed subprocess
        # spawns per call, 114 wasted error lines in one evening). 'probe'
        # means this call was elected to try primary once and report back.
        gate_state = "primary"
        try:
            from core.model_fallback import gate as _provider_gate
            gate_state = _provider_gate(self.jarvis_dir)
        except Exception:
            gate_state = "primary"
        if (gate_state == "backup"
                and os.environ.get("CLAUDE_BACKUP_ENABLED", "true") == "true"
                and os.environ.get("CLAUDE_BACKUP_AUTH_TOKEN")
                and os.environ.get("CLAUDE_BACKUP_BASE_URL")):
            use_backup = True
            backup_tried = True
        try:
            while True:
                cmd = [
                    self._claude_bin,
                    "--dangerously-skip-permissions",
                    "--no-session-persistence",
                    "--system-prompt", system_prompt,
                    "--disable-slash-commands",
                    "-p", prompt,
                ]
                if model:
                    cmd.extend(["--model", model])
                provider = "backup" if use_backup else "primary"
                attempted.add(f"{provider}:{model or ''}")
                env = None
                if use_backup:
                    env = os.environ.copy()
                    env["ANTHROPIC_AUTH_TOKEN"] = os.environ.get(
                        "CLAUDE_BACKUP_AUTH_TOKEN", "")
                    env["ANTHROPIC_BASE_URL"] = os.environ.get(
                        "CLAUDE_BACKUP_BASE_URL", "")
                self._log(
                    f"Calling Claude heartbeat provider={provider} model={model or '(default)'}"
                )
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=call_timeout, stdin=subprocess.DEVNULL,
                    cwd=str(self.work_dir),
                    env=env,
                    start_new_session=True,  # isolate from parent process group signals
                )
                if result.returncode == 0:
                    if gate_state != "primary" and not use_backup:
                        # Primary answered while the outage flag was set (we
                        # were the elected prober, or backup env is missing)
                        # — reopen it for every process.
                        try:
                            from core.model_fallback import clear as _gate_clear
                            _gate_clear(self.jarvis_dir)
                        except Exception:
                            pass
                    return result.stdout.strip()

                # warn, not info: the 7/7 spend-limit outage sat at level=info
                # for 6.5h — 216 lines nobody was ever alerted to. (This also
                # logs the 137/143 infra kills below at warn — acceptable.)
                self._log(f"Claude exited with code {result.returncode}",
                          level="warn")
                err_text = "\n".join(
                    s for s in (result.stderr.strip(), result.stdout.strip()) if s
                )
                self._last_call_error = err_text
                if err_text:
                    self._log(f"Claude error output: {err_text[:300]}",
                              level="warn")
                # Exit 143 = killed by SIGTERM (128+15). This is an infrastructure
                # event (restart/shutdown), not a task failure. Return a sentinel
                # so run_cycle doesn't punish tasks via circuit breaker.
                if result.returncode in (137, 143):  # SIGKILL=137, SIGTERM=143
                    self._last_call_error = ""  # infrastructure, not an error
                    return "__KILLED__"

                try:
                    from core.model_fallback import (fallback_for_stderr,
                                                     is_model_error,
                                                     is_spend_limit)
                    nxt = fallback_for_stderr(model or "", err_text)
                    model_problem = is_model_error(err_text)
                    spend_limited = is_spend_limit(err_text)
                except Exception:
                    nxt = None
                    model_problem = False
                    spend_limited = False
                if spend_limited and not use_backup:
                    # Account-wide: persist it so EVERY process (bot replies,
                    # background jobs, next cycles) skips the doomed primary.
                    # trip() also pages Pascal once (6h cooldown) through the
                    # daemon's Claude-independent dead-letter channel.
                    try:
                        from core.model_fallback import trip as _gate_trip
                        _gate_trip("spend_limit", self.jarvis_dir)
                    except Exception:
                        pass
                if nxt and f"{provider}:{nxt}" not in attempted:
                    self._log(
                        f"Retrying Claude heartbeat with {provider} fallback model: {nxt}")
                    model = nxt
                    continue
                if (not use_backup and not backup_tried
                        and os.environ.get("CLAUDE_BACKUP_ENABLED", "true") == "true"
                        and os.environ.get("CLAUDE_BACKUP_AUTH_TOKEN")
                        and os.environ.get("CLAUDE_BACKUP_BASE_URL")
                        and model_problem):
                    backup_tried = True
                    use_backup = True
                    model = self.model
                    self._log("Retrying Claude heartbeat with backup provider")
                    continue
                # `or use_backup`: an auth/network error from the backup relay
                # matches no model-error signature (kept tight after the
                # red-team fix), but once we're on backup, primary is already
                # known-dead — dead-ending here would silence the whole
                # heartbeat even though the OpenAI route works.
                if model_problem or use_backup:
                    fallback = self._openai_fallback_call(system_prompt, prompt)
                    if fallback:
                        return fallback
                # Nonzero Claude output is an error surface, not user content.
                return ""
        except subprocess.TimeoutExpired:
            self._call_timed_out = True
            self._last_call_error = f"claude call timed out ({call_timeout}s)"
            self._log(f"Claude call timed out ({call_timeout}s)")
            return ""
        except FileNotFoundError:
            self._last_call_error = "claude CLI not found"
            self._log("Claude CLI not found — is it installed?")
            return ""
        except Exception as e:
            self._last_call_error = str(e)
            self._log(f"Claude error: {e}")
            return ""

    def _judge_is_idle_noise(self, message: str) -> bool:
        """Cheap-model second net for leaked idle-reasoning that has no sentinel.

        The deterministic `_has_idle_sentinel` rule only catches messages that
        carry the literal HEARTBEAT_OK token. But the model sometimes leaks its
        internal "nothing needs attention right now" reasoning as plain prose,
        with no token — those slip through and reach Pascal as empty noise.

        A heartbeat output that produces actual text is the rare case, so paying
        for one haiku classification here is cheap. Conservative by design:
        returns True (drop) ONLY on a confident NOISE verdict. Any error,
        timeout, or ambiguous answer falls open to DELIVER — never silently
        swallow possibly-real content because the judge failed.
        """
        judge_prompt = (
            "A background heartbeat process produced the text below, possibly to "
            "send to the user. Classify it:\n"
            "- DELIVER: a real message carrying information, a reminder, a "
            "question, a suggestion, or any content meant for the user.\n"
            "- NOISE: the model's leaked internal reasoning or an idle "
            "'nothing needs attention / no action needed / staying quiet' note "
            "not meant for delivery.\n\n"
            "Reply with EXACTLY one word: DELIVER or NOISE. When in doubt, "
            "answer DELIVER.\n\n"
            "--- TEXT ---\n" + message
        )
        cmd = [
            self._claude_bin,
            "--dangerously-skip-permissions",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--model", "haiku",
            "-p", judge_prompt,
        ]
        # Provider gate (2026-07-07): during the primary spend-limit outage
        # every judge call failed → fail-open → noise filtering was
        # effectively disabled. Auxiliary caller: never wins the probe
        # election (probe=False), just follows the flag to the backup env.
        env = None
        try:
            from core.model_fallback import gate as _provider_gate
            if (_provider_gate(self.jarvis_dir, probe=False) == "backup"
                    and os.environ.get("CLAUDE_BACKUP_ENABLED", "true") == "true"
                    and os.environ.get("CLAUDE_BACKUP_AUTH_TOKEN")
                    and os.environ.get("CLAUDE_BACKUP_BASE_URL")):
                env = os.environ.copy()
                env["ANTHROPIC_AUTH_TOKEN"] = os.environ["CLAUDE_BACKUP_AUTH_TOKEN"]
                env["ANTHROPIC_BASE_URL"] = os.environ["CLAUDE_BACKUP_BASE_URL"]
        except Exception:
            env = None
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=60, stdin=subprocess.DEVNULL,
                cwd=str(self.work_dir),
                env=env,
                start_new_session=True,
            )
            if result.returncode != 0:
                return False  # fail-open: deliver
            verdict = result.stdout.strip().upper()
            # Only drop on a clean, confident NOISE verdict.
            return verdict == "NOISE" or verdict.endswith("NOISE")
        except Exception as e:
            self._log(f"Idle-noise judge error (failing open): {e}")
            return False

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

    def run_cycle(self, force: bool = False, only_task: str = "",
                  lock_wait: float = 0):
        """Run one heartbeat cycle. Returns user-facing message or empty string.

        Cross-process exclusive: the resident heartbeat_loop and the session
        rotation path (bot.sh runs `run_cycle(force=True, only_task=...)` in
        its own process) both call this. Without the lock, both load the full
        state dict, run concurrently, and the last save_state() clobbers the
        other's last_run/circuit updates — the root cause behind recurring
        "Heartbeat stale" daemon restarts. flock is released automatically on
        process death, so a crashed cycle can't wedge the lock.

        lock_wait: seconds to keep retrying for the lock before giving up.
        The resident loop uses 0 (skip and retry next tick); the rotation
        path passes a positive value so memory-hourly isn't silently dropped
        whenever the loop happens to be mid-cycle.
        """
        lock_path = self.state_file.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + lock_wait
        with open(lock_path, "w") as lock_f:
            while True:
                try:
                    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        self._log("Another heartbeat cycle holds the lock — skipping",
                                  only_task=only_task or "", waited=lock_wait)
                        # run_id="": no cycle_id was assigned (self._cid would
                        # be the stale id of a PREVIOUS cycle).
                        sched_emit(self.jarvis_dir, "task_skip",
                                   task=only_task or "*", run_id="",
                                   reason="overlap_lock", waited_s=lock_wait)
                        return ""
                    time.sleep(2)
            try:
                return self._run_cycle_locked(force, only_task)
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    def _run_cycle_locked(self, force: bool = False, only_task: str = ""):
        cycle_id = uuid.uuid4().hex[:8]
        self._cid = cycle_id  # used by _log
        self._cycle_prompt_variants = {}

        tasks = self._load_tasks()
        state = self.load_state()
        interval_overrides = self.load_interval_overrides()
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
                # Event-log the skip only when the task was actually DUE —
                # an open circuit is re-checked every tick (~10s) and a
                # per-tick event would flood the replay log.
                _itv = interval_overrides.get(task["name"]) \
                    or (ts.effective_interval if ts.effective_interval > 0
                        else task["interval"])
                if force or (now - last_run >= _itv):
                    self._event("task_skip", task=task["name"],
                                reason="circuit_open", retry_in_s=remaining)
                continue
            # Apply cooldown even when forced, to prevent rapid repeats
            # (e.g. multiple Lark session rotations in quick succession).
            if force and (now - last_run) < self.FORCE_COOLDOWN_SECONDS:
                continue
            # Auto-tuned interval precedence: sidecar override (W3.1) →
            # legacy effective_interval in state → HEARTBEAT.md default
            interval = interval_overrides.get(task["name"]) \
                or (ts.effective_interval if ts.effective_interval > 0 else task["interval"])
            if force or (now - last_run >= interval):
                if task["name"] in self.PIPELINE_TASKS:
                    if pipeline_picked:
                        continue
                    pipeline_picked = True
                due_tasks.append(task)

        if circuit_tripped:
            self._log(f"Circuit open: {[(n, f'{s}s') for n, s in circuit_tripped]}")

        if not due_tasks:
            return ""

        # ── Shared-call backoff gate (REQ-79.1) ────────────────────────
        # While the shared Claude call is backing off, only TIER0_TASKS may
        # proceed (calendar-sync is a pre→post direct pipe that never touches
        # Claude, and it is REQ-83's upstream). Everything else — PRIORITY,
        # heavy-solo, force-triggered alike — is held: exempting PRIORITY
        # would mean re-dialing a dead API every cycle, which is exactly what
        # the backoff exists to stop. Placed BEFORE pre-scripts on purpose:
        # intention-check's pre has stateful side effects (mark_triggered +
        # inflight manifest), so gating here leaves no reconciliation debt.
        # last_run is untouched — when the backoff lapses the held tasks are
        # naturally due again and MAX_BATCH_SIZE shaves the peak.
        _shared = state.get("__shared_call__", {})
        _backoff_until = float(_shared.get("backoff_until", 0) or 0)
        if _backoff_until > now:
            gated = [t["name"] for t in due_tasks
                     if t["name"] not in self.TIER0_TASKS]
            due_tasks = [t for t in due_tasks if t["name"] in self.TIER0_TASKS]
            if gated:
                retry_in = int(_backoff_until - now)
                self._log(f"Shared-call backoff active ({retry_in}s left) — "
                          f"holding {gated}", level="warn")
                # One aggregate event per cycle (task="*"), not one per task —
                # an open backoff is re-checked every tick and per-task rows
                # would flood the replay log.
                self._event("task_skip", task="*",
                            reason="shared_call_backoff",
                            retry_in_s=retry_in, skipped=gated)
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
            for t in deferred:
                self._event("task_skip", task=t["name"], reason="batch_deferred")

        due_tasks = priority + regular

        # Run pre-scripts (record failures in circuit breaker)
        task_data = {}
        runnable = []
        skipped = []
        for task in due_tasks:
            if task["pre"]:
                data = self.run_script(task["pre"])
                if not data:
                    outcome = getattr(self, "_last_script_outcome", "ok")
                    retry_delay = self.EMPTY_RETRY_DELAYS.get(task["name"], task["interval"])
                    ts = TaskState.from_dict(state.get(task["name"], {}))
                    ts.last_run = now - task["interval"] + retry_delay
                    if outcome in ("timeout", "error", "nonzero"):
                        # Pre failure is a FAILURE (REQ-51), not "nothing to
                        # do": the breaker sees it and last_status records it
                        # so watermarks can flag a dead data-gathering step.
                        ts.last_status = f"pre_{outcome}"
                        tripped = ts.circuit.record_failure()
                        if task["name"] in self.PRIORITY_TASKS and ts.circuit.is_open:
                            ts.circuit.disabled_until = 0
                        reason = f"pre_{outcome}"
                    else:
                        # Clean empty pre (exit 0, nothing to do) is a HEALTHY
                        # cycle — advance last_success too (red-team fix): for
                        # empty-pre-dominant tasks (intention-check 296/304
                        # empty, memory-hourly) last_success would otherwise lag
                        # forever and watermarks falsely flagged them STARVED.
                        # Only pre_timeout/pre_error/nonzero (above) leave
                        # last_success stale — the real channel-dead signal.
                        ts.last_status = "empty_pre"
                        ts.last_success = now
                        reason = "empty_pre"
                    state[task["name"]] = ts.to_dict()
                    skipped.append(task["name"])
                    self._event("task_skip", task=task["name"],
                                reason=reason, retry_in_s=retry_delay)
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

        # ── Tier 0: tasks that bypass Claude entirely ──────────────────
        # Only for tasks where pre-script output is the final product.
        # Tasks needing Claude reasoning (memory-hourly, activity-log,
        # cross-session-sync) go through Claude even if they're PRIORITY.
        tier0 = [t for t in runnable if t["name"] in self.TIER0_TASKS]
        tier2 = [t for t in runnable if t["name"] not in self.TIER0_TASKS]
        user_messages = []
        producing_tasks = []

        for task in tier0:
            t0 = time.time()
            self._event("task_spawn", task=task["name"], tier=0)
            pre_data = task_data.get(task["name"], "")
            if task["post"] and pre_data:
                post_output = self.run_script(task["post"], stdin_data=pre_data)
                if post_output:
                    self._collect_output(task["name"], post_output,
                                         user_messages, producing_tasks)
            # Update state — task ran successfully
            ts = TaskState.from_dict(state.get(task["name"], {}))
            ts.last_run = now
            ts.last_success = now
            ts.last_status = "ok"
            ts.circuit.record_success()
            state[task["name"]] = ts.to_dict()
            self._event("task_finish", task=task["name"], status="ok",
                        duration_s=round(time.time() - t0, 2), tier=0)

        if tier0:
            self._log(f"Tier 0 direct: {[t['name'] for t in tier0]}")

        # ── Heavy tasks run SOLO (DeerFlow fan-out isolation) ──────────
        # Peel heavy tasks out of the batch: each gets its own Claude call with
        # an extended timeout so it can fan out subagents and wait, and a
        # slow/failed heavy task never poisons the lightweight batch envelope.
        # Cap at HEAVY_MAX_PER_CYCLE (stalest first); unrun heavy tasks keep
        # their state untouched and are picked up next cycle. Runs after Tier 0
        # but before the batch so the (fast) batch isn't delayed behind it only
        # when no heavy task is due — when one is, it's the intended trade-off.
        heavy_due = [t for t in tier2 if t.get("heavy")]
        if heavy_due:
            heavy_due.sort(key=lambda t: state.get(t["name"], {}).get("last_run", 0))
            for task in heavy_due[:self.HEAVY_MAX_PER_CYCLE]:
                self._run_solo_task(task, task_data, state, now,
                                    user_messages, producing_tasks)
            self._log(f"Heavy solo: {[t['name'] for t in heavy_due[:self.HEAVY_MAX_PER_CYCLE]]}")

        # ── Tier 2: regular tasks go through Claude ────────────────────
        runnable = [t for t in tier2 if not t.get("heavy")]
        if not runnable:
            # No batch tasks — only Tier 0 and/or heavy-solo tasks ran this
            # cycle. Their state was already updated above; persist and return
            # whatever output they staged.
            self.save_state(state)
            combined = "\n\n---\n\n".join(m for m in user_messages if m.strip())
            if combined.strip():
                if producing_tasks:
                    try:
                        source_file = self.jarvis_dir / ".heartbeat_last_source"
                        source_file.write_text(",".join(producing_tasks))
                        (self.jarvis_dir / ".heartbeat_prompt_variants").unlink(missing_ok=True)
                    except Exception:
                        pass
                self._log(f"{self._beat_status(due_tasks, skipped, tier0, tasks)} → delivered (no batch)")
                return combined
            self._log(f"{self._beat_status(due_tasks, skipped, tier0, tasks)} → OK (no batch)")
            return ""

        # Build combined prompt
        n = len(runnable)
        ack_present = [t["name"] for t in runnable if t["name"] in self.ACK_REQUIRED_TASKS]
        variants = {}
        parts = [f"[HEARTBEAT — {n} task{'s' if n > 1 else ''} due]"]
        parts.append("Process each task below. For each, return the requested format.")
        if ack_present:
            # Contract fix (REQ-30d): the old wording instructed exactly the
            # reply that stranded intents. When an ACK task has data, a bare
            # HEARTBEAT_OK is never legitimate — its envelope must cover every
            # id, with action:"silent" for no-ops.
            parts.append(
                f"Task(s) {', '.join(ack_present)} REQUIRE their JSON envelope in your reply "
                "covering EVERY listed id (use action \"silent\" for ids with nothing to say). "
                "NEVER reply with a bare HEARTBEAT_OK when these tasks are present.")
            parts.append("For the OTHER tasks only: if nothing needs user attention, "
                         "their slices may be omitted.")
        else:
            parts.append("If NOTHING across ALL tasks needs user attention, reply with exactly: HEARTBEAT_OK")
        parts.append("")

        for task in runnable:
            variant = choose_variant(self.memory_dir, task["name"], now=now)
            if variant:
                variants[task["name"]] = variant
            parts.append(f"=== TASK: {task['name']} ===")
            parts.append(inject_variant(task["prompt"].strip(), variant))
            data = task_data.get(task["name"], "")
            if data:
                parts.append(f"\nDATA:\n{data}")
            parts.append("")
        self._cycle_prompt_variants = {
            name: variant.to_log() for name, variant in variants.items()
        }

        parts.append("=== END TASKS ===")
        if n > 1:
            parts.append('Return JSON: {"tasks":{"<task-name>": <per-task response>}, "user_message":"<combined markdown or empty>"}')
        else:
            parts.append("Return the task's requested format directly.")
        if not ack_present:
            parts.append("Or if nothing needs attention: HEARTBEAT_OK")

        prompt = "\n".join(parts)

        # Call Claude
        self._log(f"Calling Claude with {n} tasks...")
        for task in runnable:
            self._event("task_spawn", task=task["name"], batch=n)
        call_t0 = time.time()
        raw = self.claude_call(prompt)
        call_dur = round(time.time() - call_t0, 2)

        if raw == "__KILLED__":
            # Claude was killed by SIGTERM/SIGKILL (restart/shutdown).
            # This is NOT a task failure — don't punish via circuit breaker.
            # Just update last_run so tasks don't immediately re-fire.
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                ts.last_run = now
                ts.last_status = "killed"
                state[task["name"]] = ts.to_dict()
                self._event("task_finish", task=task["name"], status="killed",
                            duration_s=call_dur)
            self.save_state(state)
            self._ack_failed_posts(runnable)
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → killed (no penalty)")
            return ""

        if not raw:
            # The SHARED Claude call died (timeout / network / nonzero exit)
            # before producing a byte — infrastructure trouble, not any one
            # task's fault (REQ-79.1). The old code charged record_failure()
            # to EVERY batched task, so one dead network window tripped
            # innocent circuits (7/1: batch=6 all failed → 3 circuit_tripped
            # the same second; 7/2: checkin circuit_open during a DNS outage).
            # Mirror the parse_failed branch below: fast-retry each task,
            # keep the diagnosable emit (error= per REQ-80 — 866 error-less
            # failed events was the original sin), and let the shared counter
            # absorb the failure; at SHARED_FAIL_THRESHOLD consecutive failed
            # calls the backoff gate above holds the whole non-Tier0 roster
            # instead of re-dialing a dead API every cycle.
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                retry_delay = min(300, task["interval"])
                ts.last_run = now - task["interval"] + retry_delay
                ts.last_status = "timeout" if self._call_timed_out else "failed"
                state[task["name"]] = ts.to_dict()
                if self._call_timed_out:
                    self._event("task_timeout", task=task["name"],
                                duration_s=call_dur, timeout_s=self.claude_timeout)
                else:
                    self._event("task_finish", task=task["name"],
                                status="failed", duration_s=call_dur,
                                error=_error_excerpt(self._last_call_error))
            # One shared streak +1 per failed CYCLE, not per task — six tasks
            # in one dead call are one failure, not six.
            shared = state.get("__shared_call__", {})
            fails = int(shared.get("consecutive_failures", 0)) + 1
            shared = {"consecutive_failures": fails, "last_failure": now}
            if fails >= self.SHARED_FAIL_THRESHOLD:
                backoff = min(self.SHARED_BACKOFF_MAX,
                              self.SHARED_BACKOFF_BASE
                              * (2 ** (fails - self.SHARED_FAIL_THRESHOLD)))
                shared["backoff_until"] = now + backoff
                # Ops event — NOT a chat message (REQ-62): genuine user-
                # impacting outages still reach Pascal via the self-diagnostic
                # deterministic alert (REQ-39), in plain language.
                self._log(f"Shared Claude call failed {fails}x consecutively "
                          f"— backing off {backoff}s (Tier0 keeps running)",
                          level="warn")
                self._event("shared_call_backoff", task="*",
                            consecutive_failures=fails, backoff_s=backoff,
                            error=_error_excerpt(self._last_call_error))
            state["__shared_call__"] = shared
            self.save_state(state)
            self._ack_failed_posts(runnable)
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → Claude failed")
            return ""

        # Any non-empty reply proves the shared call PATH is alive — clear the
        # streak even if the envelope below fails to parse (parse breakage is
        # a different fault tracked by __envelope_parse__, never conflated).
        # Every branch past this point ends in save_state, so the pop lands.
        state.pop("__shared_call__", None)

        if raw.strip() == "HEARTBEAT_OK":
            # Only treat as idle if the ENTIRE response is exactly HEARTBEAT_OK.
            # A multi-task JSON envelope containing "HEARTBEAT_OK" as a per-task
            # value must NOT be discarded — other tasks may have real content.
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                ts.last_run = now
                ts.last_success = now
                ts.last_status = "idle"
                ts.circuit.record_success()
                state[task["name"]] = ts.to_dict()
                self._event("task_finish", task=task["name"], status="idle",
                            duration_s=call_dur)
            self.save_state(state)
            # ACK-required tasks (intention-check) had pre-side state writes:
            # a bare HEARTBEAT_OK must not strand them (REQ-30 — this exact
            # reply, which the old prompt even INSTRUCTED, was the #1 silent
            # intent killer).
            self._ack_failed_posts(runnable)
            # Log status to stderr (goes to jarvis.log) — NOT returned to Lark
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → OK")
            return ""

        # Route responses through post-scripts
        # (user_messages and producing_tasks may already have Tier 0 results)
        parse_failed = False
        if n == 1:
            task = runnable[0]
            if task["post"]:
                post_output = self.run_script(task["post"], stdin_data=raw)
                if post_output:
                    self._collect_output(task["name"], post_output,
                                         user_messages, producing_tasks)
            elif _has_idle_sentinel(raw):
                # Model wrote reasoning then emitted HEARTBEAT_OK — idle, drop it.
                self._log(f"Single-task idle (HEARTBEAT_OK in body): {raw[:80]!r}")
            else:
                self._collect_output(task["name"], raw,
                                     user_messages, producing_tasks)
        else:
            # Multi-task: Claude returns JSON envelope. Extract robustly via
            # the shared safety boundary so wrappers/trailers do not destroy a
            # valid first JSON object.
            envelope = parse_json_response(raw)
            if envelope is not None:
                task_responses = envelope.get("tasks", {})
                for task in runnable:
                    resp = task_responses.get(task["name"])
                    # A present-but-degenerate slice (empty string, whitespace,
                    # null, or non-dict) is treated as MISSING for ACK-required
                    # tasks (red-team fix): otherwise the row fell through to
                    # the str()-coerce branch below, the manifest was never
                    # reconciled via __NO_ENVELOPE__, AND a later catch-all
                    # could double-run the post. One deterministic path only.
                    _degenerate = (resp is None
                                   or (isinstance(resp, str) and not resp.strip()))
                    if task["name"] in self.ACK_REQUIRED_TASKS and (
                            _degenerate or not isinstance(resp, (dict, str))):
                        if task["post"]:
                            self.run_script(task["post"], stdin_data="__NO_ENVELOPE__")
                            self._event("task_skip", task=task["name"],
                                        reason="missing_slice_acked")
                        continue
                    if resp is None:
                        continue
                    resp_str = json.dumps(resp, ensure_ascii=False) if isinstance(resp, dict) else str(resp)
                    if task["post"]:
                        post_output = self.run_script(task["post"], stdin_data=resp_str)
                        if post_output:
                            self._collect_output(task["name"], post_output,
                                                 user_messages, producing_tasks)
                    # Tasks without post-scripts: only show string responses, never raw JSON
                    elif isinstance(resp, str) and resp.strip() and "HEARTBEAT_OK" not in resp:
                        self._collect_output(task["name"], resp,
                                             user_messages, producing_tasks)
                # The top-level user_message is Claude's conversational summary.
                # If a task already produced a card, that card IS the message —
                # appending top_msg here would say the same thing twice (a card
                # plus a paragraph repeating it). Only surface top_msg when no
                # card carries the content; otherwise the card stands alone.
                top_msg = envelope.get("user_message", "")
                has_card = any(m.strip().startswith('{"config":') for m in user_messages)
                # SILENT_TASKS hard guarantee: the prompt asks for user_message
                # as "combined markdown" across ALL tasks in the call, so when
                # ANY task in the batch is silent the summary may carry its
                # content (the 6/12 daily-plan leak, one hop later). Drop the
                # summary in that case — non-silent tasks still deliver through
                # their own per-task slices above.
                any_silent = any(t["name"] in self.SILENT_TASKS for t in runnable)
                if top_msg and top_msg.strip() and not has_card and not any_silent:
                    user_messages.append(top_msg)
            else:
                # NEVER dump raw JSON to user — log and treat as a FAILURE.
                parse_failed = True
                self._log(f"JSON parse failed for {n}-task response — "
                          "recording failure (will retry shortly)", level="warn")
                # Head AND tail: the head alone cannot distinguish a relay
                # truncating the completion mid-body (7/7 backup-relay
                # suspicion — starts as well-formed JSON) from genuinely
                # malformed output.
                self._log(f"Raw response (first 300 chars): {raw[:300]}")
                self._log(f"Raw response (last 300 chars): {raw[-300:]}")
                self._ack_failed_posts(runnable)

        # Update state. Parse failure used to fall through to the success
        # loop — record_success for every task whose output was just thrown
        # away, invisible to the circuit breaker, no retry until the next
        # full interval (REQ-36: 7x in 2 days, 3 destroyed memory-hourly
        # payloads). Now: record_failure + bounded fast retry (≤5 min),
        # mirroring the empty-response path's breaker semantics.
        if parse_failed:
            # parse_failed is only ever set in the n > 1 branch above (a single
            # task's reply is never an envelope), so this is ALWAYS the batch
            # case: Claude mis-formatted the COMBINED multi-task JSON. That is a
            # SHARED/infrastructure failure, not any one task's fault. The old
            # code charged record_failure() to EVERY batched task, so a single
            # bad envelope tripped innocent tasks' circuits — the bug that
            # pinned eigenflux-publish / perception-collect into multi-hour
            # cooldowns (they were usually batched behind intention-check's
            # strict envelope). We still fast-retry so no payload is lost and
            # still mark parse_failed, but we do NOT poison per-task circuits;
            # a shared envelope counter absorbs the failure instead.
            for task in runnable:
                ts = TaskState.from_dict(state.get(task["name"], {}))
                retry_delay = min(300, task["interval"])
                ts.last_run = now - task["interval"] + retry_delay
                ts.last_status = "parse_failed"
                state[task["name"]] = ts.to_dict()
                # Flattened to one line: _error_excerpt keeps only the first
                # non-empty line, so a newline in the head would drop the tail
                # — and the tail is what proves/refutes relay truncation.
                self._event("task_finish", task=task["name"], status="parse_failed",
                            duration_s=round(time.time() - call_t0, 2),
                            retry_in_s=retry_delay,
                            error=_error_excerpt(
                                "envelope unparseable; head: "
                                + " ".join(raw[:200].split())
                                + " …tail: " + " ".join(raw[-200:].split())))
            env = state.get("__envelope_parse__", {})
            env_fails = int(env.get("consecutive_failures", 0)) + 1
            state["__envelope_parse__"] = {"consecutive_failures": env_fails,
                                           "last_failure": now}
            # Persistent envelope breakage is a real signal (prompt too long /
            # model degraded) — surface it loudly rather than silently
            # poisoning task circuits.
            if env_fails >= 5:
                self._log(f"Envelope parse failed {env_fails}x consecutively "
                          f"({n}-task batch) — combined JSON keeps mis-forming "
                          "(NOT tripping per-task circuits)", level="warn")
            self.save_state(state)
            self._log(f"{self._beat_status(due_tasks, skipped, runnable, tasks)} → parse failed")
            return ""

        # (preserve circuit breaker data, record success)
        for task in runnable:
            ts = TaskState.from_dict(state.get(task["name"], {}))
            ts.last_run = now
            ts.last_success = now
            ts.last_status = "ok"
            ts.circuit.record_success()
            state[task["name"]] = ts.to_dict()
            # duration covers claude_call + this task's post-script routing
            self._event("task_finish", task=task["name"], status="ok",
                        duration_s=round(time.time() - call_t0, 2))
        state.pop("__envelope_parse__", None)  # a clean parse clears the shared streak
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
            # Drop dangling-placeholder heartbeats: a hook with no payload
            # (last line is a bare ellipsis). See _is_dangling_placeholder /
            # the 2026-06-02 broken EigenFlux 撞名 card.
            if _is_dangling_placeholder(m):
                self._log(f"Blocked incomplete placeholder heartbeat: {m[:80]!r}")
                continue
            # Final net: any message carrying the idle sentinel is leaked scratch
            # work, not content for the user. Catches paths the per-task guards miss.
            if _has_idle_sentinel(m):
                self._log(f"Blocked HEARTBEAT_OK-tainted message: {m[:80]!r}")
                continue
            # Second net (cheap model): catch leaked idle-reasoning that carries
            # NO sentinel. Conservative + fail-open (see _judge_is_idle_noise).
            if self.idle_judge and self._judge_is_idle_noise(m):
                self._log(f"Haiku judge dropped idle-noise message: {m[:80]!r}")
                continue
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
                    variant_rows = {
                        name: self._cycle_prompt_variants[name]
                        for name in producing_tasks
                        if name in self._cycle_prompt_variants
                    }
                    variant_file = self.jarvis_dir / ".heartbeat_prompt_variants"
                    if variant_rows:
                        variant_file.write_text(json.dumps(variant_rows, ensure_ascii=False))
                    else:
                        variant_file.unlink(missing_ok=True)
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
