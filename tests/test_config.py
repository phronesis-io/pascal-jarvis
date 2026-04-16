"""Tests for core.config — YAML loading, defaults, path resolution."""

from pathlib import Path

import pytest
import yaml

from core.config import Config


def _write_yaml(path: Path, data: dict):
    path.write_text(yaml.safe_dump(data))


def test_defaults_when_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = Config(None)
    assert cfg.claude["heartbeat_model"] == "sonnet"
    assert cfg.claude["max_session_size"] == 512000
    assert cfg.heartbeat["check_interval"] == 10


def test_user_config_overrides_defaults(tmp_path):
    cfg_file = tmp_path / "jarvis.yaml"
    _write_yaml(cfg_file, {
        "data_dir": str(tmp_path / "data"),
        "claude": {"heartbeat_model": "opus"},
    })
    cfg = Config(cfg_file)
    assert cfg.claude["heartbeat_model"] == "opus"
    # unmodified default survives deep merge
    assert cfg.claude["max_session_size"] == 512000


def test_work_dir_defaults_to_jarvis_dir(tmp_path):
    cfg_file = tmp_path / "jarvis.yaml"
    _write_yaml(cfg_file, {"data_dir": str(tmp_path / "data")})
    cfg = Config(cfg_file)
    assert cfg.work_dir == cfg.jarvis_dir


def test_work_dir_explicit(tmp_path):
    cfg_file = tmp_path / "jarvis.yaml"
    work = tmp_path / "project"
    _write_yaml(cfg_file, {
        "data_dir": str(tmp_path / "data"),
        "work_dir": str(work),
    })
    cfg = Config(cfg_file)
    assert cfg.work_dir == work


def test_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_file = tmp_path / "jarvis.yaml"
    _write_yaml(cfg_file, {"data_dir": "~/my_data", "work_dir": "~/project"})
    cfg = Config(cfg_file)
    assert cfg.data_dir == tmp_path / "my_data"
    assert cfg.work_dir == tmp_path / "project"


def test_dot_path_get(tmp_path):
    cfg_file = tmp_path / "jarvis.yaml"
    _write_yaml(cfg_file, {"claude": {"heartbeat_model": "haiku"}})
    cfg = Config(cfg_file)
    assert cfg.get("claude.heartbeat_model") == "haiku"
    assert cfg.get("claude.missing", "default") == "default"
    assert cfg.get("nonexistent.path") is None


def test_memory_dir_relative_to_data_dir(tmp_path):
    cfg_file = tmp_path / "jarvis.yaml"
    _write_yaml(cfg_file, {
        "data_dir": str(tmp_path / "data"),
        "memory": {"dir": "mem_custom"},
    })
    cfg = Config(cfg_file)
    assert cfg.memory_dir == tmp_path / "data" / "mem_custom"
