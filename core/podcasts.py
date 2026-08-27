#!/usr/bin/env python3
"""Podcast digest — deterministic discovery + transcript prep.

Pascal 2026-08-27: 「像这样的播客可以每天给我想办法搞一点，我挺成总结总结，
我简单看看，你可以不要让我错过了。」

This module does ONLY the deterministic half: find episodes we have not seen,
pull the official captions, and hand the heartbeat task a local transcript file
plus real metadata. The LLM never guesses what an episode is about — it reads
the actual captions. That is the whole point (REQ-78: no unverified external
facts).

Why a heartbeat task and not a routine: routines run with `no-tools: true` and
take their evidence inline, but a 5-hour transcript is ~300k chars — it cannot
be inlined next to the memory payload. So the pre-script prepares a FILE and
the task reads it in chunks.

CLI:
    python3 -m core.podcasts list                 # show the watchlist
    python3 -m core.podcasts scan [--limit N]     # unseen candidates as JSON
    python3 -m core.podcasts prepare <video_id>   # captions -> local text file
    python3 -m core.podcasts mark <video_id> [--doc URL]   # record as delivered
    python3 -m core.podcasts add --name X --url Y # add a show
    python3 -m core.podcasts remove --name X
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.safety import atomic_write  # noqa: E402

JARVIS_DIR = Path(os.environ.get("JARVIS_DIR", Path(__file__).resolve().parent.parent))
DATA_DIR = JARVIS_DIR / "data"
WATCHLIST_PATH = DATA_DIR / "podcast_watchlist.json"
STATE_PATH = DATA_DIR / "podcast_state.json"
TRANSCRIPT_DIR = DATA_DIR / "podcasts"

# Keep the seen-set bounded; a year of daily episodes is ~365 entries.
MAX_SEEN = 500
# Anything shorter is a clip/short, not an episode worth a digest.
DEFAULT_MIN_MINUTES = 25
# How far back a first run is allowed to reach, so day one does not dump a
# year of back catalogue on him.
FIRST_RUN_LOOKBACK_DAYS = 4

DEFAULT_WATCHLIST = [
    {"name": "Lex Fridman", "url": "https://www.youtube.com/@lexfridman/videos",
     "min_minutes": 40},
    {"name": "Dwarkesh Patel", "url": "https://www.youtube.com/@DwarkeshPatel/videos",
     "min_minutes": 25},
    {"name": "Latent Space", "url": "https://www.youtube.com/@LatentSpacePod/videos",
     "min_minutes": 25},
    {"name": "No Priors", "url": "https://www.youtube.com/@NoPriorsPodcast/videos",
     "min_minutes": 20},
    {"name": "Y Combinator", "url": "https://www.youtube.com/@ycombinator/videos",
     "min_minutes": 20},
    # tech x investing, the Gavin Baker end of the table — his stated interest,
    # and the one show here that argues about capex and market structure rather
    # than model releases.
    {"name": "BG2", "url": "https://www.youtube.com/@BG2Pod/videos",
     "min_minutes": 30},
]


# --------------------------------------------------------------------------- io


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_watchlist() -> list[dict]:
    shows = _read_json(WATCHLIST_PATH, None)
    if not isinstance(shows, list) or not shows:
        return list(DEFAULT_WATCHLIST)
    out = []
    for s in shows:
        if isinstance(s, dict) and s.get("url") and not s.get("paused"):
            out.append(s)
    return out


def load_state() -> dict:
    st = _read_json(STATE_PATH, None)
    if not isinstance(st, dict):
        st = {}
    st.setdefault("seen", [])
    st.setdefault("delivered", {})
    return st


def save_state(state: dict) -> None:
    state["seen"] = list(dict.fromkeys(state.get("seen", [])))[-MAX_SEEN:]
    _write_json(STATE_PATH, state)


# ---------------------------------------------------------------------- yt-dlp


def _yt_dlp() -> str | None:
    return shutil.which("yt-dlp")


def _run(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, "", str(exc)


def _parse_upload_date(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw.strip(), "%Y%m%d")
    except (ValueError, AttributeError):
        return None


def _duration_minutes(raw: str) -> int:
    """`5:15:51` / `48:12` / `612` -> whole minutes. 0 when unknown."""
    raw = (raw or "").strip()
    if not raw or raw == "NA":
        return 0
    parts = raw.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    secs = 0
    for n in nums:
        secs = secs * 60 + n
    if len(nums) == 1:          # bare seconds
        return secs // 60
    return secs // 60


def scan(limit_per_show: int = 4, lookback_days: int | None = None) -> list[dict]:
    """Unseen, long-enough episodes across the watchlist, newest first."""
    if not _yt_dlp():
        return []
    state = load_state()
    seen = set(state.get("seen", []))
    first_run = not seen
    if lookback_days is None:
        lookback_days = FIRST_RUN_LOOKBACK_DAYS if first_run else 14
    cutoff = datetime.now() - timedelta(days=lookback_days)

    found: list[dict] = []
    for show in load_watchlist():
        min_minutes = int(show.get("min_minutes") or DEFAULT_MIN_MINUTES)
        code, out, _ = _run([
            _yt_dlp(), "--flat-playlist", "--skip-download",
            "--playlist-end", str(max(1, limit_per_show)),
            "--print", "%(id)s\t%(title)s\t%(duration_string)s\t%(url)s",
            show["url"],
        ], timeout=120)
        if code != 0:
            continue
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) < 4:
                continue
            vid, title, dur, url = cols[0].strip(), cols[1].strip(), cols[2], cols[3].strip()
            if not vid or vid in seen:
                continue
            minutes = _duration_minutes(dur)
            # A flat-playlist listing sometimes omits duration; keep those and
            # let `prepare` settle it rather than silently dropping an episode.
            if minutes and minutes < min_minutes:
                continue
            found.append({
                "id": vid, "show": show.get("name") or show["url"],
                "title": title, "minutes": minutes, "url": url,
            })

    # Upload dates need a per-video probe; only pay for it on the candidates.
    dated: list[dict] = []
    for item in found:
        code, out, _ = _run([
            _yt_dlp(), "--skip-download", "--no-warnings",
            "--print", "%(upload_date)s\t%(duration_string)s", item["url"],
        ], timeout=90)
        if code != 0:
            continue
        cols = out.strip().split("\t")
        when = _parse_upload_date(cols[0] if cols else "")
        if when is None or when < cutoff:
            continue
        if len(cols) > 1 and not item["minutes"]:
            item["minutes"] = _duration_minutes(cols[1])
        item["published"] = when.strftime("%Y-%m-%d")
        dated.append(item)

    dated.sort(key=lambda i: (i.get("published", ""), i.get("minutes", 0)), reverse=True)
    return dated


# ------------------------------------------------------------------ transcript


_TS_RE = re.compile(r"^(\d\d:\d\d:\d\d)\.\d\d\d --> ")
_TAG_RE = re.compile(r"<[^>]+>")


def vtt_to_text(vtt: str, cues_per_para: int = 25) -> str:
    """Flatten a VTT file into timestamped paragraphs, de-duplicating the
    rolling-caption repeats YouTube emits."""
    out: list[tuple[str, str]] = []
    last = ""
    ts = "00:00:00"
    for line in vtt.split("\n"):
        m = _TS_RE.match(line)
        if m:
            ts = m.group(1)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith(("WEBVTT", "Kind:", "Language:")):
            continue
        text = _TAG_RE.sub("", stripped).strip()
        if not text or text == last:
            continue
        last = text
        out.append((ts, text))

    paras: list[str] = []
    buf: list[str] = []
    start = out[0][0] if out else "00:00:00"
    for cue_ts, text in out:
        buf.append(text)
        if len(buf) >= cues_per_para:
            paras.append(f"[{start}] " + " ".join(buf))
            buf, start = [], cue_ts
    if buf:
        paras.append(f"[{start}] " + " ".join(buf))
    return "\n\n".join(paras)


def prepare(video_id: str) -> dict:
    """Download captions + metadata for one episode. Returns a dict with a
    `transcript_path`, or an `error` key — never a guess."""
    if not _yt_dlp():
        return {"error": "yt-dlp not installed"}
    url = f"https://www.youtube.com/watch?v={video_id}"
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    code, out, err = _run([
        _yt_dlp(), "--skip-download", "--no-warnings", "--dump-json", url,
    ], timeout=180)
    if code != 0:
        return {"error": f"metadata fetch failed: {err.strip()[:200]}"}
    try:
        meta = json.loads(out)
    except ValueError:
        return {"error": "metadata was not JSON"}

    chapters = [
        {"start": int(c.get("start_time") or 0), "title": c.get("title") or ""}
        for c in (meta.get("chapters") or [])
    ]

    stem = TRANSCRIPT_DIR / video_id
    code, _, err = _run([
        _yt_dlp(), "--skip-download", "--no-warnings",
        "--write-auto-subs", "--write-subs", "--sub-langs", "en.*",
        "--sub-format", "vtt", "-o", str(stem), url,
    ], timeout=300)

    vtt_file = None
    for cand in sorted(TRANSCRIPT_DIR.glob(f"{video_id}*.vtt")):
        # `.en.vtt` is the human/primary track; `.en-orig.vtt` is the raw ASR
        # dump and is ~6x larger for the same words.
        if cand.name.endswith(".en.vtt"):
            vtt_file = cand
            break
        vtt_file = vtt_file or cand
    if vtt_file is None:
        return {"error": f"no captions available for {video_id}: {err.strip()[:200]}"}

    text = vtt_to_text(vtt_file.read_text(encoding="utf-8", errors="replace"))
    txt_path = TRANSCRIPT_DIR / f"{video_id}.txt"
    atomic_write(txt_path, text)
    for leftover in TRANSCRIPT_DIR.glob(f"{video_id}*.vtt"):
        leftover.unlink(missing_ok=True)

    return {
        "id": video_id,
        "url": url,
        "title": meta.get("title") or "",
        "channel": meta.get("channel") or meta.get("uploader") or "",
        "published": meta.get("upload_date") or "",
        "duration": meta.get("duration_string") or "",
        "chapters": chapters,
        "transcript_path": str(txt_path),
        "transcript_chars": len(text),
    }


def mark(video_id: str, doc_url: str = "") -> None:
    state = load_state()
    state.setdefault("seen", []).append(video_id)
    if doc_url:
        state.setdefault("delivered", {})[video_id] = {
            "doc": doc_url, "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    save_state(state)


# ------------------------------------------------------------------ selection


MAX_PICK_TRIES = 3


def pick() -> str:
    """One episode, captions already on disk, rendered as a heartbeat evidence
    block. Empty string means "nothing worth a card today" — the pre-script
    turns that into silence, never into a 「今天没有播客」 card.

    Episodes whose captions cannot be fetched are marked seen and skipped: a
    caption-less video is not something we can honestly summarise, and leaving
    it in the queue would block the show behind it every single day.
    """
    candidates = scan()
    if not candidates:
        return ""

    chosen = None
    for item in candidates[:MAX_PICK_TRIES]:
        info = prepare(item["id"])
        if info.get("error"):
            mark(item["id"])
            continue
        info["show"] = item.get("show", "")
        chosen = info
        break
    if chosen is None:
        return ""

    lines = [
        "=== PODCAST DIGEST — 今天这一期 ===",
        f"video_id: {chosen['id']}",
        f"标题: {chosen['title']}",
        f"节目: {chosen.get('show') or chosen.get('channel')}　频道: {chosen.get('channel')}",
        f"发布: {chosen.get('published')}　时长: {chosen.get('duration')}",
        f"链接: {chosen['url']}",
        f"字幕全文（已下载，{chosen['transcript_chars']} 字符）: {chosen['transcript_path']}",
        "",
        "章节（秒数可直接拼成 &t=<秒>s 的跳转链接）:",
    ]
    if chosen.get("chapters"):
        for c in chosen["chapters"]:
            lines.append(f"  {_fmt_chapter(c['start'])} ({c['start']}s)  {c['title']}")
    else:
        lines.append("  （这期没有章节标记，按内容自己分段）")

    others = [c for c in candidates if c["id"] != chosen["id"]][:4]
    if others:
        lines.append("")
        lines.append("今天没选的（只作背景，别在卡里替它们描述内容——没读过）:")
        for o in others:
            lines.append(f"  [{o['published']}] {o['show']} · {o['minutes']}min · {o['title'][:70]}")
    return "\n".join(lines)


# ----------------------------------------------------------------- broadcast

# Pascal 2026-08-27:「你可以做一个 supply，把高质量的去走发布，我们争取成为这个
# 网站里面的一个比较高质量的贡献者。」So: not every episode goes out. What leaves
# this machine is a stranger-facing artefact built from a PUBLIC transcript —
# never his name, his tools, or a link into his private drive.
_PRIVATE_RE = re.compile(
    r"(?i)\b(pascal|yongyi|jarvis|phronesis)\b|飞书|白皮书|pcnlty|feishu\.cn"
)
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
MIN_BROADCAST_CHARS = 400
MIN_BROADCAST_TIMESTAMPS = 2
MAX_SUMMARY_CHARS = 100
BROADCAST_TTL_DAYS = 30
DEFAULT_BROADCAST_DOMAINS = ["tech", "ai-agents", "research"]


def broadcast_reject_reason(video_id: str, content: str, summary: str,
                            state: dict | None = None) -> str:
    """Why this digest must NOT go on the network. Empty string == publishable.

    Fail closed: every bar here is a bar the network would otherwise have to
    trust us on. A thin post costs other agents' attention, and attention is
    the only currency the network has.
    """
    state = state if state is not None else load_state()
    if not video_id:
        return "no video_id"
    if video_id in (state.get("broadcast") or {}):
        return "already broadcast"
    content = (content or "").strip()
    summary = (summary or "").strip()
    if len(content) < MIN_BROADCAST_CHARS:
        return f"content too thin ({len(content)} < {MIN_BROADCAST_CHARS})"
    if len(_TIMESTAMP_RE.findall(content)) < MIN_BROADCAST_TIMESTAMPS:
        return "fewer than two timestamped claims — not grounded enough"
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        return "summary missing or over 100 chars"
    leak = _PRIVATE_RE.search(content) or _PRIVATE_RE.search(summary)
    if leak:
        return f"private marker in public text: {leak.group(0)!r}"
    return ""


def _eigenflux() -> str | None:
    return shutil.which("eigenflux")


def _item_id(stdout: str) -> str:
    m = re.search(r'"item_id"\s*:\s*"?(\d+)"?', stdout or "")
    return m.group(1) if m else ""


def broadcast(video_id: str, title: str, content: str, summary: str,
              url: str = "", keywords: list[str] | None = None,
              domains: list[str] | None = None) -> dict:
    """Publish one digest as a `supply` broadcast. Never raises: a failed
    broadcast must not cost him the card the digest was actually for."""
    state = load_state()
    reason = broadcast_reject_reason(video_id, content, summary, state)
    if reason:
        return {"skipped": reason}
    ef = _eigenflux()
    if not ef:
        return {"skipped": "eigenflux CLI not installed"}

    notes = {
        "type": "supply",
        "domains": (domains or DEFAULT_BROADCAST_DOMAINS)[:3],
        "summary": summary.strip(),
        "expire_time": (datetime.now() + timedelta(days=BROADCAST_TTL_DAYS))
                       .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_type": "curated",
        "expected_response": (
            "What: the podcast episode URL you want digested, plus the one "
            "question you want answered from it. Constraints: an official "
            "transcript must exist; one episode per request. Deadline: 24h."
        ),
        "keywords": (keywords or ["podcast-digest", "transcript",
                                  "ai-agents", "timestamps"])[:8],
    }
    cmd = [ef, "publish", "--content", content,
           "--notes", json.dumps(notes, ensure_ascii=False), "--accept-reply"]
    if url:
        cmd += ["--url", url]
    code, out, err = _run(cmd, timeout=180)
    if code != 0:
        return {"error": (err or out).strip()[:200]}
    item_id = _item_id(out)
    if not item_id:
        return {"error": f"publish returned no item_id: {out.strip()[:200]}"}

    state.setdefault("broadcast", {})[video_id] = {
        "item_id": item_id, "title": title[:120],
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    save_state(state)
    return {"item_id": item_id}


# ------------------------------------------------------------------------- cli


def _fmt_chapter(sec: int) -> str:
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="core.podcasts")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("pick")
    p_scan = sub.add_parser("scan")
    p_scan.add_argument("--limit", type=int, default=4)
    p_scan.add_argument("--lookback-days", type=int, default=None)
    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("video_id")
    p_bc = sub.add_parser("broadcast")
    p_bc.add_argument("video_id")
    p_bc.add_argument("--content-file", required=True)
    p_bc.add_argument("--summary", required=True)
    p_bc.add_argument("--title", default="")
    p_bc.add_argument("--url", default="")
    p_mark = sub.add_parser("mark")
    p_mark.add_argument("video_id")
    p_mark.add_argument("--doc", default="")
    p_add = sub.add_parser("add")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--min-minutes", type=int, default=DEFAULT_MIN_MINUTES)
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--name", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "list":
        for s in load_watchlist():
            print(f"{s.get('name','?'):<18} {s.get('url')}  (≥{s.get('min_minutes', DEFAULT_MIN_MINUTES)}min)")
        return 0

    if args.cmd == "pick":
        block = pick()
        if block:
            print(block)
        return 0

    if args.cmd == "scan":
        items = scan(limit_per_show=args.limit, lookback_days=args.lookback_days)
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "prepare":
        info = prepare(args.video_id)
        if info.get("chapters"):
            info["chapters_readable"] = [
                f"{_fmt_chapter(c['start'])}  {c['title']}" for c in info["chapters"]
            ]
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 1 if info.get("error") else 0

    if args.cmd == "broadcast":
        body = Path(args.content_file).read_text(encoding="utf-8")
        result = broadcast(args.video_id, args.title, body, args.summary,
                           url=args.url or f"https://www.youtube.com/watch?v={args.video_id}")
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get("error") else 0

    if args.cmd == "mark":
        mark(args.video_id, args.doc)
        print(f"marked {args.video_id}")
        return 0

    if args.cmd == "add":
        shows = _read_json(WATCHLIST_PATH, None)
        if not isinstance(shows, list):
            shows = list(DEFAULT_WATCHLIST)
        shows = [s for s in shows if s.get("name") != args.name]
        shows.append({"name": args.name, "url": args.url,
                      "min_minutes": args.min_minutes})
        _write_json(WATCHLIST_PATH, shows)
        print(f"added {args.name}")
        return 0

    if args.cmd == "remove":
        shows = _read_json(WATCHLIST_PATH, None)
        if not isinstance(shows, list):
            shows = list(DEFAULT_WATCHLIST)
        shows = [s for s in shows if s.get("name") != args.name]
        _write_json(WATCHLIST_PATH, shows)
        print(f"removed {args.name}")
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
