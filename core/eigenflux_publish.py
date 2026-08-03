"""Lifecycle helpers for Jarvis-authored EigenFlux broadcast drafts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from core.jsonl import read_jsonl
from core.timeutil import now_local_str

DRAFT_MAX_AGE_S = 48 * 3600
LAPSE_REASON = "广播草稿 48 小时未批，已自动归档"

# An APPROVED draft that failed to send is retried deterministically by
# reconcile_pending_drafts — no model in the loop, the human already decided.
# Before 2026-08-03 the failure string promised 「内容仍保留待重试」 while
# nothing retried anything: the 7/24 approval died on a PATH error and aged
# out into expired/ unsent. Expiry for an approved draft counts from
# approved_epoch, so approval near the 48h line still gets a full window.
APPROVED_MAX_ATTEMPTS = 5
APPROVED_LAPSE_REASON = "广播已批准但重试 {attempts} 次仍失败，已归档：{error}"


def resolve_eigenflux_bin() -> str:
    """Absolute path to the eigenflux CLI, or "" when it is not installed.

    The card-callback process runs under launchd, whose PATH does not include
    ~/.local/bin (the standing launchd gotcha). Resolving by bare name there
    turned an explicit user approval into `[Errno 2] ... 'eigenflux'`.
    """
    found = shutil.which("eigenflux")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "eigenflux"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return ""


def stamp_publish_state(jarvis_dir: str | Path, content: str, notes: dict) -> None:
    """Record a successful publish in publish_state.json (best-effort)."""
    state_file = Path(jarvis_dir) / "eigenflux" / "publish_state.json"
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
            "content_preview": str(content)[:120],
        })
        state["recent"] = recent[-30:]
        from core.safety import atomic_write
        atomic_write(state_file, json.dumps(state, ensure_ascii=False))
    except Exception as e:
        print(f"[eigenflux_publish] publish_state stamp failed: {e}",
              file=sys.stderr)


def publish_draft(data: dict, *, cwd: str | Path) -> tuple[bool, str]:
    """Send one draft through the CLI. Returns (ok, error)."""
    content = str(data.get("content", "")).strip()
    if not content:
        return False, "广播内容为空"
    binary = resolve_eigenflux_bin()
    if not binary:
        return False, "eigenflux CLI 未安装（PATH 和 ~/.local/bin 都没有）"
    notes = json.dumps(data.get("notes") or {}, ensure_ascii=False)
    cmd = [binary, "publish", "--content", content,
           "--notes", notes, "--accept-reply", "-f", "json"]
    if data.get("url"):
        cmd.extend(["--url", str(data["url"])])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=30, cwd=str(cwd))
    except Exception as e:
        return False, str(e)
    if result.returncode != 0:
        return False, (result.stderr or "").strip()[:160]
    return True, ""


def mark_approved_failure(path: Path, data: dict, error: str,
                          *, now: float | None = None) -> dict:
    """Stamp an approval + failure onto the draft so the retrier owns it."""
    current = time.time() if now is None else float(now)
    data.setdefault("approved_epoch", int(current))
    data["attempts"] = int(data.get("attempts", 0)) + 1
    data["last_error"] = str(error)[:200]
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        print(f"[eigenflux_publish] approval stamp failed: {e}", file=sys.stderr)
    return data


def _draft_id(path: Path, data: dict) -> str:
    value = str(data.get("id") or "").strip()
    return value or path.stem


def _load_draft(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _lapse_matching_memorial(
    jarvis_dir: Path,
    *,
    pending_id: str,
    memorial_id: str = "",
    reason: str = "",
) -> bool:
    """Close the approval card that belongs to an expired draft.

    Older drafts predate the explicit ``memorial_id`` field, so context is the
    compatibility key. Reading and writing the caller's root keeps tests and
    secondary installs isolated from the live ledger.
    """
    from core import memorial

    ledger = jarvis_dir / "memorials.jsonl"
    states = memorial._fold(read_jsonl(ledger))
    target = str(memorial_id or "").strip()
    if not target:
        marker = f"pending_publish id={pending_id}"
        for state in states.values():
            if (
                state.get("source") == "eigenflux-publish"
                and marker in str(state.get("context") or "")
            ):
                target = str(state.get("id") or "")
                break
    state = states.get(target)
    if not state or state.get("status") != "pending":
        return False
    memorial._append_line(
        ledger,
        {
            "ev": "lapse",
            "id": target,
            "ts": now_local_str(),
            "reason": reason or LAPSE_REASON,
        },
    )
    return True


def _resolve_matching_memorial(
    jarvis_dir: Path,
    *,
    pending_id: str,
    memorial_id: str = "",
    label: str,
) -> bool:
    """Converge the approval card after a deterministic retry succeeded.

    Without this the card would sit 已批 with 「广播失败」as its last visible
    outcome even though the retry later went through — the exact
    said-one-thing-did-another gap C2 exists to close.
    """
    from core import memorial

    ledger = jarvis_dir / "memorials.jsonl"
    states = memorial._fold(read_jsonl(ledger))
    target = str(memorial_id or "").strip()
    if not target:
        marker = f"pending_publish id={pending_id}"
        for state in states.values():
            if (
                state.get("source") == "eigenflux-publish"
                and marker in str(state.get("context") or "")
            ):
                target = str(state.get("id") or "")
                break
    if not target or target not in states:
        return False
    memorial._append_line(
        ledger,
        {
            "ev": "resolve",
            "id": target,
            "ts": now_local_str(),
            "label": label,
            "result": label,
        },
    )
    return True


def reconcile_pending_drafts(
    jarvis_dir: str | Path,
    *,
    now: float | None = None,
    max_age_s: int = DRAFT_MAX_AGE_S,
    publisher=None,
) -> dict:
    """Archive stale drafts and converge their approval cards.

    Returns counts for the scheduler and UI. Existing files in ``expired/``
    are also reconciled, repairing cards produced before the lifecycle link
    was added.
    """
    root = Path(jarvis_dir)
    pending_dir = root / "eigenflux" / "pending_publish"
    expired_dir = pending_dir / "expired"
    current_time = time.time() if now is None else float(now)
    active = 0
    expired = 0
    lapsed = 0

    retried = 0
    published = 0

    def _expire(path: Path, data: dict, reason: str = "") -> None:
        nonlocal expired, lapsed
        expired_dir.mkdir(parents=True, exist_ok=True)
        destination = expired_dir / path.name
        try:
            os.replace(path, destination)
        except OSError:
            return
        expired += 1
        if _lapse_matching_memorial(
            root,
            pending_id=_draft_id(destination, data),
            memorial_id=str(data.get("memorial_id") or ""),
            reason=reason,
        ):
            lapsed += 1

    for path in sorted(pending_dir.glob("*.json")):
        data = _load_draft(path)
        approved_epoch = int(data.get("approved_epoch") or 0)

        if approved_epoch:
            # The human already said 发. This block's only job is to make that
            # decision stick: deterministic retry, capped, honest terminal.
            attempts = int(data.get("attempts") or 0)
            if attempts >= APPROVED_MAX_ATTEMPTS:
                _expire(path, data, reason=APPROVED_LAPSE_REASON.format(
                    attempts=attempts,
                    error=str(data.get("last_error") or "")[:120]))
                continue
            retried += 1
            ok, error = (publisher or publish_draft)(data, cwd=root)
            if ok:
                published += 1
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                stamp_publish_state(root, str(data.get("content", "")),
                                    data.get("notes") or {})
                _resolve_matching_memorial(
                    root,
                    pending_id=_draft_id(path, data),
                    memorial_id=str(data.get("memorial_id") or ""),
                    label=f"✅ 已广播（第 {attempts + 1} 次重试成功）",
                )
                continue
            mark_approved_failure(path, data, error, now=current_time)
            active += 1
            continue

        try:
            age = current_time - path.stat().st_mtime
        except OSError:
            continue
        if age <= max_age_s:
            active += 1
            continue
        _expire(path, data)

    # Repair legacy drafts that were already moved before memorial linkage.
    for path in sorted(expired_dir.glob("*.json")):
        data = _load_draft(path)
        if _lapse_matching_memorial(
            root,
            pending_id=_draft_id(path, data),
            memorial_id=str(data.get("memorial_id") or ""),
        ):
            lapsed += 1

    return {"active": active, "expired": expired, "lapsed": lapsed,
            "retried": retried, "published": published}
