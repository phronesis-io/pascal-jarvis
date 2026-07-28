"""Deterministic, read-only evidence providers for user-authored Routines.

A Routine declares which evidence it needs (``["calendar", "cards:7",
"memory:hot/user_profile.md"]``). This module turns that declaration into a
bounded text block *before* any model call, so the routine's output is grounded
in real state instead of the model's recollection.

Design rules, all enforced here rather than in a prompt:

- **Read-only.** No provider mutates anything and none of them shell out.
- **Path-guarded.** ``file:`` and ``memory:`` resolve inside JARVIS_DIR /
  MEMORY_DIR only, and refuse the credential-bearing config files even when a
  path lands inside those roots.
- **Bounded.** Every provider is capped individually and the joined block is
  capped again, so one runaway file cannot blow the heartbeat prompt budget.
- **Fail-soft, never fail-silent.** A provider that errors contributes a
  visible ``(unavailable: ...)`` line. A routine that quietly loses half its
  evidence would produce confident output about state nobody read.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Per-provider output ceiling and the ceiling for the joined block.
PROVIDER_MAX_CHARS = 4000
TOTAL_MAX_CHARS = 12000

# Files that live inside the allowed roots but must never enter a model prompt.
# Matched on the resolved path's name, so a symlink or ``../`` detour cannot
# rename its way past the check.
DENIED_NAMES = {
    "jarvis.yaml",
    "sources.yaml",
    ".env",
    "mobile_access.json",
    "active_sessions.json",
}
DENIED_SUFFIXES = (".key", ".pem", ".sqlite", ".db")


class EvidenceError(Exception):
    """A provider spec is malformed or refers to something out of bounds."""


def _jarvis_dir() -> Path:
    return Path(os.environ.get("JARVIS_DIR") or PROJECT_ROOT)


def _memory_dir() -> Path:
    raw = os.environ.get("MEMORY_DIR")
    if raw:
        return Path(raw)
    return Path.home() / ".jarvis" / "memory"


def _clip(text: str, limit: int = PROVIDER_MAX_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    # Truncation is announced. A silently cut evidence block reads to the model
    # as a complete picture of state that it is not.
    return text[:limit].rstrip() + f"\n…（已截断，原文 {len(text)} 字）"


def _read_guarded(path: Path, root: Path, label: str) -> str:
    """Read a file, refusing anything outside ``root`` or on the deny list."""
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError as exc:
        raise EvidenceError(f"{label}: 路径无法解析（{exc}）") from exc
    if not resolved.is_relative_to(root_resolved):
        raise EvidenceError(f"{label}: 越界，只允许 {root_resolved} 以内")
    if resolved.name in DENIED_NAMES or resolved.name.endswith(DENIED_SUFFIXES):
        raise EvidenceError(f"{label}: {resolved.name} 含凭证或二进制状态，不进提示词")
    if not resolved.is_file():
        raise EvidenceError(f"{label}: 文件不存在")
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise EvidenceError(f"{label}: 读取失败（{exc}）") from exc


# ── providers ────────────────────────────────────────────────────────────────
# Each takes the spec argument (text after the colon, "" when absent) and
# returns the evidence body. Raising EvidenceError yields an "(unavailable)"
# line instead of killing the run.


def _p_calendar(arg: str) -> str:
    return _read_guarded(_memory_dir() / "hot" / "calendar_today.md",
                         _memory_dir(), "calendar")


def _p_memory(arg: str) -> str:
    if not arg:
        raise EvidenceError("memory: 需要一个相对路径，例如 memory:hot/user_profile.md")
    return _read_guarded(_memory_dir() / arg, _memory_dir(), f"memory:{arg}")


def _p_file(arg: str) -> str:
    if not arg:
        raise EvidenceError("file: 需要一个相对路径")
    return _read_guarded(_jarvis_dir() / arg, _jarvis_dir(), f"file:{arg}")


def _p_intents(arg: str) -> str:
    from core import intentions
    rows = intentions.list_intents(status="pending")
    if not rows:
        return "（当前没有待触发的 intent）"
    lines = []
    for r in rows[:40]:
        when = r.get("trigger_config") or ""
        if isinstance(when, dict):
            when = json.dumps(when, ensure_ascii=False)
        lines.append(f"- {r.get('name', '?')} [{r.get('trigger_type', '?')} {when}]")
    return "\n".join(lines)


def _p_cards(arg: str) -> str:
    """Recent memorial cards and whether they were actually decided."""
    days = _int_arg(arg, default=7, lo=1, hi=90, label="cards")
    from datetime import timedelta

    from core import memorial
    from core.timeutil import now_local

    cutoff = (now_local() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    rows = [r for r in memorial.list_memorials() if str(r.get("ts", "")) >= cutoff]
    if not rows:
        return f"（最近 {days} 天没有卡片）"
    decided = sum(1 for r in rows if r.get("status") != "pending")
    lines = [f"最近 {days} 天 {len(rows)} 张卡，其中 {decided} 张被批过。"]
    for r in rows[-40:]:
        mark = "未批" if r.get("status") == "pending" else "已批"
        lines.append(f"- [{mark}] {r.get('source', '?')}: {str(r.get('title', ''))[:40]}")
    return "\n".join(lines)


def _p_tasks(arg: str) -> str:
    from core.tasks import TaskManager
    tm = TaskManager(_memory_dir())
    open_items = tm.active()
    if not open_items:
        return "（没有未完成任务）"
    return "\n".join(f"- {t.get('content') or t.get('title') or '?'} "
                     f"[{t.get('status', '?')}]" for t in open_items[:40])


def _p_mail(arg: str) -> str:
    days = _int_arg(arg, default=3, lo=1, hi=30, label="mail")
    from datetime import timedelta

    from core.jsonl import read_jsonl
    from core.timeutil import now_local

    path = _jarvis_dir() / "mail" / "triaged.jsonl"
    if not path.exists():
        return "（没有邮件记录）"
    cutoff = (now_local() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = [r for r in read_jsonl(path) if str(r.get("ts", ""))[:10] >= cutoff]
    if not rows:
        return f"（最近 {days} 天没有邮件）"
    return "\n".join(f"- [{r.get('decision', '?')}] {str(r.get('subject', ''))[:60]}"
                     for r in rows[-40:])


def _p_git(arg: str) -> str:
    """Recent commits for a sibling repo under the repos root."""
    if not arg:
        raise EvidenceError("git: 需要仓库名，例如 git:eigenflux-pgc")
    if "/" in arg or arg.startswith("."):
        raise EvidenceError(f"git:{arg} 只接受同级仓库名，不接受路径")
    repo = (_jarvis_dir().parent / arg).resolve()
    if not (repo / ".git").exists():
        raise EvidenceError(f"git:{arg} 不是一个 git 仓库")
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "--since=7.days",
             "--pretty=format:%h %ad %s", "--date=short", "-n", "40"],
            capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceError(f"git:{arg} 执行失败（{exc}）") from exc
    if out.returncode != 0:
        raise EvidenceError(f"git:{arg} 失败（{out.stderr.strip()[:120]}）")
    return out.stdout.strip() or f"（{arg} 最近 7 天没有提交）"


def _int_arg(arg: str, *, default: int, lo: int, hi: int, label: str) -> int:
    if not arg:
        return default
    try:
        val = int(arg)
    except ValueError as exc:
        raise EvidenceError(f"{label}:{arg} 不是整数") from exc
    if not lo <= val <= hi:
        raise EvidenceError(f"{label}:{arg} 超出范围 {lo}-{hi}")
    return val


PROVIDERS = {
    "calendar": _p_calendar,
    "intents": _p_intents,
    "cards": _p_cards,
    "tasks": _p_tasks,
    "mail": _p_mail,
    "memory": _p_memory,
    "file": _p_file,
    "git": _p_git,
}

# Shown to the user (and to the model that helps author routines) so an
# unsupported source is rejected at creation time, not at 3am on first fire.
PROVIDER_HELP = {
    "calendar": "今天和近期日程（hot/calendar_today.md）",
    "intents": "当前待触发的 intent 列表",
    "cards:N": "最近 N 天的奏折卡片和批阅情况（默认 7）",
    "tasks": "未完成任务列表",
    "mail:N": "最近 N 天已分拣的邮件标题（默认 3）",
    "memory:<相对路径>": "记忆目录下的某个文件",
    "file:<相对路径>": "Jarvis 目录下的某个文件",
    "git:<仓库名>": "同级仓库最近 7 天的提交",
}


def validate_spec(spec: str) -> str:
    """Normalize one evidence spec, raising EvidenceError when unusable."""
    spec = str(spec).strip()
    if not spec:
        raise EvidenceError("空的证据项")
    name, _, arg = spec.partition(":")
    name = name.strip().lower()
    if name not in PROVIDERS:
        raise EvidenceError(
            f"未知证据源 {name!r}；可用：{', '.join(sorted(PROVIDER_HELP))}")
    if name in {"memory", "file", "git"} and not arg.strip():
        raise EvidenceError(f"{name} 需要参数，例如 {name}:...")
    return f"{name}:{arg.strip()}" if arg.strip() else name


def collect(specs) -> tuple[str, list[str]]:
    """Gather every declared evidence source into one bounded block.

    Returns ``(text, gathered_labels)``. Labels record what was actually read,
    which the audit trail stores — a run whose evidence was unavailable must
    stay distinguishable from one that genuinely saw an empty world.
    """
    chunks: list[str] = []
    gathered: list[str] = []
    for raw in list(specs or []):
        spec = str(raw).strip()
        if not spec:
            continue
        name, _, arg = spec.partition(":")
        name = name.strip().lower()
        arg = arg.strip()
        fn = PROVIDERS.get(name)
        if fn is None:
            chunks.append(f"### {spec}\n(unavailable: 未知证据源)")
            continue
        try:
            body = _clip(fn(arg))
            gathered.append(spec)
        except EvidenceError as exc:
            body = f"(unavailable: {exc})"
        except Exception as exc:  # provider bug must not kill the routine
            body = f"(unavailable: {type(exc).__name__}: {exc})"
        chunks.append(f"### {spec}\n{body}")
    text = "\n\n".join(chunks)
    if len(text) > TOTAL_MAX_CHARS:
        text = text[:TOTAL_MAX_CHARS].rstrip() + "\n…（证据总量超限，已截断）"
    return text, gathered
