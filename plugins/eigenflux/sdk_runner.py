"""Run isolated SDK acceptance without starting the resident Jarvis stack."""

import argparse
import json
import os
from pathlib import Path
import signal
import stat
import threading
import time


def private_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--context-file", type=Path, required=True)
    parser.add_argument("--base-url", default="https://www.eigenflux.ai")
    parser.add_argument("--agent-id", required=True, help="stable external identifier")
    parser.add_argument("--runtime-version", default="1.15.0")
    parser.add_argument("--poll-seconds", type=int, default=3600)
    parser.add_argument("--seconds", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    repo = Path(__file__).resolve().parents[2]
    if root == repo or root == Path.home() / ".jarvis":
        parser.error("root must be a dedicated acceptance directory")
    credential = args.credential_file.resolve(strict=True)
    if not stat.S_ISREG(credential.stat().st_mode) or credential.stat().st_mode & 0o077:
        parser.error("credential file must be private (0600)")
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ["JARVIS_DIR"] = str(root)
    os.environ["JARVIS_DB_PATH"] = str(root / "data" / "jarvis.db")
    from eigenflux_sdk import Client
    from plugins.eigenflux.sdk_adapter import JarvisInbox, read_context

    inbox = JarvisInbox(root)
    stopped = threading.Event()
    delivered = threading.Event()

    def on_status(state, reason):
        value = {"state": state, "reason": reason, "at": int(time.time() * 1000)}
        private_json(root / "status.json", value)
        with (root / "status.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")
        if state == "delivered":
            delivered.set()

    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        with Client(base_url=args.base_url, app_credential=credential.read_text().strip(),
                    external_agent_id=args.agent_id, runtime_name="jarvis",
                    runtime_version=args.runtime_version, state_dir=root / "sdk",
                    feed_poll_interval_seconds=args.poll_seconds, auto_update=False,
                    context_provider=lambda: read_context(args.context_file, root),
                    push_to_agent=inbox.push_to_agent, delivery_status=inbox.delivery_status,
                    on_status=on_status) as client:
            client.wait_until_registered(timeout=min(args.seconds, 60))
            deadline = time.monotonic() + args.seconds
            while not stopped.wait(0.2):
                if args.once and delivered.is_set():
                    break
                if time.monotonic() >= deadline:
                    if args.once:
                        raise TimeoutError("no nonempty Feed consumed before deadline")
                    break
    finally:
        private_json(root / "snapshot.json", inbox.snapshot())
    print(json.dumps(inbox.snapshot()))


if __name__ == "__main__":
    main()
