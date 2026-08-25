from __future__ import annotations

from core.memory_relevance import relevant_warm_lines


def test_retrieval_finds_exact_warm_context_without_loading_whole_file(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "team.md").write_text(
        "Vic 负责白皮书编辑，Blog 05 发布前和她对稿。\n"
        "另一条无关的普通记录。\n",
        encoding="utf-8",
    )
    (warm / "noise.md").write_text("Blog 是一种常见写作形式。", encoding="utf-8")

    matches = relevant_warm_lines(
        tmp_path, ["Blog 05 发布前和 Vic 对稿"], max_chars=500)

    assert len(matches) == 1
    assert matches[0]["file"] == "team.md"
    assert "Vic 负责白皮书编辑" in matches[0]["text"]


def test_retrieval_requires_two_overlaps_and_stays_bounded(tmp_path):
    warm = tmp_path / "warm"
    warm.mkdir()
    (warm / "notes.md").write_text(
        "\n".join(f"EigenFlux Blog 05 line {index}" for index in range(50)),
        encoding="utf-8",
    )

    matches = relevant_warm_lines(
        tmp_path, ["EigenFlux Blog 05"], max_lines=3, max_chars=300)

    assert 1 <= len(matches) <= 3
    assert sum(len(row["text"]) for row in matches) <= 300


def test_retrieval_skips_archive_and_index(tmp_path):
    warm = tmp_path / "warm"
    archive = warm / "archive"
    archive.mkdir(parents=True)
    (warm / "_index.md").write_text("Vic Blog 05", encoding="utf-8")
    (archive / "old.md").write_text("Vic Blog 05", encoding="utf-8")

    assert relevant_warm_lines(tmp_path, ["Vic Blog 05"]) == []
