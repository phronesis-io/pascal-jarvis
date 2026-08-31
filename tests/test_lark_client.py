"""Executable contracts for latency-sensitive Lark shell helpers."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time


ROOT = Path(__file__).resolve().parent.parent
CLIENT = ROOT / "plugins" / "lark" / "client.sh"


def test_reaction_removal_has_a_hard_wall_clock_bound(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation = tmp_path / "invocation"
    fake_cli = fake_bin / "lark-cli"
    fake_cli.write_text(
        "#!/bin/sh\n"
        "printf called > \"$INVOCATION\"\n"
        "trap '' TERM\n"
        "/bin/sleep 30\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "JARVIS_DIR": str(ROOT),
        "LOG_FILE": str(tmp_path / "lark.log"),
        "INVOCATION": str(invocation),
    }

    started = time.monotonic()
    result = subprocess.run(
        [
            "/bin/bash", "-c",
            f'source "{CLIENT}"; lark_remove_reaction message reaction',
        ],
        env=env,
        timeout=3,
    )

    assert result.returncode == 0
    assert invocation.read_text(encoding="utf-8") == "called"
    assert time.monotonic() - started < 2
