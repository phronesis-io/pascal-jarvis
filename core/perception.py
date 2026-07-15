"""Unified perception layer runtime — MVP per docs/prd_perception_ingestion.md.

Pipeline (deterministic, no LLM in the MVP path):
    registry (sources.yaml) → adapter.collect() → normalize → dedup
    → inbox buffer (memory/system/inbox_<domain>.md) → seen-store + audit

Design deviations from the PRD, with reasons:
- Per-source state lives in perception_state.json (own sidecar), NOT
  heartbeat_state.json['perception_sources']: this runtime executes inside a
  heartbeat pre-script while run_cycle holds the state file in memory — a
  child writing heartbeat_state.json gets clobbered by the cycle's final
  save_state (the exact bug that ate engagement-analyze's output for two
  months, fixed 2026-06-11).
- MVP ships 3 adapters (file_watch, git_repo, lark_chat). claude_sessions and
  cli_stream stay on their existing dedicated pipelines per the PRD's own
  incremental migration path (§10) — no parallel double-delivery.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import time
from pathlib import Path

from core.jsonl import append_jsonl, read_jsonl
from core.timeutil import now_local, now_local_str

SENSITIVITIES = {"public", "internal", "private", "secret"}
REQUIRED_FIELDS = ("event_id", "source_id", "source_type", "ts", "title",
                   "summary", "actor", "sensitivity")
SEEN_MAX_LINES = 10_000
INBOX_MAX_LINES = 500
INBOX_MAX_AGE_DAYS = 7

# REQ-92 (2026-07-14): buffers the loader char-caps are retained on disk to
# the SAME cap, split on whole `### ` entry boundaries. The old 500-line rule
# kept ~35k chars of which the loader only ever injected the tail 12k — 2/3
# of retained content was dark matter, and the line split left a mid-entry
# fragment at the top of the file. Single source of truth for the numbers is
# core.memory._SYSTEM_FILE_CAPS; buffers without a cap keep the line rule.
from core.memory import _SYSTEM_FILE_CAPS as _INBOX_CHAR_CAPS


# Entries younger than this are never trimmed even when the buffer is over
# its char cap. inbox_private_mail.md is a WORK QUEUE, not just a display
# buffer: mail-triage's collect_new() parses it for entries missing from
# triaged.jsonl at ≤15/cycle — an aggressive cap trim during a mail burst or
# a triage outage (user-token死 7/5, ~1 day) would delete mail that was never
# triaged. The loader still tail-caps the PROMPT view at the char cap, so
# protecting recent entries costs disk only, never prompt budget.
INBOX_PROTECT_RECENT_S = 48 * 3600


def _entry_epoch(header_line: str) -> float | None:
    """Epoch of a `### <id> | <src> | <who> | <ts> | ...` header, else None."""
    parts = [p.strip() for p in header_line[4:].split(" | ")]
    if len(parts) < 4:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(parts[3]).timestamp()
    except ValueError:
        return None


def split_entries_for_cap(text: str, cap: int,
                          protect_since: float | None = None,
                          expire_before: float | None = None) -> tuple[str, str]:
    """(keep, overflow) — newest whole `### ` entries totalling ≤ cap chars.

    Three rules, in precedence order (§5.4, re-cut 2026-07-14 REQ-92):
    1. expire_before: entries older than this are ALWAYS overflow, even under
       the cap — restores the PRD's "或 7 天(先到为准)" age bound that the old
       line-based trim documented but never implemented.
    2. protect_since: entries at/after this are kept even OVER the cap — the
       mail buffer is a work queue (mail-triage drains ≤15/cycle), so a burst
       or triage outage must not push untriaged mail into the archive.
    3. Otherwise: newest entries fitting within cap; the newest entry is kept
       even if it alone exceeds the cap.

    An unparseable header timestamp counts as OLD, so malformed entries can't
    pin the file open forever. Content before the first `### ` header (e.g. a
    legacy mid-entry fragment from the old line-based trim) counts as the
    oldest block, so one pass heals a corrupted head. Text without any `### `
    structure gets a raw tail-keep on a line boundary — a capped buffer whose
    entry format drifted must stay bounded, not grow forever.
    """
    if len(text) <= cap and expire_before is None:
        return text, ""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith("### ")]
    if not starts:
        if len(text) <= cap:
            return text, ""
        keep = text[-cap:]
        nl = keep.find("\n")
        if 0 <= nl < len(keep) - 1:
            keep = keep[nl + 1:]
        return keep, text[: len(text) - len(keep)]
    bounds = ([0] if starts[0] != 0 else []) + starts + [len(lines)]
    blocks = ["".join(lines[b:e]) for b, e in zip(bounds, bounds[1:])]

    def _epoch(block: str) -> float | None:
        return (_entry_epoch(block.split("\n", 1)[0])
                if block.startswith("### ") else None)

    n = len(blocks)
    expired = [False] * n
    if expire_before is not None:
        for i, b in enumerate(blocks):
            e = _epoch(b)
            expired[i] = e is None or e < expire_before
    kept = [False] * n
    size = 0
    any_kept = False
    for i in range(n - 1, -1, -1):
        if expired[i]:
            continue
        b = blocks[i]
        if any_kept and size + len(b) > cap:
            if protect_since is None:
                break
            e = _epoch(b)
            if e is None or e < protect_since:
                break
        kept[i] = True
        any_kept = True
        size += len(b)
    keep = "".join(b for i, b in enumerate(blocks) if kept[i])
    overflow = "".join(b for i, b in enumerate(blocks) if not kept[i])
    return keep, overflow
DEDUP_CLUSTER_WINDOW = 2 * 3600  # ±2h content_hash clustering (§8.0)
DEFAULT_INTERVAL = 900


def content_hash(title: str, body: str) -> str:
    """§5.6 single authoritative formula: sha256(title[:80] + body[:100])."""
    return hashlib.sha256((title[:80] + body[:100]).encode("utf-8")).hexdigest()


def parse_interval(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            return int(float(s[:-1]) * units[s[-1]])
        except ValueError:
            return DEFAULT_INTERVAL
    try:
        return int(s)
    except ValueError:
        return DEFAULT_INTERVAL


class PerceptionRuntime:
    def __init__(self, jarvis_dir: str | Path, memory_dir: str | Path):
        self.jarvis_dir = Path(jarvis_dir)
        self.memory_dir = Path(memory_dir)
        self.state_file = self.jarvis_dir / "perception_state.json"
        self.system_dir = self.memory_dir / "system"
        self.seen_file = self.system_dir / "perception_seen.jsonl"
        self.delivery_file = self.system_dir / "perception_delivery.jsonl"

    # ── registry ─────────────────────────────────────────────────────

    def load_sources(self) -> tuple[dict, list[dict]]:
        """Returns (defaults, sources) from sources.yaml. Missing file → empty."""
        path = self.jarvis_dir / "sources.yaml"
        if not path.exists():
            return {}, []
        try:
            import yaml
            data = yaml.safe_load(path.read_text()) or {}
        except Exception:
            return {}, []
        perception = data.get("perception", {}) or {}
        return perception.get("defaults", {}) or {}, perception.get("sources", []) or []

    # ── state ────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _save_state(self, state: dict):
        tmp = self.state_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        os.replace(tmp, self.state_file)

    # ── dedup ────────────────────────────────────────────────────────

    def _load_seen(self) -> tuple[set, dict]:
        """(primary keys {(source_id,event_id)}, {content_hash: latest_epoch})."""
        primary, clusters = set(), {}
        for e in read_jsonl(self.seen_file):
            primary.add((e.get("source_id"), e.get("event_id")))
            ch = e.get("content_hash")
            if ch:
                clusters[ch] = max(clusters.get(ch, 0), e.get("epoch", 0))
        return primary, clusters

    @staticmethod
    def _signal_epoch(sig: dict) -> float:
        from datetime import datetime
        try:
            return datetime.fromisoformat(sig.get("ts", "")).timestamp()
        except (ValueError, TypeError):
            return time.time()

    # ── inbox buffer (落地区①) ───────────────────────────────────────

    def _write_inbox(self, sig: dict, buffer_name: str):
        self.system_dir.mkdir(parents=True, exist_ok=True)
        path = self.system_dir / buffer_name
        actor = sig.get("actor") or {}
        who = actor.get("resolved") or actor.get("raw") or "?"
        header = (f"### {sig['event_id']} | {sig['source_id']} | {who} | "
                  f"{sig['ts']} | {sig['sensitivity']} | buffer")
        body = sig.get("body") or sig.get("summary") or ""
        url = sig.get("url") or ""
        lines = [header, sig.get("title", ""), body[:2048]]
        if url:
            lines.append(url)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n\n")

    def _trim_inbox(self, buffer_name: str):
        """Authoritative retention (§5.4, re-cut 2026-07-14 REQ-92): buffers
        with a loader char-cap keep the newest whole `### ` entries within
        that same cap (disk content ≈ injected content); others keep the old
        last-500-lines rule. Overflow archives to
        warm/archive/perception_archive_YYYYMM.md (never auto-injected — the
        loader skips warm/archive/). Writing to top-level warm/ violated that
        contract: the loader injects every warm/*.md newest-mtime-first, so
        the constantly-appended archive won the ordering and squatted the
        surviving warm budget on every call (2026-07-07 memory audit)."""
        path = self.system_dir / buffer_name
        if not path.exists():
            return
        # Sidecar flock + a size recheck before the swap (same contract as
        # memory_tidy's todos enforcement, 2026-07-08 red-team race): an
        # append landing between our read and os.replace would be silently
        # reverted — neither kept nor archived. Known writers today run in
        # this same thread (run_collect appends, then trims), so this guards
        # future/external writers.
        import fcntl
        lock_path = path.with_suffix(".lock")
        with open(lock_path, "w") as lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            except OSError:
                pass
            stat_before = path.stat()
            text = path.read_text(encoding="utf-8")
            cap = _INBOX_CHAR_CAPS.get(buffer_name)
            if cap is not None:
                now = time.time()
                keep_text, overflow_text = split_entries_for_cap(
                    text, cap,
                    protect_since=now - INBOX_PROTECT_RECENT_S,
                    expire_before=now - INBOX_MAX_AGE_DAYS * 86400)
                if not overflow_text:
                    return
            else:
                lines = text.splitlines()
                if len(lines) <= INBOX_MAX_LINES:
                    return
                overflow_text = "\n".join(lines[:-INBOX_MAX_LINES])
                keep_text = "\n".join(lines[-INBOX_MAX_LINES:]) + "\n"
            archive_dir = self.memory_dir / "warm" / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive = archive_dir / f"perception_archive_{now_local_str('%Y%m')}.md"
            with open(archive, "a", encoding="utf-8") as f:
                f.write(overflow_text.rstrip() + "\n")
                # Persist the archive BEFORE the buffer is replaced — a crash
                # in between duplicates a block, never loses one.
                f.flush()
                os.fsync(f.fileno())
            try:
                stat_now = path.stat()
                if (stat_now.st_size, stat_now.st_mtime_ns) != (
                        stat_before.st_size, stat_before.st_mtime_ns):
                    # A non-locking writer appended mid-trim: abort this pass
                    # (worst case = a duplicated archive block next pass,
                    # never a lost entry) and let the next run re-trim.
                    return
            except OSError:
                return
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(keep_text, encoding="utf-8")
            os.replace(tmp, path)

    # ── main entry ───────────────────────────────────────────────────

    def run_collect(self) -> str:
        """One collection pass over all due sources. Returns a brief summary."""
        defaults, sources = self.load_sources()
        if not sources:
            return "no sources configured"

        state = self._load_state()
        primary_seen, clusters = self._load_seen()
        now = time.time()
        collected = deduped = errors = 0
        touched_buffers = set()
        notes = []

        for src in sources:
            sid = src.get("id")
            stype = src.get("type")
            if not sid or not stype or not src.get("enabled", True):
                continue
            schedule = src.get("schedule") or {}
            interval = parse_interval(schedule.get("interval",
                                      defaults.get("interval", DEFAULT_INTERVAL)))
            src_state = state.get(sid, {})
            if now - src_state.get("last_run_ts", 0) < interval:
                continue

            try:
                adapter = importlib.import_module(f"sources.{stype}")
            except ImportError as e:
                notes.append(f"{sid}: adapter '{stype}' missing ({e})")
                errors += 1
                continue

            # Optional adapter hook (sources/README.md): a misconfigured
            # source is skipped and named in the notes instead of crashing
            # inside collect() or, worse, half-running with bad config.
            validate = getattr(adapter, "validate_cfg", None)
            if callable(validate):
                try:
                    cfg_errors = validate(src.get("collect") or {}) or []
                except Exception as e:  # a broken validator must not kill the pass
                    cfg_errors = [f"validate_cfg crashed: {e}"]
                if cfg_errors:
                    notes.append(f"{sid}: config invalid: "
                                 + "; ".join(str(x) for x in cfg_errors[:3]))
                    errors += 1
                    continue

            try:
                signals, new_adapter_state = adapter.collect(
                    src.get("collect") or {}, src_state.get("adapter_state", {}))
            except Exception as e:  # adapters must not raise; belt-and-braces
                notes.append(f"{sid}: collect crashed: {e}")
                src_state["error_type"] = "crash"
                src_state["error_count"] = src_state.get("error_count", 0) + 1
                src_state["last_run_ts"] = int(now)
                state[sid] = src_state
                errors += 1
                continue

            err = (new_adapter_state or {}).pop("error_type", None)
            if err:
                src_state["error_type"] = err
                src_state["error_count"] = src_state.get("error_count", 0) + 1
                errors += 1
                notes.append(f"{sid}: {err}")
            else:
                src_state["error_type"] = None
                src_state["error_count"] = 0

            buffer_name = ((src.get("perceive") or {}).get("buffer")
                           or "inbox_ops.md").split("/")[-1]
            sensitivity_default = src.get("sensitivity",
                                          defaults.get("sensitivity", "internal"))

            for sig in signals or []:
                sig.setdefault("source_id", sid)
                sig.setdefault("source_type", stype)
                sig.setdefault("sensitivity", sensitivity_default)
                sig.setdefault("actor", {"raw": "", "resolved": ""})
                sig.setdefault("summary", sig.get("title", ""))
                if sig.get("sensitivity") not in SENSITIVITIES:
                    sig["sensitivity"] = "internal"
                if any(not sig.get(k) and k not in ("summary",) for k in REQUIRED_FIELDS):
                    continue  # normalize: drop malformed
                sig.setdefault("content_hash",
                               content_hash(sig.get("title", ""), sig.get("body", "")))

                key = (sig["source_id"], sig["event_id"])
                epoch = self._signal_epoch(sig)
                ch = sig["content_hash"]
                if key in primary_seen or \
                        (ch in clusters and abs(epoch - clusters[ch]) < DEDUP_CLUSTER_WINDOW):
                    deduped += 1
                    append_jsonl(self.seen_file,
                                 {"event_id": sig["event_id"], "source_id": sig["source_id"],
                                  "content_hash": ch, "action": "dedup",
                                  "epoch": int(epoch), "ts": now_local_str()})
                    continue

                self._write_inbox(sig, buffer_name)
                touched_buffers.add(buffer_name)
                primary_seen.add(key)
                clusters[ch] = epoch
                collected += 1
                append_jsonl(self.seen_file,
                             {"event_id": sig["event_id"], "source_id": sig["source_id"],
                              "content_hash": ch, "action": "deliver",
                              "epoch": int(epoch), "ts": now_local_str()})
                append_jsonl(self.delivery_file,
                             {"ts": now_local_str(), "source_id": sig["source_id"],
                              "source_type": sig["source_type"],
                              "event_id": sig["event_id"], "action": "buffer",
                              "sensitivity": sig["sensitivity"]},
                             keep_last=5000)

            src_state["adapter_state"] = new_adapter_state or {}
            src_state["last_run_ts"] = int(now)
            state[sid] = src_state

        for buf in touched_buffers:
            self._trim_inbox(buf)
        # seen-store cap (§5.4): 10K lines
        seen_lines = read_jsonl(self.seen_file)
        if len(seen_lines) > SEEN_MAX_LINES:
            from core.jsonl import write_jsonl
            write_jsonl(self.seen_file, seen_lines[-SEEN_MAX_LINES:])

        self._save_state(state)
        parts = [f"collected={collected}", f"deduped={deduped}", f"errors={errors}"]
        if notes:
            parts.append("notes: " + "; ".join(notes[:5]))
        return " ".join(parts)


if __name__ == "__main__":
    jarvis_dir = os.environ.get("JARVIS_DIR", ".")
    memory_dir = os.environ.get("MEMORY_DIR", str(Path(jarvis_dir) / "memory"))
    import sys
    sys.path.insert(0, jarvis_dir)
    print(PerceptionRuntime(jarvis_dir, memory_dir).run_collect())
