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
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "components.yaml"

# Per-root config cache — check_components can be called every 30s by the
# daemon; re-parsing jarvis.yaml per component would be wasteful.
_config_cache: dict[str, object] = {}


def _config_get(root: Path, dotpath: str):
    """Read a dotted key from <root>/jarvis.yaml. Missing/broken config →
    None (treated as 'not configured'). Never raises."""
    key = str(root)
    if key not in _config_cache:
        try:
            from core.config import Config
            _config_cache[key] = Config(root / "jarvis.yaml")
        except Exception:
            _config_cache[key] = None
    cfg = _config_cache[key]
    if cfg is None:
        return None
    try:
        return cfg.get(dotpath)
    except Exception:
        return None


def _gate_reason(comp: dict, root: Path) -> str | None:
    """Fresh-install/optional-feature gate (2026-07-13): components.yaml used
    to mark ef-stream/lark-sidecar/admin critical UNCONDITIONALLY while
    doctor.sh calls the same features optional — a collaborator's default
    install (admin.enabled: false, no eigenflux CLI, no launchd services)
    alarmed [critical] forever on components that were never supposed to run.
    A component whose declared precondition is unmet is SKIPPED (reported ok,
    marked skipped), not red. Preconditions:
      requires_cmd:    <binary>            — command must exist on PATH
      requires_file:   <path>              — file must exist (~ expanded;
                                             relative paths resolve from root)
      requires_config: <dot.path>[=value]  — jarvis.yaml key truthy / == value
    Returns the human-readable skip reason, or None when armed."""
    cmd = comp.get("requires_cmd")
    if cmd and shutil.which(str(cmd)) is None:
        return f"{cmd} not installed"
    fpath = comp.get("requires_file")
    if fpath:
        p = Path(os.path.expanduser(str(fpath)))
        if not p.is_absolute():
            p = root / p
        if not p.exists():
            return f"{fpath} not installed"
    ckey = comp.get("requires_config")
    if ckey:
        ckey = str(ckey)
        expected = None
        if "=" in ckey:
            ckey, expected = ckey.split("=", 1)
        val = _config_get(root, ckey)
        if expected is None:
            if not val:
                return f"{ckey} not enabled in jarvis.yaml"
        elif str(val) != expected:
            return f"{ckey} != {expected} in jarvis.yaml"
    return None


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
            # Wording matters: "no process owned by pid X" read as if two
            # components shared one PID (X is the BOT's pid, the same owner
            # for every child — 2026-07-13 confusion). Say what it means.
            return False, f"not running (bot pid {owner} has no matching child process)"
        return False, "no process"
    except Exception as e:
        return False, f"pgrep error: {e}"


def _check_http(comp: dict, root: Path) -> tuple[bool, str]:
    url = comp.get("url", "")
    try:
        import urllib.request
        import urllib.error
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            ok = resp.status < 500
            return ok, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        # The server responded — it's reachable, not down. A 5xx (e.g. /health
        # returning 503 "degraded" because a circuit is open) is a soft failure;
        # do NOT report it as "unreachable", which falsely reads as a dead process.
        if e.code >= 500:
            return False, f"degraded (HTTP {e.code})"
        return True, f"HTTP {e.code}"
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


def _check_audit_age(comp: dict, root: Path) -> tuple[bool, str]:
    p = root / comp.get("path", "")
    max_h = float(comp.get("max_age_hours", 24))
    if not p.is_file():
        return False, "database missing"
    try:
        from core.conversation_audit import connect
        db = connect(p)
        row = db.execute(
            "SELECT completed_at FROM audit_runs "
            "WHERE completed_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        return False, f"audit read failed ({type(exc).__name__})"
    finally:
        if "db" in locals():
            db.close()
    if row is None:
        return False, "no completed audit run"
    try:
        completed = datetime.fromisoformat(str(row[0]))
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        age_h = (
            datetime.now(timezone.utc) - completed.astimezone(timezone.utc)
        ).total_seconds() / 3600
    except (TypeError, ValueError, OverflowError):
        return False, "invalid completed audit timestamp"
    return (
        0 <= age_h <= max_h,
        f"completed age {age_h:.1f}h (max {max_h:.0f}h)",
    )


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


def _check_tailnet(comp: dict, root: Path) -> tuple[bool, str]:
    try:
        from core.tailnet import tailnet_status
        status = tailnet_status(
            int(comp.get("port", 3458)), mode=comp.get("mode"))
    except Exception as e:
        return False, f"tailnet check failed: {e}"
    return bool(status.get("ready")), str(status.get("detail") or "not served")


def _check_taskline(comp: dict, root: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["taskline", "status"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        return False, f"status probe failed ({type(exc).__name__})"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "probe failed").strip()
        return False, detail[:240]
    try:
        payload = json.loads(result.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, "status probe returned invalid JSON"
    healthy = bool(payload.get("healthy") and payload.get("registered"))
    return healthy, (
        "healthy and workspace registered"
        if healthy
        else "server unhealthy or workspace unregistered"
    )


_CHECKS = {
    "pid": _check_pid,
    "pgrep": _check_pgrep,
    "http": _check_http,
    "file_age": _check_file_age,
    "audit_age": _check_audit_age,
    "launchctl": _check_launchctl,
    "tailnet": _check_tailnet,
    "taskline": _check_taskline,
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
        reason = _gate_reason(comp, root)
        if reason:
            # ok=True keeps every consumer (daemon critical probe, ⚠️ line
            # extraction, exit code) treating an unconfigured optional
            # feature as healthy; skipped=True lets reports label it.
            results.append({"name": comp.get("name", "?"), "ok": True,
                            "skipped": True,
                            "detail": f"skipped — {reason}",
                            "critical": bool(comp.get("critical", False))})
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
        mark = "○" if r.get("skipped") else ("✓" if r["ok"] else "⚠️")
        crit = " [critical]" if r["critical"] and not r["ok"] else ""
        lines.append(f"  {mark} {r['name']}: {r['detail']}{crit}")
    bad = sum(1 for r in results if not r["ok"])
    skipped = sum(1 for r in results if r.get("skipped"))
    tail = f"  ({len(results) - bad}/{len(results)} healthy"
    if skipped:
        tail += f", {skipped} skipped — optional features not configured"
    lines.append(tail + ")")
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
