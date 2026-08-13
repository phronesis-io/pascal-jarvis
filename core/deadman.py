"""Optional external dead-man heartbeat for whole-machine outages.

The local guardian can restart Jarvis after a process failure, but it cannot
page anyone while the Mac is powered off or waiting at FileVault login. A
configured external service can alert when these success pings stop.

The endpoint may contain a secret token. This module never logs or returns the
URL and stores only timestamps in the runtime data directory.
"""

from __future__ import annotations

import argparse
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from core.config import Config


ATTEMPT_STAMP = ".external_deadman_attempt"
SUCCESS_STAMP = ".external_deadman_ok"
MIN_INTERVAL_SECONDS = 60
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class DeadmanResult:
    status: str
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return self.status in {"disabled", "not_due", "ok"}


def _settings(root: Path) -> dict:
    cfg = Config(root / "jarvis.yaml")
    raw = cfg.get("ops.deadman", {}) or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "url": str(os.environ.get("JARVIS_DEADMAN_URL") or raw.get("url", "")).strip(),
        "interval": max(
            MIN_INTERVAL_SECONDS,
            int(raw.get("interval_seconds", DEFAULT_INTERVAL_SECONDS) or DEFAULT_INTERVAL_SECONDS),
        ),
        "timeout": max(
            1,
            min(30, int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS) or DEFAULT_TIMEOUT_SECONDS)),
        ),
    }


def _stamp(root: Path, name: str) -> Path:
    return root / "data" / name


def _read_epoch(path: Path) -> float:
    try:
        return float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0


def _write_epoch(path: Path, value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(value), encoding="utf-8")
    os.replace(tmp, path)


def _valid_endpoint(url: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.netloc:
        return True
    return parsed.scheme == "http" and parsed.hostname in {
        "127.0.0.1", "localhost", "::1",
    }


def ping_due(root: str | Path, *, now: float | None = None, opener=None) -> DeadmanResult:
    """Ping the configured endpoint at most once per configured interval."""
    base = Path(root)
    settings = _settings(base)
    if not settings["enabled"]:
        return DeadmanResult("disabled", "not enabled")
    if not _valid_endpoint(settings["url"]):
        return DeadmanResult("failed", "endpoint missing or invalid")

    epoch = time.time() if now is None else float(now)
    attempt = _stamp(base, ATTEMPT_STAMP)
    if epoch - _read_epoch(attempt) < settings["interval"]:
        return DeadmanResult("not_due", "rate limited")

    # Persist before I/O: a failing endpoint must not be hammered every 30s.
    try:
        _write_epoch(attempt, epoch)
    except OSError:
        return DeadmanResult("failed", "attempt stamp unavailable")

    request = urllib.request.Request(
        settings["url"],
        headers={"User-Agent": "pascal-jarvis/deadman"},
        method="GET",
    )
    try:
        open_url = opener or urllib.request.urlopen
        response = open_url(request, timeout=settings["timeout"])
        try:
            status_code = int(getattr(response, "status", 200) or 200)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if status_code >= 400:
            return DeadmanResult("failed", f"endpoint returned HTTP {status_code}")
        _write_epoch(_stamp(base, SUCCESS_STAMP), epoch)
        return DeadmanResult("ok", "external heartbeat accepted")
    except Exception as exc:
        # Exception text can contain the tokenized URL. The type is enough for
        # operations and keeps credentials out of logs and component reports.
        return DeadmanResult("failed", f"request failed ({type(exc).__name__})")


def status(root: str | Path, *, now: float | None = None) -> DeadmanResult:
    """Read configuration and the last successful ping without network I/O."""
    base = Path(root)
    settings = _settings(base)
    if not settings["enabled"]:
        return DeadmanResult("disabled", "not enabled")
    if not _valid_endpoint(settings["url"]):
        return DeadmanResult("failed", "endpoint missing or invalid")
    success = _read_epoch(_stamp(base, SUCCESS_STAMP))
    if not success:
        return DeadmanResult("failed", "no successful external heartbeat yet")
    epoch = time.time() if now is None else float(now)
    max_age = max(settings["interval"] * 3, 15 * 60)
    age = max(0, epoch - success)
    if age > max_age:
        return DeadmanResult("failed", f"last success is {int(age)}s old")
    return DeadmanResult("ok", f"last success is {int(age)}s old")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="External dead-man heartbeat")
    parser.add_argument("command", choices=("ping", "status"), nargs="?", default="status")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    result = ping_due(args.root) if args.command == "ping" else status(args.root)
    print(f"{result.status}: {result.detail}")
    return 0 if result.healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
