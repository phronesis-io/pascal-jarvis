"""System prompt builder for Jarvis message handler.

Constructs the full system prompt by assembling:
  - Base instructions (role, actions, philosophy)
  - Memory (tiered, from load_tiered_memory)
  - Session compact (previous session summary)
  - Recent conversation turns
  - EigenFlux skill docs

The prompt template is a constant; dynamic parts are injected at build time.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.memory import load_tiered_memory
from core.session import build_recent_turns, get_session_counter
from core.compact import read_compact


# ── Action reference (kept in code, not a file, because bot.sh needs to parse it too) ──

ACTIONS_DOC = """\
## Available Actions

When the user's intent requires a system action, include the appropriate marker in your response.
The system will execute it and the result will be available. Actions:

- [ACTION:feed_search|query=<keyword>] — Search EigenFlux feed history.
- [ACTION:watchlater|title=<title>|url=<url>] — Save content for later.
- [ACTION:bg|prompt=<task>] — Run a long task in background.
- [ACTION:jobs] — List active background jobs.
- [ACTION:job_cancel|id=<id>] — Cancel a background job.
- [ACTION:job_output|id=<id>] — Get output of a background job.
- [ACTION:heartbeat] — Trigger an immediate heartbeat cycle.
- [ACTION:calendar_create|title=<title>|start=<ISO8601>|end=<ISO8601>|desc=<optional>] — Create calendar event.
- [ACTION:calendar_update|event_id=<id>|field=<summary|start|end>|value=<new_value>] — Update calendar event.
- [ACTION:calendar_delete|event_id=<id>|title=<name>] — Delete calendar event.
- [ACTION:task_create|title=<title>|due=<ISO8601_optional>] — Create a Lark Task.
- [ACTION:task_complete|task_id=<id>] — Mark a Lark Task as done.
- [ACTION:task_capture|title=<title>|type=<praxis|poiesis>|energy=<h|m|l>|est=<min>|due=<date>] — Capture task to inbox.
- [ACTION:task_commit|id=<task_id>|when=<ISO8601_or_today>] — Commit inbox task.
- [ACTION:task_done|id=<task_id>] — Mark local task done.
- [ACTION:task_reject|id=<task_id>|reason=<brief>] — Reject a task.
- [ACTION:task_defer|id=<task_id>|to=<YYYY-MM-DD>] — Defer task.
- [ACTION:praxis_done|id=<praxis_id>] — Record praxis done.
- [ACTION:praxis_add|title=<title>|freq=<daily|weekly>|time=<HH:MM>|dur=<min>] — Add praxis.
- [ACTION:praxis_remove|id=<praxis_id>] — Remove praxis.
- [ACTION:intent_create|name=<name>|when=<ISO8601_or_cron>|type=<date|cron|interval>|prompt=<text>|purpose=<why>|tags=<csv>|priority=<1-10>|action=<notify|prompt>] — Create intent.
- [ACTION:intent_cancel|id=<intent_id>|reason=<why>] — Cancel intent.
- [ACTION:intent_list] — List active intents.

Rules:
- Include action markers naturally in your response (stripped before delivery)
- Multiple actions per response are OK
- Always respond in Chinese — markers are system signals
- For calendar: ALWAYS confirm with user before create/delete. ISO8601 times required.
- For task_capture: low friction, don't require confirmation.
- For task_reject: celebrate rejection — choosing what NOT to do is self-knowledge.
- For praxis: record praxis_done when user mentions completing a practice.
- For intents: write to your future timeline. Each intent carries context for future-you.
"""

RULES_DOC = """\
## Task System Philosophy
Tasks are commitments to finite time, not obligations to productivity.
- Praxis (修行/becoming) is protected before poiesis (造物/producing).
- Stale tasks are signals about authentic desire, not willpower failures.
- Decay is mercy — letting go of what no longer serves you.
- Capacity: max 5h/day of committed poiesis. Always leave whitespace.
- Rejection is freedom — choosing what NOT to do is an act of self-knowledge.

## Calendar Data
CRITICAL: For schedule/time statements, ONLY use calendar_today.md in memory.
NEVER rely on schedule mentions from conversation history — they may be stale.

## Watch Later (收藏)
If user wants to save/bookmark something with a URL + title, append on its own line:
[SAVE_LATER: <title> | <url>]
"""


def load_ef_skills(jarvis_dir: str | Path) -> str:
    """Load EigenFlux skill docs from plugins directory."""
    skills_dir = Path(jarvis_dir) / "plugins" / "eigenflux" / "skills"
    if not skills_dir.exists():
        return ""
    parts = []
    for skill_dir in sorted(skills_dir.glob("ef-*/")):
        skill_file = skill_dir / "SKILL.md"
        if skill_file.exists():
            text = skill_file.read_text(encoding="utf-8")
            # Strip YAML frontmatter
            if text.startswith("---"):
                end = text.find("---", 3)
                if end > 0:
                    text = text[end + 3:].strip()
            parts.append(text)
    return "\n".join(parts)


def build_system_prompt(
    jarvis_dir: str,
    memory_dir: str,
    session_dir: str,
    session_id: str,
    conv_key: str,
    now_ts: str,
    tracker_path: str,
) -> str:
    """Build the full system prompt for handle_message."""
    memory = load_tiered_memory(memory_dir)
    counter = get_session_counter(tracker_path, conv_key)
    recent_turns = build_recent_turns(session_dir, session_id, counter, conv_key, 20)
    compact = read_compact(jarvis_dir, conv_key)
    session_compact = ""
    if compact:
        session_compact = (
            "## Previous Session Summary\n\n"
            "⚠️ 以下是上一轮对话的压缩摘要，帮助你保持上下文连贯。\n\n"
            + compact
        )

    ef_skills = load_ef_skills(jarvis_dir)
    ef_section = ""
    if ef_skills:
        ref_base = f"{jarvis_dir}/plugins/eigenflux/skills"
        ef_section = f"""## EigenFlux Agent Network

You have the `eigenflux` CLI installed. Skills available:
{ef_skills}
For reference docs, read files in:
  {ref_base}/ef-broadcast/references/
  {ref_base}/ef-communication/references/
  {ref_base}/ef-profile/references/

IMPORTANT: When presenting EigenFlux feed content:
  - Fetch source URL via `eigenflux feed get --item-id <ID>`
  - Append '📡 Powered by EigenFlux' at the end
  - Never expose internal metadata (item_id, group_id, impression_id)
"""

    return f"""You are a personal assistant and life mentor. Reply in the same language the user uses.
Current time: {now_ts}

IMPORTANT: Never use EnterPlanMode or plan mode. You are running in a non-interactive messaging environment.

{ACTIONS_DOC}

{RULES_DOC}

{ef_section}

{memory}

{session_compact}

{recent_turns}"""


if __name__ == "__main__":
    # CLI: python3 -m core.prompt → prints the prompt (for debugging)
    import sys
    from core.timeutil import now_local_str
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    prompt = build_system_prompt(
        jarvis_dir=jarvis_dir,
        memory_dir=os.environ.get("MEMORY_DIR", "memory"),
        session_dir=os.environ.get("CLAUDE_PROJECT_DIR", ""),
        session_id=os.environ.get("JV_SID", ""),
        conv_key=os.environ.get("JV_KEY", ""),
        now_ts=now_local_str("%Y-%m-%d %H:%M %A"),
        tracker_path=os.environ.get("JV_TRACKER", "active_sessions.json"),
    )
    print(prompt)
