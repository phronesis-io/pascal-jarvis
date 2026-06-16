"""Component manifest checks — "what should be running" as data (REQ-40).

The dashboard died for 23 days because four supervision surfaces each kept a
hardcoded, divergent component list. components.yaml is now the single source
of truth and this module the single checker, consumed by:

  - tasks/self_diagnostic_pre.sh   (every 4h, full list → ⚠️ lines → REQ-39 alert)
  - daemon.py                      (every 30s, critical subset, alert-only)
  - scripts/doctor.sh              (install-time)
  - restart.sh --status            (operator view)

CLI:
    python3 -m core.components            # full report, ⚠️ lines on failure
    python3 -m core.components --critical # critical subset only
    python3 -m core.components --json     # machine-readable
Exit code: number of failing components (0 = all green), capped at 100.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "components.yaml"


def load_manifest(path: Path | None = None) -> list[dict]:
    import yaml
    p = path or MANIFEST
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except (OSError, Exception):
        return []
    return list(data.get("components", []))


def _check_pid(comp: dict, root: Path) -> tuple[bool, str]:
    pid_file = root / comp.get("path", "")
    try:
        first = pid_file.read_text().strip().split()[0]
        pid = int(first)
    except (OSError, ValueError, IndexError):
        return False, "pidfile missing/unreadable"
    try:
        os.kill(pid, 0)
        return True, f"pid {pid} alive"
    except (ProcessLookupError, PermissionError):
        return False, f"pid {pid} dead"


def _read_pidfile(root: Path, path: str) -> int | None:
    try:
        return int((root / path).read_text().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _has_ancestor(pid: int, ancestor: int, procs: dict[int, tuple[int, str]]) -> bool:
    seen = set()
    cur = pid
    while cur and cur not in seen:
        if cur == ancestor:
            return True
        seen.add(cur)
        cur = procs.get(cur, (0, ""))[0]
    return False


def _check_pgrep(comp: dict, root: Path) -> tuple[bool, str]:
    pattern = comp.get("pattern", "")
    owner = _read_pidfile(root, comp.get("owned_by_pidfile", "")) \
        if comp.get("owned_by_pidfile") else None
    try:
        # macOS pgrep -f only inspects a truncated cmdline (~first 100 chars),
        # so a long interpreter path prefix can hide patterns like
        # "-m core.heartbeat_loop". Use a full-cmdline ps scan as the source
        # of truth; it also lets us filter out this diagnostic command and
        # orphaned processes not owned by bot.sh.
        ps = subprocess.run(["ps", "ax", "-o", "pid=,command="],
                            capture_output=True, text=True, timeout=5)
        ps_tree = subprocess.run(["ps", "ax", "-o", "pid=,ppid=,command="],
                                 capture_output=True, text=True, timeout=5)
        procs: dict[int, tuple[int, str]] = {}
        for line in ps_tree.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) < 3:
                continue
            try:
                procs[int(parts[0])] = (int(parts[1]), parts[2])
            except ValueError:
                continue
        pids = []
        for line in ps.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            pid, _, cmd = line.partition(" ")
            try:
                pid_i = int(pid)
            except ValueError:
                continue
            if not pattern or pattern not in cmd:
                continue
            if (
                "core.components" in cmd
                or "rg " in cmd
                or " --system-prompt " in cmd
                or "/claude" in cmd
                or cmd.startswith("claude ")
            ):
                continue
            if owner and not _has_ancestor(pid_i, owner, procs):
                continue
            pids.append(str(pid_i))
        if pids:
            detail = f"pids {pids}"
            if owner:
                detail += f" owned by {owner}"
            return True, detail
        if owner:
            return False, f"no process owned by pid {owner}"
        return False, "no process"
    except Exception as e:
        return False, f"pgrep error: {e}"


def _check_http(comp: dict, root: Path) -> tuple[bool, str]:
    url = comp.get("url", "")
    try:
        import urllib.request
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status < 500
            return ok, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"unreachable ({type(e).__name__})"


def _check_file_age(comp: dict, root: Path) -> tuple[bool, str]:
    import time
    p = root / comp.get("path", "")
    max_h = float(comp.get("max_age_hours", 24))
    try:
        age_h = (time.time() - p.stat().st_mtime) / 3600
    except OSError:
        return False, "file missing"
    return (age_h <= max_h,
            f"age {age_h:.1f}h (max {max_h:.0f}h)")


def _check_launchctl(comp: dict, root: Path) -> tuple[bool, str]:
    label = comp.get("label", "")
    try:
        uid = os.getuid()
        r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                           capture_output=True, text=True, timeout=5)
        return (r.returncode == 0,
                "loaded" if r.returncode == 0 else "not loaded")
    except Exception as e:
        return False, f"launchctl error: {e}"


_CHECKS = {
    "pid": _check_pid,
    "pgrep": _check_pgrep,
    "http": _check_http,
    "file_age": _check_file_age,
    "launchctl": _check_launchctl,
}


def check_components(critical_only: bool = False,
                     manifest_path: Path | None = None,
                     root: Path | None = None) -> list[dict]:
    """Run all manifest checks. Returns [{name, ok, detail, critical}, ...]."""
    root = root or ROOT
    results = []
    for comp in load_manifest(manifest_path):
        if critical_only and not comp.get("critical", False):
            continue
        fn = _CHECKS.get(comp.get("check", ""))
        if fn is None:
            results.append({"name": comp.get("name", "?"), "ok": False,
                            "detail": f"unknown check type {comp.get('check')!r}",
                            "critical": bool(comp.get("critical", False))})
            continue
        try:
            ok, detail = fn(comp, root)
        except Exception as e:  # a checker bug must not kill the report
            ok, detail = False, f"checker crashed: {e}"
        results.append({"name": comp.get("name", "?"), "ok": ok,
                        "detail": detail,
                        "critical": bool(comp.get("critical", False))})
    return results


def format_report(results: list[dict]) -> str:
    """Human/diagnostic report — failures as ⚠️ lines (REQ-39 picks them up)."""
    lines = ["--- Components ---"]
    for r in results:
        mark = "✓" if r["ok"] else "⚠️"
        crit = " [critical]" if r["critical"] and not r["ok"] else ""
        lines.append(f"  {mark} {r['name']}: {r['detail']}{crit}")
    bad = sum(1 for r in results if not r["ok"])
    lines.append(f"  ({len(results) - bad}/{len(results)} healthy)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    critical = "--critical" in argv
    results = check_components(critical_only=critical)
    if "--json" in argv:
        print(json.dumps(results, ensure_ascii=False))
    else:
        print(format_report(results))
    return min(100, sum(1 for r in results if not r["ok"]))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
