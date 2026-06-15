"""Tests for core.doc_guard — protected-doc write verification (REQ-65)."""

from core import doc_guard as dg


def test_readback_counts_live_not_generation():
    assert dg.readback_count("<latex>a</latex> <latex>b</latex>", "<latex>") == 2
    assert dg.readback_count("no formulas here", "<latex>") == 0
    assert dg.readback_count("Day1 Day2 Day3", r"Day\d", regex=True) == 3


def test_diff_blocks_detects_destructive_overwrite():
    d = dg.diff_blocks("a\nb\nc\nd\ne", "x")
    assert d["blocks_before"] == 5
    assert d["deleted"] == 5
    assert d["deletion_ratio"] == 1.0


def test_diff_blocks_patch_is_low_deletion():
    d = dg.diff_blocks("a\nb\nc", "a\nb\nc\nd")
    assert d["deleted"] == 0 and d["added"] == 1
    assert d["deletion_ratio"] == 0.0


def test_verify_rejects_fake_success_count():
    """The whitepaper bug: claim 56 <latex> on a doc that has zero."""
    v = dg.verify_write("a\nb", "a\nb", claim_feature="<latex>", claim_min_count=56)
    assert v["ok"] is False
    assert "read-back finds 0" in v["reason"]
    assert v["readback"] == 0


def test_verify_rejects_destructive_overwrite():
    """The handbook bug: a 'rebuild' that wipes hand-entered blocks."""
    before = "\n".join(f"line{i}" for i in range(10))
    v = dg.verify_write(before, "rebuilt single line")
    assert v["ok"] is False
    assert "destructive" in v["reason"]


def test_verify_allows_confirmed_overwrite():
    before = "\n".join(f"line{i}" for i in range(10))
    v = dg.verify_write(before, "fully rewritten", allow_overwrite=True)
    assert v["ok"] is True


def test_verify_accepts_legit_patch_with_met_claim():
    v = dg.verify_write("intro\nbody\nend", "intro\nbody\nnew formula <latex>x</latex>\nend",
                        claim_feature="<latex>", claim_min_count=1)
    assert v["ok"] is True
    assert v["readback"] == 1


def test_cli_verify_exit_code(tmp_path):
    import subprocess, sys
    before = tmp_path / "b.txt"; before.write_text("a\nb\nc")
    after = tmp_path / "a.txt"; after.write_text("a\nb\nc")
    r = subprocess.run([sys.executable, "-m", "core.doc_guard", "verify",
                        str(before), str(after), "--feature", "<latex>", "--min", "5"],
                       capture_output=True, text=True)
    assert r.returncode == 1   # claim unmet → non-zero


def test_behavioral_rules_carry_the_new_discipline():
    """The REQ-65/69/74 rules must be in the loaded behavioral_rules.md so the
    bot actually follows them (the deterministic helper alone isn't enough)."""
    from pathlib import Path
    md = Path.home() / ".claude/projects/-Users-pascal-Desktop-jarvis-repos-pascal-jarvis/memory/hot/behavioral_rules.md"
    if not md.exists():
        import pytest
        pytest.skip("behavioral_rules.md not present in this environment")
    text = md.read_text(encoding="utf-8")
    assert "core.doc_guard" in text            # REQ-65 read-back protocol
    assert "截断" in text and "甩" in text       # REQ-69 no false-truncation blame
    assert "裸 URL" in text or "[文字](链接)" in text  # REQ-69 link lint rule
    assert "表忠心" in text or "证据行" in text   # REQ-74 evidence not narrative


def test_diff_counts_duplicate_row_deletions_with_multiplicity():
    """Red-team P1: a set-based diff collapsed N identical rows to one, so
    wiping 30 table rows read as deleting 1 (ratio 0.024) and passed as safe."""
    before = "\n".join(["intro"] + [f"| row | data{i} |" for i in range(5)]
                        + ["| --- | --- |"] * 30)   # 30 identical separator rows
    after = "intro\n| header |"
    d = dg.diff_blocks(before, after)
    assert d["deleted"] >= 30                       # counted with multiplicity
    assert d["deletion_ratio"] > 0.30
    v = dg.verify_write(before, after)
    assert v["ok"] is False and "destructive" in v["reason"]
