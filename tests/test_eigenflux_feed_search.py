"""Tests for plugins.eigenflux.feed_search."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_cli_cache(tmp: Path, server: str, day: str, items: list[dict]) -> Path:
    """Mimic the official CLI cache layout."""
    day_dir = tmp / "servers" / server / "data" / "broadcasts" / day
    day_dir.mkdir(parents=True, exist_ok=True)
    f = day_dir / f"feeds-{day}-120000.json"
    f.write_text(json.dumps({"items": items, "has_more": False}))
    return f


def _make_legacy_jsonl(repo_root: Path, items: list[dict]) -> Path:
    """Write the frozen legacy jsonl that feed_search may fall back to."""
    legacy = repo_root / "eigenflux" / "feed_store.jsonl"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    with legacy.open("w") as f:
        for it in items:
            f.write(json.dumps(it) + "\n")
    return legacy


def _reload_feed_search(tmp_home: Path, tmp_repo: Path):
    """Reimport feed_search with patched paths."""
    import importlib
    import plugins.eigenflux.feed_search as fs
    importlib.reload(fs)
    fs.CLI_HOME = tmp_home
    fs.LEGACY_JSONL = tmp_repo / "eigenflux" / "feed_store.jsonl"
    return fs


class TestFeedSearch:
    def setup_method(self):
        self.tmp_home = Path(tempfile.mkdtemp(prefix="ef-home-"))
        self.tmp_repo = Path(tempfile.mkdtemp(prefix="ef-repo-"))
        self.fs = _reload_feed_search(self.tmp_home, self.tmp_repo)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmp_home, ignore_errors=True)
        shutil.rmtree(self.tmp_repo, ignore_errors=True)

    def test_empty_query_returns_empty(self):
        assert self.fs.search_feed_history("") == []

    def test_no_cache_returns_empty(self):
        assert self.fs.search_feed_history("anything") == []

    def test_match_against_cli_summary(self):
        _make_cli_cache(self.tmp_home, "eigenflux", "20260521", [
            {"item_id": "1", "summary": "Multi-agent systems and credit assignment",
             "domains": ["AI"], "updated_at": "2026-05-21T12:00:00Z"},
            {"item_id": "2", "summary": "Cooking with cast iron",
             "domains": ["Food"], "updated_at": "2026-05-21T12:05:00Z"},
        ])
        results = self.fs.search_feed_history("multi-agent")
        assert len(results) == 1
        assert results[0]["item_id"] == "1"
        assert results[0]["source"] == "cli-cache"

    def test_match_is_case_insensitive(self):
        _make_cli_cache(self.tmp_home, "eigenflux", "20260521", [
            {"item_id": "1", "summary": "PROMPT engineering deep dive",
             "updated_at": "2026-05-21T12:00:00Z"},
        ])
        assert len(self.fs.search_feed_history("prompt")) == 1
        assert len(self.fs.search_feed_history("PROMPT")) == 1
        assert len(self.fs.search_feed_history("Prompt")) == 1

    def test_match_against_keywords_and_domains(self):
        _make_cli_cache(self.tmp_home, "eigenflux", "20260521", [
            {"item_id": "1", "summary": "...", "keywords": ["RLHF", "DPO"],
             "domains": ["AI"], "updated_at": "2026-05-21T12:00:00Z"},
        ])
        assert len(self.fs.search_feed_history("rlhf")) == 1

    def test_dedup_prefers_cli_over_legacy(self):
        _make_cli_cache(self.tmp_home, "eigenflux", "20260521", [
            {"item_id": "shared", "summary": "from CLI cache (newer schema)",
             "url": "https://cli.example", "updated_at": "2026-05-21T12:00:00Z"},
        ])
        _make_legacy_jsonl(self.tmp_repo, [
            {"item_id": "shared", "summary": "from legacy jsonl (older)",
             "fetched_at": "2026-04-17T12:00:00Z"},
        ])
        results = self.fs.search_feed_history("from")
        assert len(results) == 1
        assert results[0]["source"] == "cli-cache"
        assert results[0]["url"] == "https://cli.example"

    def test_legacy_archives_searchable_when_cli_empty(self):
        _make_legacy_jsonl(self.tmp_repo, [
            {"item_id": "old-1", "summary": "archived ROC analysis from April",
             "fetched_at": "2026-04-10T08:00:00Z"},
        ])
        results = self.fs.search_feed_history("ROC")
        assert len(results) == 1
        assert results[0]["source"] == "legacy-jsonl"

    def test_results_sorted_by_updated_at_desc(self):
        _make_cli_cache(self.tmp_home, "eigenflux", "20260521", [
            {"item_id": "old", "summary": "match",
             "updated_at": "2026-05-21T08:00:00Z"},
            {"item_id": "new", "summary": "match",
             "updated_at": "2026-05-21T20:00:00Z"},
            {"item_id": "mid", "summary": "match",
             "updated_at": "2026-05-21T14:00:00Z"},
        ])
        results = self.fs.search_feed_history("match", limit=10)
        assert [r["item_id"] for r in results] == ["new", "mid", "old"]

    def test_limit_respected(self):
        items = [
            {"item_id": str(i), "summary": "match",
             "updated_at": f"2026-05-21T{i:02d}:00:00Z"}
            for i in range(10)
        ]
        _make_cli_cache(self.tmp_home, "eigenflux", "20260521", items)
        assert len(self.fs.search_feed_history("match", limit=3)) == 3

    def test_format_results_empty(self):
        assert self.fs.format_results([]) == "没找到相关内容"

    def test_format_results_renders_url_and_truncates(self):
        out = self.fs.format_results([{
            "item_id": "1", "summary": "x" * 200,
            "suggestion": "Read paper Y — apply to Z" + "!" * 200,
            "url": "https://example.com",
            "updated_at": "2026-05-21T12:00:00Z",
            "source": "cli-cache",
        }])
        assert "https://example.com" in out
        assert "[2026-05-21T12:00]" in out
        assert "…" in out  # truncated

    def test_corrupt_cache_file_skipped(self):
        day_dir = self.tmp_home / "servers" / "eigenflux" / "data" / "broadcasts" / "20260521"
        day_dir.mkdir(parents=True)
        (day_dir / "feeds-20260521-120000.json").write_text("not valid json {{{")
        (day_dir / "feeds-20260521-130000.json").write_text(
            json.dumps({"items": [{"item_id": "ok", "summary": "real match",
                                   "updated_at": "2026-05-21T13:00:00Z"}]})
        )
        results = self.fs.search_feed_history("real match")
        assert len(results) == 1
        assert results[0]["item_id"] == "ok"
