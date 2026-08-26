"""Tests for provider credential process boundaries."""

from core.model_credentials import without_model_credentials


def test_without_model_credentials_scrubs_only_model_secrets():
    source = {
        "ANTHROPIC_API_KEY": "primary",
        "CLAUDE_BACKUP_AUTH_TOKEN": "backup",
        "OPENAI_API_KEY": "openai",
        "SAFE_MARKER": "kept",
    }

    clean = without_model_credentials(source)

    assert clean == {"SAFE_MARKER": "kept"}
    assert source["OPENAI_API_KEY"] == "openai"


def test_without_model_credentials_can_keep_the_active_provider_secret():
    clean = without_model_credentials(
        {
            "ANTHROPIC_API_KEY": "primary",
            "ANTHROPIC_AUTH_TOKEN": "relay",
            "OPENAI_API_KEY_CONFIG": "configured-openai",
        },
        keep=frozenset({"ANTHROPIC_API_KEY"}),
    )

    assert clean == {"ANTHROPIC_API_KEY": "primary"}
