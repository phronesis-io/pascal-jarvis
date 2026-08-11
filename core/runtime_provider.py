"""Durable per-conversation model-provider preference.

The preference changes routing order, not truth: the provider/model recorded
after a reply is always the route that actually answered.
"""

from __future__ import annotations

import argparse

from core.timeutil import now_local_str


VALID_PREFERENCES = {"auto", "codex"}


def _db():
    from dashboard.db import get_db
    return get_db()


def get_preference(conv_key: str) -> str:
    key = str(conv_key or "").strip()
    if not key:
        return "auto"
    row = _db().execute(
        "SELECT preference FROM conversation_provider_preferences "
        "WHERE conv_key = ?",
        (key,),
    ).fetchone()
    value = str(row["preference"] if row else "auto")
    return value if value in VALID_PREFERENCES else "auto"


def set_preference(conv_key: str, preference: str) -> str:
    key = str(conv_key or "").strip()
    value = str(preference or "").strip().lower()
    if not key:
        raise ValueError("conv_key is required")
    if value not in VALID_PREFERENCES:
        raise ValueError(f"unsupported provider preference: {value}")
    db = _db()
    db.execute(
        """INSERT INTO conversation_provider_preferences
           (conv_key, preference, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(conv_key) DO UPDATE SET
             preference=excluded.preference, updated_at=excluded.updated_at""",
        (key, value, now_local_str("%Y-%m-%dT%H:%M:%S")),
    )
    db.commit()
    return value


def preference_label(preference: str) -> str:
    if preference == "codex":
        return "Codex 优先（不可用时自动由 Claude 接力）"
    return "Claude 优先（不可用时自动由 Codex 接力）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    get_parser = sub.add_parser("get")
    get_parser.add_argument("conv_key")
    set_parser = sub.add_parser("set")
    set_parser.add_argument("conv_key")
    set_parser.add_argument("preference", choices=sorted(VALID_PREFERENCES))
    args = parser.parse_args(argv)
    if args.command == "get":
        print(get_preference(args.conv_key))
    else:
        print(set_preference(args.conv_key, args.preference))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
