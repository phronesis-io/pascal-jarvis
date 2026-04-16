"""Smoke tests for plugins/eigenflux/setup.py — ensure the wizard module
imports cleanly and its helper functions behave (without actually running
the interactive login flow)."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_setup_module():
    spec = importlib.util.spec_from_file_location(
        "eigenflux_setup",
        ROOT / "plugins" / "eigenflux" / "setup.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_module_imports_cleanly():
    mod = _load_setup_module()
    # Must expose the main entry point
    assert callable(mod.main)


def test_is_valid_email():
    mod = _load_setup_module()
    assert mod.is_valid_email("you@example.com")
    assert mod.is_valid_email("foo.bar+tag@sub.example.co.uk")
    assert not mod.is_valid_email("no-at-sign")
    assert not mod.is_valid_email("foo@")
    assert not mod.is_valid_email("@example.com")
    assert not mod.is_valid_email("")


def test_existing_client_returns_client_with_correct_workdir():
    mod = _load_setup_module()
    c = mod.existing_client()
    assert c.workdir == ROOT / "eigenflux"
    assert c.workdir.is_dir()
