"""First-principles contract for owner-visible Jarvis messages.

Codex owns user-initiated work. Jarvis earns an interruption only when a
cross-time trigger, external change, completed asynchronous result, or an
irreducible owner decision cannot be supplied by a foreground Codex task at
the right moment. This module is pure so every producer and audit uses the
same vocabulary.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable


OWNER_NEEDS = {
    "none",
    "judgment",
    "authority",
    "deadline",
    "requested_result",
    "external_change",
    "scheduled_companion",
    "decision_batch",
}

OWNER_NEED_LABELS = {
    "none": "不需要打扰",
    "judgment": "需要你的判断",
    "authority": "需要你的授权",
    "deadline": "错过时机会有代价",
    "requested_result": "你交代的工作有结果了",
    "external_change": "外部的人或状态变了",
    "scheduled_companion": "你主动订阅的陪伴节奏到了",
    "decision_batch": "值得合并批一次的判断",
}

MESSAGE_GOALS = {
    "none": "preserve_state",
    "judgment": "unlock_judgment",
    "authority": "unlock_authority",
    "deadline": "protect_time_or_opportunity",
    "requested_result": "return_entrusted_result",
    "external_change": "surface_material_external_change",
    "scheduled_companion": "honor_retained_rhythm",
    "decision_batch": "compress_owner_decisions",
}

MESSAGE_CONTRACT_FIELDS = (
    "owner_need",
    "why_now",
    "owner_action",
    "silence_cost",
    "message_gate_version",
    "owner_need_explicit",
)

_SOURCE_NEEDS = {
    "attention-roi": "none",
    "cross-session-sync": "none",
    "checkin": "scheduled_companion",
    "daily-reflect": "scheduled_companion",
    "exercise-week": "scheduled_companion",
    "morning-anchor": "decision_batch",
    "memorial-escrow": "decision_batch",
    "weekly-review": "decision_batch",
    "mail": "external_change",
    "eigenflux": "external_change",
    "eigenflux-feed-triage": "external_change",
    "eigenflux-friends": "authority",
    "eigenflux-publish": "authority",
    "delegation": "judgment",
    "iteration-observe": "judgment",
    "intentions": "judgment",
    "calendar-sync": "deadline",
    "selfmon": "authority",
    "guardian-daemon": "deadline",
}


def infer_owner_need(source: str, attention: str) -> str:
    """Compatibility inference for model-authored and historical cards."""
    src = str(source or "")
    if src.startswith("routine:"):
        return "scheduled_companion"
    if src in _SOURCE_NEEDS:
        return _SOURCE_NEEDS[src]
    if attention == "alert":
        return "deadline"
    if attention == "decision":
        return "judgment"
    return "requested_result"


def build_message_contract(
    *,
    source: str,
    attention: str,
    work_receipt: str,
    owner_need: str = "",
    why_now: str = "",
    owner_action: str = "",
    silence_cost: str = "",
) -> dict:
    """Normalize and validate the versioned contract for a new Item."""
    explicit = bool(str(owner_need).strip())
    contract = {
        "owner_need": str(owner_need).strip() or infer_owner_need(
            source, attention),
        "why_now": " ".join(str(why_now).split())[:180],
        "owner_action": " ".join(str(owner_action).split())[:180],
        "silence_cost": " ".join(str(silence_cost).split())[:180],
        "message_gate_version": 2 if explicit else 0,
        "owner_need_explicit": explicit,
    }
    validate_explicit({
        "source": source,
        "attention": attention,
        "work_receipt": work_receipt,
        **contract,
    })
    return contract


def evaluate(state: dict) -> dict:
    """Return the delivery lane and contract errors for one Item."""
    explicit = bool(state.get("owner_need_explicit"))
    attention = str(state.get("attention") or "notice")
    requested_need = str(state.get("owner_need") or "")
    unknown_explicit = explicit and requested_need not in OWNER_NEEDS
    need = requested_need if requested_need in OWNER_NEEDS else infer_owner_need(
        str(state.get("source") or ""), attention)
    why_now = " ".join(str(state.get("why_now") or "").split())
    receipt = " ".join(str(state.get("work_receipt") or "").split())
    owner_action = " ".join(str(state.get("owner_action") or "").split())
    silence_cost = " ".join(str(state.get("silence_cost") or "").split())
    gate_version = int(state.get("message_gate_version") or 0)
    errors: list[str] = []
    if unknown_explicit:
        errors.append(f"unknown owner need: {requested_need}")
    if explicit and need != "none" and not receipt:
        errors.append("owner-visible message requires completed-work evidence")
    if explicit and need != "none" and not why_now:
        errors.append("owner-visible message requires why-now evidence")
    if explicit and need != "none" and gate_version >= 2 and not owner_action:
        errors.append("owner-visible message requires one minimal owner action")
    if explicit and need != "none" and gate_version >= 2 and not silence_cost:
        errors.append("owner-visible message requires cost-of-silence evidence")
    if need == "none" and attention in {"decision", "alert"}:
        errors.append("non-interrupting work cannot demand attention")
    if attention == "alert" and need != "deadline":
        errors.append("only deadline risk may use the alert lane")
    if attention == "decision" and need not in {
            "judgment", "authority", "decision_batch"}:
        errors.append("decision lane requires judgment or authority")
    if need == "judgment" and attention != "decision":
        errors.append("judgment must use the decision lane")
    if need == "deadline" and attention != "alert":
        errors.append("deadline risk must use the alert lane")
    if need == "scheduled_companion" and attention != "notice":
        errors.append("scheduled companion work must remain optional")
    lane = "ledger" if need == "none" else "lark"
    cadence = (
        "immediate" if need == "deadline"
        else "batch" if need == "decision_batch"
        else "bounded"
    )
    return {
        "owner_need": need,
        "message_goal": MESSAGE_GOALS[need],
        "label": OWNER_NEED_LABELS[need],
        "lane": lane,
        "cadence": cadence,
        "why_now": why_now,
        "owner_action": owner_action,
        "silence_cost": silence_cost,
        "message_gate_version": gate_version,
        "explicit": explicit,
        "valid": not errors,
        "errors": errors,
    }


def validate_explicit(state: dict) -> dict:
    decision = evaluate(state)
    if state.get("owner_need_explicit") and not decision["valid"]:
        raise ValueError("; ".join(decision["errors"]))
    return decision


def audit(states: Iterable[dict]) -> dict:
    rows = list(states)
    decisions = [evaluate(row) for row in rows]
    needs = Counter(item["owner_need"] for item in decisions)
    goals = Counter(item["message_goal"] for item in decisions)
    lanes = Counter(item["lane"] for item in decisions)
    explicit_invalid = sum(
        1 for item in decisions if item["explicit"] and not item["valid"])
    legacy_mismatch = sum(
        1 for item in decisions if not item["explicit"] and not item["valid"])
    return {
        "items": len(rows),
        "explicit": sum(1 for item in decisions if item["explicit"]),
        "legacy_inferred": sum(1 for item in decisions if not item["explicit"]),
        "invalid": explicit_invalid + legacy_mismatch,
        "explicit_invalid": explicit_invalid,
        "legacy_mismatch": legacy_mismatch,
        "gate_v2_visible": sum(
            1 for item in decisions
            if item["explicit"] and item["owner_need"] != "none"
            and item["message_gate_version"] >= 2
        ),
        "legacy_explicit_visible": sum(
            1 for item in decisions
            if item["explicit"] and item["owner_need"] != "none"
            and item["message_gate_version"] < 2
        ),
        "by_owner_need": dict(sorted(needs.items())),
        "by_message_goal": dict(sorted(goals.items())),
        "by_lane": dict(sorted(lanes.items())),
    }
