"""Executable macOS Bash 3.2 regressions for dispatcher process groups."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parent.parent
HELPERS = ROOT / "scripts" / "process_lifecycle.sh"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _group_members(pgid: int) -> list[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid="], capture_output=True, text=True, check=True,
    )
    members = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and int(fields[1]) == pgid:
            members.append(int(fields[0]))
    return members


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition did not become true before timeout")


def test_identity_guard_rejects_root_pid(tmp_path):
    script = tmp_path / "identity.sh"
    script.write_text(
        f'source "{HELPERS}"\n'
        'token=$(process_start_token "$$")\n'
        'if process_is_descendant "$$" "$$"; then exit 9; fi\n'
        'if process_group_is_owned "$$" "$token" "$$"; then exit 10; fi\n',
        encoding="utf-8",
    )
    result = subprocess.run(["/bin/bash", str(script)], timeout=5)
    assert result.returncode == 0


def test_atomic_registry_stops_continuously_forking_group(tmp_path):
    marker = tmp_path / ".dispatch_conv_stress"
    ready = tmp_path / "ready"
    script = tmp_path / "forking-handler.sh"
    script.write_text(
        f'source "{HELPERS}"\nmarker="$1"\nready="$2"\n'
        'handler(){\n'
        "  own=$(/bin/sh -c 'printf \"%s\" \"$PPID\"')\n"
        '  token=$(process_start_token "$own")\n'
        '  dispatch_marker_wait_owned "$marker" "$own" "$token" 100 || exit 74\n'
        '  : > "$ready"\n'
        '  while :; do sleep 30 & sleep 0.01; done\n'
        '}\n'
        'set -m\nhandler & leader=$!\nset +m\n'
        'token=$(process_start_token "$leader")\n'
        'dispatch_marker_publish "$marker" "$leader" "$token"\n'
        'printf \'%s\\t%s\\n\' "$leader" "$token"\nwait\n',
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["/bin/bash", str(script), str(marker), str(ready)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    leader_text, token = process.stdout.readline().rstrip("\n").split("\t", 1)
    leader = int(leader_text)
    _wait_until(lambda: ready.exists() and len(_group_members(leader)) >= 2)
    stored = marker.read_text(encoding="utf-8").rstrip("\n").split("\t", 1)
    assert stored == [str(leader), token]

    killer = subprocess.run(
        [
            "/bin/bash", "-c",
            f'source "{HELPERS}"; terminate_registered_group "$1" "$2"',
            "_", str(marker), str(process.pid),
        ],
        capture_output=True, text=True, timeout=8,
    )
    assert killer.returncode == 0, killer.stderr
    process.wait(timeout=5)
    _wait_until(lambda: not _group_members(leader))


def test_group_cleanup_continues_after_leader_exits_before_child(tmp_path):
    marker = tmp_path / ".dispatch_conv_early_leader"
    ready = tmp_path / "ready"
    child_pid_file = tmp_path / "child.pid"
    child = tmp_path / "ignore-term.py"
    child.write_text(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "while True: time.sleep(1)\n",
        encoding="utf-8",
    )
    script = tmp_path / "early-leader.sh"
    script.write_text(
        f'source "{HELPERS}"\nmarker="$1"\nready="$2"\npid_file="$3"\n'
        'handler(){\n'
        "  own=$(/bin/sh -c 'printf \"%s\" \"$PPID\"')\n"
        '  token=$(process_start_token "$own")\n'
        '  trap "exit 0" TERM\n'
        '  dispatch_marker_wait_owned "$marker" "$own" "$token" 100 || exit 74\n'
        f'  python3 "{child}" & child_pid=$!\n'
        '  printf \'%s\' "$child_pid" > "$pid_file"\n'
        '  : > "$ready"\n'
        '  wait "$child_pid"\n'
        '}\n'
        'set -m\nhandler & leader=$!\nset +m\n'
        'token=$(process_start_token "$leader")\n'
        'dispatch_marker_publish "$marker" "$leader" "$token"\n'
        'printf \'%s\\n\' "$leader"\nwait\n',
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["/bin/bash", str(script), str(marker), str(ready), str(child_pid_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    leader = int(process.stdout.readline())
    _wait_until(lambda: ready.exists() and child_pid_file.exists())
    child_pid = int(child_pid_file.read_text(encoding="utf-8"))

    killer = subprocess.run(
        [
            "/bin/bash", "-c",
            f'source "{HELPERS}"; terminate_registered_group "$1" "$2"',
            "_", str(marker), str(process.pid),
        ],
        capture_output=True, text=True, timeout=8,
    )
    assert killer.returncode == 0, killer.stderr
    process.wait(timeout=5)
    _wait_until(lambda: not _alive(child_pid))
    assert not _group_members(leader)


def test_no_child_abnormal_exit_removes_owned_marker(tmp_path):
    marker = tmp_path / ".dispatch_conv_abnormal"
    script = tmp_path / "abnormal-handler.sh"
    script.write_text(
        f'source "{HELPERS}"\nset -u\nmarker="$1"\nroot=$$\n'
        'handler(){\n'
        "  own=$(/bin/sh -c 'printf \"%s\" \"$PPID\"')\n"
        '  token=$(process_start_token "$own")\n'
        '  finish(){ dispatch_marker_remove_owned "$marker" "$own" "$token"; trap - EXIT TERM INT; }\n'
        '  abort(){ finish; kill -KILL -- "-$own" 2>/dev/null || true; }\n'
        "  trap 'abort' EXIT\n  trap 'abort; exit 143' TERM INT\n"
        '  dispatch_marker_wait_owned "$marker" "$own" "$token" 100 || exit 74\n'
        '  printf \'%s\' "$definitely_unbound" >/dev/null\n'
        '}\n'
        'set -m\nhandler & leader=$!\nset +m\n'
        'token=$(process_start_token "$leader")\n'
        'dispatch_marker_publish "$marker" "$leader" "$token"\nwait\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", str(script), str(marker)],
        capture_output=True, text=True, timeout=8,
    )
    assert "unbound variable" in result.stderr
    assert not marker.exists()


def test_atomic_handoff_sidecar_lets_parent_remove_promoted_marker(tmp_path):
    old = tmp_path / ".dispatch_conv_old"
    new = tmp_path / ".dispatch_job_new"
    sidecar = tmp_path / "answer.dispatch_marker"
    script = tmp_path / "watchdog-rehome.sh"
    script.write_text(
        f'source "{HELPERS}"\n'
        'handler(){\n'
        "  own=$(/bin/sh -c 'printf \"%s\" \"$PPID\"')\n"
        '  token=$(process_start_token "$own")\n'
        '  marker="$1"\n'
        '  dispatch_marker_wait_owned "$marker" "$own" "$token" 100 || exit 74\n'
        '  ( dispatch_marker_handoff_owned "$marker" "$2" "$3" "$own" "$token" ) &\n'
        '  wait\n'
        '  [ -f "$3" ] && marker=$(cat "$3")\n'
        '  dispatch_marker_remove_owned "$marker" "$own" "$token"\n'
        '}\n'
        'set -m\nhandler "$1" "$2" "$3" & leader=$!\nset +m\n'
        'token=$(process_start_token "$leader")\n'
        'dispatch_marker_publish "$1" "$leader" "$token"\nwait\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", str(script), str(old), str(new), str(sidecar)], timeout=8,
    )
    assert result.returncode == 0
    assert not old.exists()
    assert not new.exists()


def test_failed_handoff_keeps_original_marker_discoverable(tmp_path):
    old = tmp_path / ".dispatch_conv_old"
    new = tmp_path / ".dispatch_job_new"
    missing_sidecar = tmp_path / "missing" / "answer.dispatch_marker"
    script = tmp_path / "failed-handoff.sh"
    script.write_text(
        f'source "{HELPERS}"\n'
        'token=$(process_start_token "$$")\n'
        'dispatch_marker_publish "$1" "$$" "$token"\n'
        'dispatch_marker_handoff_owned "$1" "$2" "$3" "$$" "$token"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["/bin/bash", str(script), str(old), str(new), str(missing_sidecar)],
        timeout=5,
    )
    assert result.returncode != 0
    assert old.exists()
    assert not new.exists()


def test_wrong_start_token_never_kills_live_group(tmp_path):
    marker = tmp_path / ".dispatch_wrong_token"
    script = tmp_path / "sleeper.sh"
    script.write_text("set -m\nsleep 30 & p=$!\nset +m\nprintf '%s\\n' \"$p\"\nwait\n", encoding="utf-8")
    process = subprocess.Popen(
        ["/bin/bash", str(script)], stdout=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    leader = int(process.stdout.readline())
    marker.write_text(f"{leader}\twrong-token\n", encoding="utf-8")
    killer = subprocess.run(
        [
            "/bin/bash", "-c",
            f'source "{HELPERS}"; terminate_registered_group "$1"',
            "_", str(marker),
        ],
        timeout=5,
    )
    assert killer.returncode != 0
    assert _alive(leader)
    os.killpg(leader, 9)
    process.wait(timeout=5)


def test_session_lock_rejects_reused_pid_identity(tmp_path):
    lock = tmp_path / ".session_lock_test"
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        lock.write_text(f"{sleeper.pid}\twrong-start\towner-token\n", encoding="utf-8")
        result = subprocess.run(
            [
                "/bin/bash", "-c",
                f'source "{HELPERS}"; session_lock_identity "$1"',
                "_", str(lock),
            ],
            timeout=5,
        )
        assert result.returncode != 0
        assert _alive(sleeper.pid)
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_session_lock_requires_matching_live_handler_owner(tmp_path):
    child_pid_file = tmp_path / "provider.pid"
    script = tmp_path / "handler-owner.sh"
    script.write_text(
        f'source "{HELPERS}"\npid_file="$1"\n'
        'handler(){ sleep 30 & provider=$!; printf \'%s\' "$provider" > "$pid_file"; wait; }\n'
        'set -m\nhandler & leader=$!\nset +m\n'
        'token=$(process_start_token "$leader")\n'
        'printf \'%s\\t%s\\n\' "$leader" "$token"\nwait\n',
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["/bin/bash", str(script), str(child_pid_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    leader_text, handler_start = process.stdout.readline().rstrip("\n").split("\t", 1)
    leader = int(leader_text)
    _wait_until(child_pid_file.exists)
    provider = int(child_pid_file.read_text(encoding="utf-8"))
    provider_start = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(provider)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    lock = tmp_path / ".session_lock_test"
    try:
        lock.write_text(
            f"{provider}\t{provider_start}\tforeign-owner\n", encoding="utf-8",
        )
        rejected = subprocess.run(
            [
                "/bin/bash", "-c",
                f'source "{HELPERS}"; session_lock_identity_for_handler "$1" "$2" "$3"',
                "_", str(lock), str(leader), handler_start,
            ],
            timeout=5,
        )
        assert rejected.returncode != 0
        lock.write_text(
            f"{provider}\t{provider_start}\t{leader}|{handler_start}|nonce\n",
            encoding="utf-8",
        )
        accepted = subprocess.run(
            [
                "/bin/bash", "-c",
                f'source "{HELPERS}"; session_lock_identity_for_handler "$1" "$2" "$3"',
                "_", str(lock), str(leader), handler_start,
            ],
            capture_output=True, text=True, timeout=5,
        )
        assert accepted.returncode == 0, accepted.stderr
        assert accepted.stdout.split("\t", 1)[0] == str(provider)
    finally:
        os.killpg(leader, 9)
        process.wait(timeout=5)


def test_group_term_lets_wrapper_reap_detached_session_and_clear_reaction(tmp_path):
    marker = tmp_path / ".dispatch_conv_detached"
    reaction = tmp_path / "reaction-cleared"
    detached_pid_file = tmp_path / "detached.pid"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import os, pathlib, signal, subprocess, sys, time\n"
        "pid_file = pathlib.Path(sys.argv[1])\n"
        "child = subprocess.Popen(['sleep', '30'], start_new_session=True)\n"
        "pid_file.write_text(str(child.pid))\n"
        "def stop(*_):\n"
        "    try: os.killpg(child.pid, signal.SIGKILL)\n"
        "    except ProcessLookupError: pass\n"
        "    child.wait()\n"
        "    raise SystemExit(143)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    script = tmp_path / "managed-handler.sh"
    script.write_text(
        f'source "{HELPERS}"\nmarker="$1"\nreaction="$2"\npid_file="$3"\n'
        'handler(){\n'
        "  own=$(/bin/sh -c 'printf \"%s\" \"$PPID\"')\n"
        '  token=$(process_start_token "$own")\n'
        '  finish(){ printf cleared > "$reaction"; dispatch_marker_remove_owned "$marker" "$own" "$token"; trap - EXIT TERM INT; }\n'
        "  trap 'finish' EXIT\n  trap 'finish; exit 143' TERM INT\n"
        '  dispatch_marker_wait_owned "$marker" "$own" "$token" 100 || exit 74\n'
        f'  python3 "{wrapper}" "$pid_file" & wrapper_pid=$!\n'
        '  wait "$wrapper_pid"\n  finish\n'
        '}\n'
        'set -m\nhandler & leader=$!\nset +m\n'
        'token=$(process_start_token "$leader")\n'
        'dispatch_marker_publish "$marker" "$leader" "$token"\n'
        'printf \'%s\\n\' "$leader"\nwait\n',
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["/bin/bash", str(script), str(marker), str(reaction), str(detached_pid_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdout is not None
    leader = int(process.stdout.readline())
    _wait_until(lambda: detached_pid_file.exists())
    detached_pid = int(detached_pid_file.read_text(encoding="utf-8"))
    assert _alive(detached_pid)
    killer = subprocess.run(
        [
            "/bin/bash", "-c",
            f'source "{HELPERS}"; terminate_registered_group "$1" "$2"',
            "_", str(marker), str(process.pid),
        ],
        capture_output=True, text=True, timeout=8,
    )
    assert killer.returncode == 0, killer.stderr
    process.wait(timeout=5)
    _wait_until(lambda: not _alive(detached_pid))
    assert reaction.read_text(encoding="utf-8") == "cleared"
    assert not marker.exists()
