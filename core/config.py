"""Configuration loader for Jarvis Harness."""

import os
import yaml
from pathlib import Path

_DEFAULTS = {
    "data_dir": "~/.jarvis",
    "work_dir": "",
    "lark": {"user_id": "", "app_id": ""},
    "claude": {
        # Explicit "opus" (= Opus 4.8), NOT "" : an empty value made every
        # main-conversation claude call inherit the account's DEFAULT model.
        # When that default is a banned model (Fable), the whole bot would
        # fail. Pinning opus here severs that dependency.
        "main_model": "opus",
        "heartbeat_model": "opus",
        "max_session_size": 512000,
        # Optional Claude Code-compatible backup provider. When configured,
        # main chat can retry `claude -p` with ANTHROPIC_* env pointed at this
        # endpoint before falling back to OpenAI.
        "backup_enabled": True,
        "backup_auth_token": "",
        "backup_base_url": "",
        "backup_model": "",
        "backup2_enabled": False,
        "backup2_auth_token": "",
        "backup2_base_url": "",
        "backup2_model": "",
        # Bounded relay context. core.memory gives identity/safety first when
        # this is smaller than the primary memory budget.
        "backup_max_session_size": 100000,
        "backup_max_memory_chars": 40000,
        # Max seconds a single heartbeat Claude call may run. Raised from the
        # old hard-coded 300s so heartbeat tasks have room to fan out subagents
        # (Task/Agent) and wait for them. Bumping this lengthens the worst-case
        # time one cycle blocks other due tasks — keep it bounded.
        "heartbeat_timeout": 600,
    },
    "openai": {
        # Last-resort main-chat fallback when Claude Code fails with a
        # model/account limit. It is deliberately off unless an API key exists
        # and bot.sh sees a Claude model error.
        "fallback_enabled": True,
        "fallback_model": "gpt-5.5",
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "user_agent": "",
        "timeout": 120,
        "max_output_tokens": 4096,
    },
    "codex": {
        # Uses the local Codex CLI and existing ChatGPT login. If the binary
        # is absent, the route fails quickly and the API fallback remains next.
        "fallback_enabled": True,
        "fallback_model": "gpt-5.5",
        "binary": "",
        "timeout": 300,
    },
    "memory": {
        "dir": "memory",
        "hourly_min_entries": 3,
        "daily_min_entries": 5,
        "weekly_min_words": 200,
    },
    "heartbeat": {
        "check_interval": 10,
        "tasks_file": "HEARTBEAT.md",
    },
    "schedule": {
        "working_hours": {"start": 9, "end": 22},
        "daily_plan_window": {"start": "08:00", "end": "09:30"},
        "daily_reflect_window": {"start": "20:30", "end": "23:30"},
        "weekly_review": {"day": 7, "start": 10, "end": 12},
    },
    "thresholds": {
        "free_block_min_minutes": 30,
        "nudge_max_per_day": 2,
        "checkin_transition_minutes": 15,
        "max_batch_size": 4,
    },
    "admin": {
        "enabled": False,
        "port": 3456,
        "host": "127.0.0.1",
    },
    "ops": {
        "deadman": {
            "enabled": False,
            "url": "",
            "interval_seconds": 300,
            "timeout_seconds": 5,
        },
    },
    "triage_profile": {
        "domains": [],
        "projects": [],
        "organizations": [],
        "portfolio_sectors": [],
    },
    "plugins": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class Config:
    """Jarvis configuration backed by jarvis.yaml."""

    def __init__(self, config_path: str | Path | None = None):
        self._raw = dict(_DEFAULTS)
        if config_path is None:
            config_path = self._find_config()
        if config_path and Path(config_path).exists():
            with open(config_path, encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            self._raw = _deep_merge(self._raw, user_cfg)

        # Resolve paths
        self.jarvis_dir = Path(config_path).parent if config_path else Path.cwd()
        self.data_dir = Path(os.path.expanduser(self._raw["data_dir"]))
        self.memory_dir = self.data_dir / self._raw["memory"]["dir"]
        # work_dir: where Claude runs (file access, git, etc.). Defaults to jarvis_dir.
        _wd = self._raw.get("work_dir", "")
        self.work_dir = Path(os.path.expanduser(_wd)) if _wd else self.jarvis_dir

    @staticmethod
    def _find_config() -> Path | None:
        candidates = [
            Path.cwd() / "jarvis.yaml",
            Path.home() / ".jarvis" / "jarvis.yaml",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    @property
    def owner_name(self) -> str:
        """How the assistant refers to its owner in multi-party contexts
        (group chats). Per-user config — never hardcode a name in code."""
        return str(self._raw.get("owner_name", "") or "主人")

    @property
    def lark(self) -> dict:
        return self._raw.get("lark", {})

    @property
    def claude(self) -> dict:
        return self._raw.get("claude", {})

    @property
    def openai(self) -> dict:
        return self._raw.get("openai", {})

    @property
    def codex(self) -> dict:
        return self._raw.get("codex", {})

    @property
    def memory(self) -> dict:
        return self._raw.get("memory", {})

    @property
    def heartbeat(self) -> dict:
        return self._raw.get("heartbeat", {})

    @property
    def plugins(self) -> dict:
        return self._raw.get("plugins", {})

    @property
    def schedule(self) -> dict:
        return self._raw.get("schedule", {})

    @property
    def thresholds(self) -> dict:
        return self._raw.get("thresholds", {})

    @property
    def admin(self) -> dict:
        return self._raw.get("admin", {})

    @property
    def ops(self) -> dict:
        return self._raw.get("ops", {})

    def get(self, dotpath: str, default=None):
        """Get nested config value by dot-separated path: 'claude.heartbeat_model'."""
        parts = dotpath.split(".")
        node = self._raw
        for p in parts:
            if isinstance(node, dict):
                node = node.get(p)
            else:
                return default
            if node is None:
                return default
        return node
