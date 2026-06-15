"""REQ-77 model-crash / spend-limit graceful fallback."""
from core import model_fallback as mf


def test_model_error_detection():
    assert mf.is_model_error("There's an issue with the selected model (claude-fable-5)")
    assert mf.is_model_error("You've hit your monthly spend limit")
    assert mf.is_model_error("invalid model")
    assert not mf.is_model_error("connection reset by peer")
    assert not mf.is_model_error("")


def test_fallback_chain():
    # model-unavailable → one-step degrade
    assert mf.fallback_for_stderr("opus", "issue with the selected model") == "sonnet"
    assert mf.fallback_for_stderr("sonnet", "invalid model") == "haiku"
    assert mf.fallback_for_stderr("haiku", "invalid model") is None  # exhausted


def test_spend_limit_jumps_to_cheapest():
    assert mf.fallback_for_stderr("opus", "monthly spend limit") == "haiku"
    assert mf.fallback_for_stderr("sonnet", "spend limit") == "haiku"
    assert mf.fallback_for_stderr("haiku", "spend limit") is None  # already cheapest


def test_transient_error_no_fallback():
    assert mf.fallback_for_stderr("opus", "connection reset") is None
    assert mf.fallback_for_stderr("opus", "") is None


def test_fable_never_in_chain():
    assert "fable" not in [m.lower() for m in mf.DEGRADE_CHAIN]


def test_cli(tmp_path):
    import subprocess, sys
    r = subprocess.run([sys.executable, "-m", "core.model_fallback", "opus"],
                       input="issue with the selected model", capture_output=True, text=True)
    assert r.stdout.strip() == "sonnet"


def test_bot_sh_wires_reply_closure_and_model_fallback():
    from pathlib import Path
    bot = (Path(__file__).parent.parent / "bot.sh").read_text()
    assert "core.reply_closure" in bot         # REQ-64 wired
    assert "core.model_fallback" in bot        # REQ-77 wired
    assert '"$_cur_model"' in bot              # main path uses degradable model
