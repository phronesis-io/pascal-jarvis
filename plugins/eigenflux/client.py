"""EigenFlux API client — connect to the EigenFlux broadcast network."""

import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

API_BASE = "https://www.eigenflux.ai/api/v1"
SKILL_VERSION = "0.0.7"


def _headers(token: str | None = None) -> dict:
    h = {"Content-Type": "application/json", "X-Skill-Ver": SKILL_VERSION}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _request(method: str, path: str, data: dict | None = None,
             params: dict | None = None, token: str | None = None) -> dict:
    url = f"{API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    body = json.dumps(data).encode() if data else None
    req = Request(url, data=body, headers=_headers(token), method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        error_body = e.read().decode()
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            return {"code": e.code, "msg": error_body}


class EigenFluxClient:
    """Stateful client that manages credentials and local feed storage."""

    def __init__(self, workdir: str | Path):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.creds_file = self.workdir / "credentials.json"
        self.settings_file = self.workdir / "user_settings.json"
        self.seen_file = self.workdir / "seen_items.json"
        self.feed_store = self.workdir / "feed_store.jsonl"
        self.msg_store = self.workdir / "message_store.jsonl"
        self.publish_state = self.workdir / "publish_state.json"

    # ── Token ──────────────────────────────────────────────────────

    def _token(self) -> str | None:
        if self.creds_file.exists():
            return json.loads(self.creds_file.read_text()).get("access_token")
        return None

    def _save_token(self, data: dict):
        """Atomic write via temp + rename (creds_file must survive crashes)."""
        tmp = self.creds_file.with_suffix(self.creds_file.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.creds_file)

    # ── Settings ───────────────────────────────────────────────────

    def load_settings(self) -> dict:
        if self.settings_file.exists():
            return json.loads(self.settings_file.read_text())
        return {}

    def save_settings(self, settings: dict):
        self.settings_file.write_text(json.dumps(settings, indent=2))

    # ── Auth ───────────────────────────────────────────────────────

    def login(self, email: str) -> dict:
        resp = _request("POST", "/auth/login",
                        {"email": email, "login_method": "email"})
        if resp.get("code") == 0:
            d = resp.get("data", {})
            if d.get("access_token"):
                self._save_token(d)
            return d
        return resp

    def verify(self, challenge_id: str, code: str) -> dict:
        resp = _request("POST", "/auth/login/verify", {
            "challenge_id": challenge_id, "code": code, "login_method": "email"
        })
        if resp.get("code") == 0:
            self._save_token(resp.get("data", {}))
        return resp

    # ── Profile ────────────────────────────────────────────────────

    def get_me(self) -> dict:
        return _request("GET", "/agents/me", token=self._token())

    def update_profile(self, agent_name: str | None = None,
                       bio: str | None = None) -> dict:
        data = {}
        if agent_name:
            data["agent_name"] = agent_name
        if bio:
            data["bio"] = bio
        return _request("PUT", "/agents/profile", data, token=self._token())

    # ── Feed ───────────────────────────────────────────────────────

    def pull_feed(self, limit: int = 20, action: str = "refresh") -> dict:
        resp = _request("GET", "/items/feed",
                        params={"limit": limit, "action": action},
                        token=self._token())
        data = resp.get("data", {})
        # Persist the impression_id — required for feedback submission.
        impression_id = data.get("impression_id")
        if impression_id:
            self._save_impression_id(impression_id)
        # Persist items locally
        items = data.get("items", [])
        if items:
            self._persist_items(items)
        return resp

    def _persist_items(self, items: list[dict]):
        """Append items to local JSONL feed store (deduped by item_id).

        Write is made durable per-item (flush + fsync) and the seen-set is
        persisted both before (for duplicates) and after (for durability)
        writing, so a crash mid-loop leaves the two files consistent.
        """
        seen = self._load_seen()
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        newly_written = []
        with open(self.feed_store, "a", encoding="utf-8") as f:
            for item in items:
                iid = str(item.get("item_id", ""))
                if not iid or iid in seen:
                    continue
                record = {"fetched_at": ts, **item}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass  # some filesystems (tmpfs, etc.) don't support fsync
                seen.add(iid)
                newly_written.append(iid)
                # Persist seen-set periodically so a crash doesn't orphan writes
                if len(newly_written) % 5 == 0:
                    self._save_seen(seen)
        if newly_written:
            self._save_seen(seen)

    def _load_seen(self) -> set:
        if self.seen_file.exists():
            return set(json.loads(self.seen_file.read_text()))
        return set()

    def _save_seen(self, seen: set):
        """Atomic write via temp + rename to avoid partial file on crash."""
        tmp = self.seen_file.with_suffix(self.seen_file.suffix + ".tmp")
        tmp.write_text(json.dumps(sorted(seen)))
        os.replace(tmp, self.seen_file)

    def _save_impression_id(self, impression_id: str):
        """Save the latest feed impression_id (needed for feedback submission)."""
        state_file = self.workdir / "impression_id.txt"
        state_file.write_text(impression_id)

    def _load_impression_id(self) -> str:
        """Load the most recent impression_id from the last pull_feed call."""
        state_file = self.workdir / "impression_id.txt"
        if state_file.exists():
            return state_file.read_text().strip()
        return ""

    def get_item(self, item_id: int) -> dict:
        return _request("GET", f"/items/{item_id}", token=self._token())

    def submit_feedback(self, items: list[dict],
                        impression_id: str | None = None) -> dict:
        """Submit scoring feedback for consumed feed items.

        Per spec, `impression_id` from the feed response must be included.
        If not passed explicitly, we try the last-saved one from pull_feed.
        """
        if impression_id is None:
            impression_id = self._load_impression_id()
        body: dict = {"items": items}
        if impression_id:
            body["impression_id"] = impression_id
        return _request("POST", "/items/feedback", body, token=self._token())

    # ── Feed History (local) ───────────────────────────────────────

    def search_feed_history(self, query: str, limit: int = 20) -> list[dict]:
        """Search locally persisted feed items by keyword."""
        if not self.feed_store.exists():
            return []
        results = []
        query_lower = query.lower()
        for line in self.feed_store.open(encoding="utf-8", errors="ignore"):
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            searchable = json.dumps(item, ensure_ascii=False).lower()
            if query_lower in searchable:
                results.append(item)
                if len(results) >= limit:
                    break
        return results

    def feed_history_stats(self) -> dict:
        """Get stats about local feed store."""
        if not self.feed_store.exists():
            return {"total_items": 0}
        count = 0
        first_ts = None
        last_ts = None
        for line in self.feed_store.open(encoding="utf-8", errors="ignore"):
            try:
                item = json.loads(line)
                ts = item.get("fetched_at", "")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                count += 1
            except json.JSONDecodeError:
                continue
        return {
            "total_items": count,
            "first_fetched": first_ts,
            "last_fetched": last_ts,
        }

    # ── Publish ────────────────────────────────────────────────────

    def publish(self, content: str, notes: dict,
                url: str | None = None, accept_reply: bool = True) -> dict:
        data = {
            "content": content,
            "notes": json.dumps(notes),
            "accept_reply": accept_reply,
        }
        if url:
            data["url"] = url
        resp = _request("POST", "/items/publish", data, token=self._token())
        if resp.get("code") == 0:
            self._record_publish()
        return resp

    def delete_item(self, item_id: int) -> dict:
        return _request("DELETE", f"/agents/items/{item_id}", token=self._token())

    def last_publish_time(self) -> int:
        if self.publish_state.exists():
            return json.loads(self.publish_state.read_text()).get("last_publish_epoch", 0)
        return 0

    def _record_publish(self):
        """Atomic write via temp + rename."""
        tmp = self.publish_state.with_suffix(self.publish_state.suffix + ".tmp")
        tmp.write_text(json.dumps({"last_publish_epoch": int(time.time())}))
        os.replace(tmp, self.publish_state)

    # ── Messages ───────────────────────────────────────────────────

    def fetch_messages(self, limit: int = 20) -> dict:
        resp = _request("GET", "/pm/fetch",
                        params={"limit": limit}, token=self._token())
        # Persist messages locally
        messages = resp.get("data", {}).get("messages", [])
        if messages:
            self._persist_messages(messages)
        return resp

    def _persist_messages(self, messages: list[dict]):
        """Append messages to local JSONL store with per-line durability."""
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.msg_store, "a", encoding="utf-8") as f:
            for msg in messages:
                record = {"fetched_at": ts, **msg}
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass

    def send_message(self, content: str, item_id: int | None = None,
                     conv_id: str | None = None,
                     receiver_id: str | None = None) -> dict:
        data = {"content": content}
        if item_id:
            data["item_id"] = item_id
        if conv_id:
            data["conv_id"] = conv_id
        if receiver_id:
            data["receiver_id"] = receiver_id
        return _request("POST", "/pm/send", data, token=self._token())

    def list_conversations(self, limit: int = 20) -> dict:
        return _request("GET", "/pm/conversations",
                        params={"limit": limit}, token=self._token())

    def message_history(self, conv_id: str) -> dict:
        return _request("GET", "/pm/history",
                        params={"conv_id": conv_id}, token=self._token())

    # ── Relations ──────────────────────────────────────────────────

    def send_friend_request(self, agent_id: str | None = None,
                            email: str | None = None,
                            greeting: str | None = None) -> dict:
        data = {}
        if agent_id:
            data["to_uid"] = agent_id
        if email:
            cleaned = email.removeprefix("eigenflux#").strip()
            if "@" not in cleaned:
                return {"code": -1, "msg": f"invalid email: {email!r}"}
            data["to_email"] = cleaned
        if greeting:
            data["greeting"] = greeting[:200]
        if not data.get("to_uid") and not data.get("to_email"):
            return {"code": -1, "msg": "either agent_id or email is required"}
        return _request("POST", "/relations/apply", data, token=self._token())

    def list_friends(self) -> dict:
        return _request("GET", "/relations/friends", token=self._token())

    def list_applications(self) -> dict:
        return _request("GET", "/relations/applications", token=self._token())

    def handle_friend_request(self, application_id: str, action: str) -> dict:
        return _request("POST", "/relations/handle",
                        {"application_id": application_id, "action": action},
                        token=self._token())
