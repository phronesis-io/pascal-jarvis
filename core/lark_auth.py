"""User-identity re-authorization — the device flow as one deterministic move.

When the lark-cli user token drops, every ``--as user`` channel (calendar,
mail, group ingest) goes dark. The recovery has been proven hands-free since
2026-07-05: start the device flow, hand Pascal the verification link in
Feishu, poll in the background, receipt on success. Before this module that
sequence lived only in operator memory — the「现在授权」card button could not
run it, so tapping it led nowhere (the 2026-08-07 dead end).

Entry points:
- ``ActionProcessor._do_lark_auth_login`` (card button)
- ``python3 -m core.lark_auth start`` (heartbeat tasks, operators)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

JARVIS_DIR = Path(__file__).resolve().parent.parent

# Device codes live 10 minutes; the poll subprocess blocks until authorized
# or expired, so its timeout only guards against a hung CLI.
POLL_TIMEOUT_S = 660

TRIGGER_PATH = Path("/tmp/jarvis-heartbeat-trigger")

_LINK_DM = (
    "**飞书授权链接（10 分钟内有效）**\n\n"
    "点开登录确认即可，user 身份信道会一起恢复；完成后我会自动回执，"
    "不用回话。\n\n{url}"
)
_RECEIPT_DM = ("✅ 授权成功。日历/邮件/团队群等 user 身份信道恢复中，"
               "下轮心跳自动回补，不用做任何事。")


def _cli_env() -> dict:
    env = os.environ.copy()
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    return env


def _owner_open_id() -> str:
    """Pascal's open_id: USER_ID env (bot.sh exports it) → jarvis.yaml."""
    uid = os.environ.get("USER_ID", "").strip()
    if uid:
        return uid
    try:
        from core.config import Config
        return str(Config(JARVIS_DIR / "jarvis.yaml").lark.get("user_id", "") or "")
    except Exception:
        return ""


def _send_dm(text: str, run=subprocess.run) -> bool:
    uid = _owner_open_id()
    if not uid:
        return False
    try:
        r = run(["lark-cli", "im", "+messages-send", "--as", "bot",
                 "--user-id", uid, "--markdown", text],
                capture_output=True, text=True, timeout=30, env=_cli_env())
        return r.returncode == 0
    except Exception as exc:
        print(f"lark_auth: DM send failed: {exc}", file=sys.stderr)
        return False


def _user_token_ready(run=subprocess.run) -> bool:
    try:
        r = run(["lark-cli", "auth", "status", "--json", "--verify"],
                capture_output=True, text=True, timeout=30, env=_cli_env())
        user = json.loads(r.stdout).get("identities", {}).get("user", {})
        return str(user.get("status", "")) == "ready"
    except Exception:
        return False


def start_device_flow(run=subprocess.run, popen=subprocess.Popen) -> str:
    """Mint a device code, DM the link, detach the poller.

    Returns a human-readable receipt (card action_result / task output).
    Raises RuntimeError when the flow could not be started — the caller's
    honest-toast path must surface that, never claim success.
    """
    r = run(["lark-cli", "auth", "login", "--no-wait", "--json",
             "--domain", "all"],
            capture_output=True, text=True, timeout=30, env=_cli_env())
    if r.returncode != 0:
        raise RuntimeError(
            (r.stderr or r.stdout or "lark-cli auth login failed").strip()[:300])
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"device flow response not JSON: {exc}") from exc
    url = str(data.get("verification_url", "")).strip()
    code = str(data.get("device_code", "")).strip()
    if not url or not code:
        raise RuntimeError("device flow response missing verification_url/device_code")

    sent = _send_dm(_LINK_DM.format(url=url), run=run)
    popen([sys.executable, "-m", "core.lark_auth", "poll", code],
          cwd=str(JARVIS_DIR), stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, start_new_session=True)
    if sent:
        return ("已把授权链接发到你的飞书私聊（10 分钟内有效），"
                "点开确认即可；完成后我会自动回执。")
    # DM failed (no owner id / bot down) — hand the link back directly so the
    # tap still leads somewhere.
    return f"授权链接（10 分钟内有效）：{url}"


def poll(device_code: str, run=subprocess.run) -> int:
    """Block until the device code is authorized or expires; receipt on success.

    An expired/declined code stays silent: the link message already said it is
    short-lived, and a fresh tap mints a fresh link — nagging adds nothing.
    """
    try:
        r = run(["lark-cli", "auth", "login", "--device-code", device_code,
                 "--json"],
                capture_output=True, text=True, timeout=POLL_TIMEOUT_S,
                env=_cli_env())
    except subprocess.TimeoutExpired:
        return 1
    if r.returncode != 0 or not _user_token_ready(run=run):
        return 1
    _send_dm(_RECEIPT_DM, run=run)
    try:  # hasten channel recovery — best-effort
        TRIGGER_PATH.touch()
    except OSError:
        pass
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["start"]:
        print(start_device_flow())
        return 0
    if len(argv) >= 2 and argv[0] == "poll":
        return poll(argv[1])
    print("usage: python3 -m core.lark_auth start | poll <device_code>",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
