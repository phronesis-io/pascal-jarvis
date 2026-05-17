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
import time
import uuid
from pathlib import Path

from core.config import Config

ROOT = Path(__file__).resolve().parent.parent
_config = Config(ROOT / "jarvis.yaml")

VIEWS_DIR = ROOT / "views"
VIEWS_DIR.mkdir(exist_ok=True)

# Max views to keep (FIFO cleanup)
MAX_VIEWS = 200


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
    view_file = VIEWS_DIR / f"{view_id}.json"
    view_file.write_text(json.dumps(view, ensure_ascii=False, indent=2))

    # Cleanup old views if over limit
    _cleanup()

    # Build URL from admin config
    host = _config.admin.get("host", "127.0.0.1")
    port = int(_config.admin.get("port", 3456))
    base = f"http://{host}:{port}"
    return f"{base}/view/{view_id}"


def get_view(view_id: str) -> dict | None:
    """Load a view by ID. Returns None if expired or missing."""
    view_file = VIEWS_DIR / f"{view_id}.json"
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
    for f in sorted(VIEWS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
    files = sorted(VIEWS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    while len(files) > MAX_VIEWS:
        files[0].unlink(missing_ok=True)
        files.pop(0)
