"""Task management — JSONL-based task and praxis system.

Data files live in $MEMORY_DIR/system/:
  tasks.jsonl          — primary task store (one JSON per line)
  praxis.jsonl         — recurring praxis (habits/practices) registry
  tasks_archive.jsonl  — resolved items older than 30 days
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from core.timeutil import now_local

DAILY_BUDGET_MIN = 300


@contextmanager
def _locked(path: Path):
    """Exclusive file lock via sibling .lock file (survives atomic renames)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    items = []
    for line in lines:
        line = line.strip()
        if line:
            items.append(json.loads(line))
    return items


def _write_jsonl(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def _now_iso() -> str:
    return now_local().isoformat(timespec="seconds")


class TaskManager:
    def __init__(self, memory_dir: Path):
        self._dir = memory_dir / "system"
        self._tasks_path = self._dir / "tasks.jsonl"
        self._praxis_path = self._dir / "praxis.jsonl"
        self._archive_path = self._dir / "tasks_archive.jsonl"

    # --- internal helpers ---

    def _load_tasks(self) -> list[dict]:
        return _read_jsonl(self._tasks_path)

    def _save_tasks(self, tasks: list[dict]) -> None:
        _write_jsonl(self._tasks_path, tasks)

    def _next_task_id(self, tasks: list[dict]) -> str:
        today = now_local().strftime("%Y%m%d")
        prefix = f"t_{today}_"
        existing = [t["id"] for t in tasks if t["id"].startswith(prefix)]
        n = len(existing) + 1
        return f"{prefix}{n:03d}"

    def _next_praxis_id(self, items: list[dict]) -> str:
        nums = []
        for item in items:
            try:
                nums.append(int(item["id"].split("_")[1]))
            except (IndexError, ValueError):
                pass
        n = (max(nums) if nums else 0) + 1
        return f"px_{n:03d}"

    def _find_task(self, tasks: list[dict], task_id: str) -> dict | None:
        for t in tasks:
            if t["id"] == task_id:
                return t
        return None

    # --- CRUD ---

    def capture(
        self,
        title: str,
        type: str = "poiesis",
        energy: str = "medium",
        time_est_min: int = 30,
        due: str | None = None,
        source: str = "conversation",
        tags: list[str] | None = None,
    ) -> dict:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = {
                "id": self._next_task_id(tasks),
                "title": title,
                "type": type,
                "status": "inbox",
                "created": _now_iso(),
                "committed_date": None,
                "when": None,
                "due": due,
                "energy": energy,
                "time_est_min": time_est_min,
                "source": source,
                "tags": tags or [],
                "decay_touches": 0,
                "last_surfaced": _today_str(),
                "resolution": None,
                "resolved_date": None,
                "notes": "",
            }
            tasks.append(task)
            self._save_tasks(tasks)
        return task

    def commit(self, task_id: str, when: str | None = None, date: str | None = None) -> bool:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = self._find_task(tasks, task_id)
            if not task or task["status"] not in ("inbox", "committed"):
                return False
            task["status"] = "committed"
            task["committed_date"] = date or _today_str()
            if when:
                task["when"] = when
            self._save_tasks(tasks)
        return True

    def done(self, task_id: str) -> bool:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = self._find_task(tasks, task_id)
            if not task or task["status"] not in ("inbox", "committed"):
                return False
            task["status"] = "done"
            task["resolution"] = "done"
            task["resolved_date"] = _today_str()
            self._save_tasks(tasks)
        return True

    def reject(self, task_id: str, reason: str = "") -> bool:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = self._find_task(tasks, task_id)
            if not task or task["resolution"]:
                return False
            task["status"] = "rejected"
            task["resolution"] = "rejected"
            task["resolved_date"] = _today_str()
            if reason:
                task["notes"] = (task.get("notes") or "") + f" [rejected: {reason}]"
            self._save_tasks(tasks)
        return True

    def defer(self, task_id: str, to_date: str) -> bool:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = self._find_task(tasks, task_id)
            if not task or task["resolution"]:
                return False
            task["status"] = "inbox"
            task["committed_date"] = None
            task["when"] = to_date
            task["resolution"] = None
            task["notes"] = (task.get("notes") or "") + f" [deferred→{to_date}]"
            self._save_tasks(tasks)
        return True

    # --- Queries ---

    def inbox(self) -> list[dict]:
        return [t for t in self._load_tasks() if t["status"] == "inbox"]

    def today_committed(self, date: str | None = None) -> list[dict]:
        d = date or _today_str()
        return [
            t for t in self._load_tasks()
            if t["status"] == "committed" and t.get("committed_date") == d
        ]

    def active(self) -> list[dict]:
        return [t for t in self._load_tasks() if t["resolution"] is None]

    def stale_inbox(self, hours: int = 48) -> list[dict]:
        cutoff = now_local() - timedelta(hours=hours)
        results = []
        for t in self._load_tasks():
            if t["status"] != "inbox":
                continue
            try:
                created = datetime.fromisoformat(t["created"])
                if created < cutoff:
                    results.append(t)
            except (ValueError, TypeError):
                pass
        return results

    def ready_to_decay(self, threshold: int = 3) -> list[dict]:
        return [
            t for t in self._load_tasks()
            if t["resolution"] is None and t.get("decay_touches", 0) >= threshold
        ]

    # --- Decay ---

    def touch(self, task_id: str) -> None:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = self._find_task(tasks, task_id)
            if task and task["resolution"] is None:
                task["decay_touches"] = task.get("decay_touches", 0) + 1
                task["last_surfaced"] = _today_str()
                self._save_tasks(tasks)

    def decay(self, task_id: str, reason: str = "") -> bool:
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            task = self._find_task(tasks, task_id)
            if not task or task["resolution"]:
                return False
            task["status"] = "decayed"
            task["resolution"] = "decayed"
            task["resolved_date"] = _today_str()
            if reason:
                task["notes"] = (task.get("notes") or "") + f" [decayed: {reason}]"
            self._save_tasks(tasks)
        return True

    # --- Capacity ---

    def today_capacity(self, date: str | None = None) -> dict:
        committed = self.today_committed(date)
        committed_min = sum(t.get("time_est_min", 0) for t in committed)
        return {
            "committed_min": committed_min,
            "budget": DAILY_BUDGET_MIN,
            "remaining": max(0, DAILY_BUDGET_MIN - committed_min),
            "over": max(0, committed_min - DAILY_BUDGET_MIN),
        }

    # --- Archive ---

    def archive_old(self, days: int = 30) -> int:
        cutoff = (_today_str_to_date(_today_str()) - timedelta(days=days)).isoformat()
        with _locked(self._tasks_path):
            tasks = self._load_tasks()
            keep, archive = [], []
            for t in tasks:
                if t.get("resolved_date") and t["resolved_date"] <= cutoff:
                    archive.append(t)
                else:
                    keep.append(t)
            if not archive:
                return 0
            # Append to archive file
            existing_archive = _read_jsonl(self._archive_path)
            existing_archive.extend(archive)
            _write_jsonl(self._archive_path, existing_archive)
            self._save_tasks(keep)
        return len(archive)

    # --- Praxis ---

    def praxis_list(self) -> list[dict]:
        return _read_jsonl(self._praxis_path)

    def praxis_add(
        self,
        title: str,
        frequency: str = "daily",
        preferred_time: str = "08:30",
        duration_min: int = 20,
        protect: bool = True,
    ) -> dict:
        with _locked(self._praxis_path):
            items = _read_jsonl(self._praxis_path)
            entry = {
                "id": self._next_praxis_id(items),
                "title": title,
                "frequency": frequency,
                "preferred_time": preferred_time,
                "duration_min": duration_min,
                "protect": protect,
                "streak_current": 0,
                "streak_best": 0,
                "last_done": None,
            }
            items.append(entry)
            _write_jsonl(self._praxis_path, items)
        return entry

    def praxis_done(self, praxis_id: str) -> None:
        with _locked(self._praxis_path):
            items = _read_jsonl(self._praxis_path)
            for item in items:
                if item["id"] == praxis_id:
                    today = _today_str()
                    if item.get("last_done") == today:
                        return  # already marked today
                    item["streak_current"] = item.get("streak_current", 0) + 1
                    item["streak_best"] = max(
                        item.get("streak_best", 0), item["streak_current"]
                    )
                    item["last_done"] = today
                    break
            _write_jsonl(self._praxis_path, items)

    def praxis_remove(self, praxis_id: str) -> bool:
        with _locked(self._praxis_path):
            items = _read_jsonl(self._praxis_path)
            new_items = [i for i in items if i["id"] != praxis_id]
            if len(new_items) == len(items):
                return False
            _write_jsonl(self._praxis_path, new_items)
        return True


def _today_str_to_date(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")
