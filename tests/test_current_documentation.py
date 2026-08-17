"""Current-state documentation must not drift back to retired product rules."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return " ".join(text.split())


def test_current_docs_share_the_product_surface_contract():
    current = _read("docs/current_system.md")
    product = _read("PRODUCT.md")
    design = _read("DESIGN.md")
    decisions = _read("DECISIONS.md")

    assert "Lark is the product" in product
    assert "Lark is the sole user-facing delivery and decision surface" in decisions
    assert "Mobile gateway `:3458` | None | Retired" in current
    assert "Tailscale" in design and "retired" in design
    assert "Product expansion is frozen" in product


def test_current_docs_distinguish_active_routines_from_frozen_expansion():
    current = _read("docs/current_system.md")
    product = _read("PRODUCT.md")
    domain = _read("DOMAIN.md")
    architecture = _read("ARCHITECTURE.md")

    assert "Existing Routines are active" in current
    assert "existing definitions remain active" in product
    assert "`no_output` and `deferred` are different facts" in domain
    assert "claimed runs become `deferred`" in architecture


def test_install_docs_distinguish_code_release_from_runtime_restart():
    readme = _read("README.md")
    install = _read("docs/INSTALL.md")
    contributing = _read("CONTRIBUTING.md")

    assert "Code pulled from Git must go through the governed full release path" in readme
    assert "代码变化必须走受治理的 `./restart.sh` 发布路径" in install
    assert "`--runtime` is only" in contributing
    assert "Delete it to force all tasks" not in readme


def test_historical_prds_cannot_override_current_inventory():
    index = _read("docs/README.md")
    portfolio = _read("docs/prd_portfolio.md")
    historical_source = _read("docs/prd_percep" + "tion_ingestion.md")

    assert "A historical PRD cannot implicitly add a surface" in _squash(index)
    assert "Core accepted and shipped; expansion demand-gated" in portfolio
    assert "core shipped; expansion frozen" in historical_source
    assert "not current inventory" in historical_source
    assert "historical with two deliberate shadows" in _read(
        "docs/prd_interaction_v4.md"
    )
    assert "Do not recreate a parallel" in _read("docs/design_task_system.md")


def test_concurrency_docs_cover_multi_provider_session_boundaries():
    concurrency = _read("docs/concurrency_and_bg_jobs.md")

    normalized = _squash(concurrency)
    assert "This document covers Claude-compatible" in normalized
    assert "Codex CLI, and GPT API routes" in normalized
    assert "One physical provider session or thread" in normalized
    assert "Ambiguous tool-capable failures stop" in normalized
    assert "context captured at launch" in normalized


def test_links_in_current_state_documents_resolve_locally():
    documents = (
        "README.md",
        "AGENTS.md",
        "PRODUCT.md",
        "DESIGN.md",
        "DOMAIN.md",
        "ARCHITECTURE.md",
        "DECISIONS.md",
        "CONTRIBUTING.md",
        "docs/current_system.md",
        "docs/README.md",
        "docs/concurrency_and_bg_jobs.md",
        "docs/prd_percep" "tion_ingestion.md",
        "docs/prd_interaction_quality.md",
        "docs/prd_interaction_v3.md",
        "docs/prd_interaction_v4.md",
        "docs/prd_system_iteration_v2.md",
        "docs/prd_card_delivery_closure.md",
        "docs/design_task_system.md",
        "docs/heartbeat_tasks.md",
        "docs/INSTALL.md",
        "docs/RESTORE.md",
        "docs/prd_portfolio.md",
        "docs/engineering_health.md",
        "docs/repository_scorecard.md",
        "plugins/lark/README.md",
        "plugins/eigenflux/README.md",
    )
    missing: list[str] = []
    for name in documents:
        source = ROOT / name
        for raw in re.findall(r"\[[^\]]+\]\(([^)]+)\)", _read(name)):
            target = raw.strip().strip("<>").split("#", 1)[0]
            if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                continue
            if not (source.parent / target).resolve().exists():
                missing.append(f"{name}: {raw}")
    assert missing == []
