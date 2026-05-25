"""Tests for core.compact — session compaction (summary generation)."""

import json
import uuid
from pathlib import Path
from unittest.mock import patch

from core.compact import get_compact_path, get_old_session_id, read_compact


def test_get_compact_path(tmp_path):
    path = get_compact_path(tmp_path, "user123")
    assert "user123" in str(path)
    assert path.suffix == ".md"


def test_read_compact_exists(tmp_path):
    path = get_compact_path(tmp_path, "user123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Previous session summary content")
    result = read_compact(tmp_path, "user123")
    assert "Previous session summary" in result


def test_read_compact_missing(tmp_path):
    result = read_compact(tmp_path, "nonexistent")
    assert result == ""


def test_get_old_session_id_deterministic():
    """Same inputs must always produce the same session ID."""
    id1 = get_old_session_id("user123", 5)
    id2 = get_old_session_id("user123", 5)
    assert id1 == id2
    # Must be a valid UUID
    uuid.UUID(id1)


def test_get_old_session_id_different_counters():
    id1 = get_old_session_id("user123", 5)
    id2 = get_old_session_id("user123", 6)
    assert id1 != id2
