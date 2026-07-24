"""Small, deterministic runtime resource checks for resident processes."""

from __future__ import annotations

import argparse
import re
import resource
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FdHealth:
    pid: int
    count: int
    limit: int
    state: str

    @property
    def ratio(self) -> float:
        return self.count / self.limit if self.limit > 0 else 0.0

    def line(self) -> str:
        percent = round(self.ratio * 100)
        if self.state == "critical":
            return (
                f"⚠️ PID {self.pid} 文件描述符接近耗尽："
                f"{self.count}/{self.limit}（{percent}%）"
            )
        if self.state == "warning":
            return (
                f"⚠️ PID {self.pid} 文件描述符持续偏高："
                f"{self.count}/{self.limit}（{percent}%）"
            )
        return f"✓ PID {self.pid} 文件描述符：{self.count}/{self.limit}"


def open_fd_count(pid: int) -> int | None:
    """Return a process FD count on Linux or macOS, None if unsupported."""
    proc = Path(f"/proc/{int(pid)}/fd")
    if proc.is_dir():
        try:
            return sum(1 for _ in proc.iterdir())
        except OSError:
            return None
    if not shutil.which("lsof"):
        return None
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(int(pid)), "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return sum(
        1 for line in result.stdout.splitlines()
        if re.fullmatch(r"f\d+", line.strip())
    )


def soft_fd_limit() -> int | None:
    candidates: list[int] = []
    try:
        value = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (OSError, ValueError):
        value = None
    if value not in (None, -1, resource.RLIM_INFINITY) and value > 0:
        candidates.append(int(value))

    # Jarvis is supervised by launchd. A terminal Python can report a much
    # larger RLIMIT than launchd's service domain (observed: 1,048,575 vs
    # 256), so the current process limit would miss the exact production
    # exhaustion this check exists to detect.
    if sys.platform == "darwin" and shutil.which("launchctl"):
        try:
            result = subprocess.run(
                ["launchctl", "limit", "maxfiles"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            values = [
                int(token)
                for token in result.stdout.split()
                if token.isdigit() and int(token) > 0
            ]
            if result.returncode == 0 and values:
                candidates.append(values[0])
        except (OSError, subprocess.SubprocessError):
            pass
    return min(candidates) if candidates else None


def evaluate_fd_health(
    pid: int,
    *,
    count: int | None = None,
    limit: int | None = None,
    warning_ratio: float = 0.60,
    critical_ratio: float = 0.80,
) -> FdHealth | None:
    count = open_fd_count(pid) if count is None else int(count)
    limit = soft_fd_limit() if limit is None else int(limit)
    if count is None or limit is None or limit <= 0:
        return None
    ratio = count / limit
    state = (
        "critical"
        if ratio >= critical_ratio
        else "warning"
        if ratio >= warning_ratio
        else "healthy"
    )
    return FdHealth(pid=int(pid), count=count, limit=limit, state=state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resident process resource health")
    parser.add_argument("--pid", required=True, type=int)
    args = parser.parse_args(argv)
    if args.pid <= 0:
        return 2
    health = evaluate_fd_health(args.pid)
    if health is None:
        print(f"(PID {args.pid} 文件描述符检查不可用)")
        return 0
    print(health.line())
    return 1 if health.state == "critical" else 0


if __name__ == "__main__":
    raise SystemExit(main())
