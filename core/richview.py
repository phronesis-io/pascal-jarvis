"""RichView — Jarvis's universal rich-output channel.

Any module can call publish() to render structured content as a web page
and get back a URL suitable for embedding in a Lark card button.

This is NOT a feature — it's an output modality. The model uses it whenever
flat text is insufficient: timelines, charts, tables, diff views, dashboards.

Usage:
    from core.richview import publish

    url = publish(
        title="日报 2026-05-17",
        sections=[
            {"type": "markdown", "content": "## 摘要\\n今天做了..."},
            {"type": "table", "headers": ["指标","值"], "rows": [["完成率","85%"]]},
            {"type": "kv", "items": {"状态": "✅ 正常", "下次检查": "18:00"}},
            {"type": "code", "language": "python", "content": "print('hello')"},
            {"type": "timeline", "events": [{"time": "09:00", "text": "起床"}]},
        ],
        meta={"source": "daily_post", "date": "2026-05-17"},
    )
    # url = "http://127.0.0.1:3456/view/a1b2c3d4"
"""

import json
import os
import time
import uuid
from pathlib import Path

from core.config import Config

CODE_ROOT = Path(__file__).resolve().parent.parent
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR") or CODE_ROOT)
_INITIAL_JARVIS_DIR = JARVIS_DIR

VIEWS_DIR = JARVIS_DIR / "views"
_INITIAL_VIEWS_DIR = VIEWS_DIR

# Max views to keep (FIFO cleanup)
MAX_VIEWS = 200


def runtime_root() -> Path:
    """Resolve state at call time while preserving the legacy module hook."""
    root = Path(JARVIS_DIR)
    if root != _INITIAL_JARVIS_DIR:
        return root
    configured = os.environ.get("JARVIS_DIR", "").strip()
    return Path(configured) if configured else root


def _views_dir() -> Path:
    configured = Path(VIEWS_DIR)
    if configured != _INITIAL_VIEWS_DIR:
        return configured
    return runtime_root() / "views"


def publish(
    title: str,
    sections: list[dict],
    meta: dict | None = None,
    ttl_hours: float = 72,
) -> str:
    """Publish structured content and return a viewable URL.

    Args:
        title: Page title / header
        sections: List of content blocks (see module docstring for types)
        meta: Optional metadata (source module, date, tags, etc.)
        ttl_hours: How long the view stays accessible (default 72h)

    Returns:
        URL string pointing to the rendered view
    """
    view_id = uuid.uuid4().hex[:10]
    view = {
        "id": view_id,
        "title": title,
        "sections": sections,
        "meta": meta or {},
        "created_at": time.time(),
        "expires_at": time.time() + ttl_hours * 3600,
    }

    # Write view data
    views_dir = _views_dir()
    views_dir.mkdir(parents=True, exist_ok=True)
    view_file = views_dir / f"{view_id}.json"
    try:
        view_file.write_text(json.dumps(view, ensure_ascii=False, indent=2))
    except OSError as e:
        # Disk full, unmounted, etc. — return a fallback URL
        import sys
        print(f"[richview] Failed to write view {view_id}: {e}", file=sys.stderr)
        return ""

    # Cleanup old views if over limit
    _cleanup()

    # Build URL from admin config
    config = Config(runtime_root() / "jarvis.yaml")
    host = config.admin.get("host", "127.0.0.1")
    port = int(config.admin.get("port", 3456))
    base = f"http://{host}:{port}"
    return f"{base}/view/{view_id}"


def get_view(view_id: str) -> dict | None:
    """Load a view by ID. Returns None if expired or missing."""
    if not view_id or ".." in view_id or "/" in view_id or "\\" in view_id:
        return None
    views_dir = _views_dir()
    view_file = views_dir / f"{view_id}.json"
    if not view_file.resolve().is_relative_to(views_dir.resolve()):
        return None
    if not view_file.exists():
        return None
    view = json.loads(view_file.read_text())
    if time.time() > view.get("expires_at", 0):
        view_file.unlink(missing_ok=True)
        return None
    return view


def list_views() -> list[dict]:
    """List all active (non-expired) views, newest first."""
    views = []
    for f in sorted(_views_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            v = json.loads(f.read_text())
            if time.time() <= v.get("expires_at", 0):
                views.append({"id": v["id"], "title": v["title"], "created_at": v["created_at"], "meta": v.get("meta", {})})
            else:
                f.unlink(missing_ok=True)
        except (json.JSONDecodeError, KeyError):
            pass
    return views


def _cleanup():
    """Remove oldest views if over MAX_VIEWS."""
    files = sorted(_views_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    while len(files) > MAX_VIEWS:
        files[0].unlink(missing_ok=True)
        files.pop(0)
