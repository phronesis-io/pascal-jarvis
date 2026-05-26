#!/usr/bin/env python3
"""Run Claude CLI in an isolated process session (setsid).

Prevents SIGTERM propagation from parent process group (lark-cli pipe
breaks, daemon restarts, watchdog signals) from killing Claude mid-response.

Environment variables (set by bot.sh):
  JV_CONTENT  — user message text (piped to Claude's stdin)
  JV_SYS      — system prompt (--append-system-prompt)
  JV_OUT      — output file path (stdout → JV_OUT, stderr → JV_OUT.stderr)
  JV_SID      — session ID
  JV_CWD      — working directory for Claude
  JV_MODE     — "resume" or "new"
"""

import os
import subprocess
import sys

def main():
    # Isolate into own process session — this is the whole point
    os.setsid()

    content = os.environ["JV_CONTENT"]
    sys_prompt = os.environ["JV_SYS"]
    out_file = os.environ["JV_OUT"]
    session_id = os.environ["JV_SID"]
    cwd = os.environ["JV_CWD"]
    mode = os.environ.get("JV_MODE", "new")

    cmd = ["claude", "-p"]
    if mode == "resume":
        cmd.extend(["--resume", session_id])
    else:
        cmd.extend(["--session-id", session_id])
    cmd.extend([
        "--append-system-prompt", sys_prompt,
        "--dangerously-skip-permissions",
    ])

    try:
        with open(out_file, "w") as stdout_f, \
             open(f"{out_file}.stderr", "w") as stderr_f:
            result = subprocess.run(
                cmd,
                input=content,
                text=True,
                stdout=stdout_f,
                stderr=stderr_f,
                cwd=cwd,
                timeout=600,
            )
        # Write exit code
        with open(f"{out_file}.exit", "w") as f:
            f.write(str(result.returncode))
    except subprocess.TimeoutExpired:
        with open(f"{out_file}.exit", "w") as f:
            f.write("timeout")
    except Exception as e:
        with open(f"{out_file}.stderr", "a") as f:
            f.write(f"\nclaude_isolated.py error: {e}\n")
        with open(f"{out_file}.exit", "w") as f:
            f.write("error")

if __name__ == "__main__":
    main()
