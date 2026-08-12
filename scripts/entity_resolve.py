#!/usr/bin/env python3
"""Entity resolver: cross-references names/aliases against known contacts.

Reads JSON from stdin containing entity names (from friend requests, messages,
feed items, etc.), enriches with matches from the contacts registry and team
memory file.

Usage:
    echo '{"requests": [...]}' | python3 entity_resolve.py
    echo '{"messages": [...]}' | python3 entity_resolve.py

The script auto-detects the input format and extracts entity names from:
- Friend requests: from_name, greeting
- Messages: sender_name, author_name
- Feed items: publisher_name, author_name

Output: original JSON with "entity_matches" array appended.
"""

import json
import os
import re
import sys
from pathlib import Path

# ── Locate code and data independently ────────────────────────────

CODE_ROOT = Path(__file__).resolve().parent.parent
JARVIS_DIR = Path(os.environ.get("JARVIS_DIR") or CODE_ROOT)
sys.path.insert(0, str(CODE_ROOT))
from core.claude_projects import auto_memory_dir as _auto_mem
from core.eigenflux_friends import temporary_friend_policy_active
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", str(_auto_mem())))

CONTACTS_FILE = JARVIS_DIR / "data" / "contacts.jsonl"
TEAM_FILE = MEMORY_DIR / "warm" / "team.md"


# ── Load known entities ────────────────────────────────────────────

def load_contacts() -> list[dict]:
    """Load structured contacts from contacts.jsonl."""
    contacts = []
    if CONTACTS_FILE.exists():
        for line in CONTACTS_FILE.read_text().strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                contacts.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return contacts


def parse_team_md() -> list[dict]:
    """Extract team members from warm/team.md as contact entries."""
    contacts = []
    if not TEAM_FILE.exists():
        return contacts

    text = TEAM_FILE.read_text()
    # Match lines like: - **Name** role...
    for line in text.splitlines():
        m = re.match(r"^- \*\*(.+?)\*\*\s*(.*)", line)
        if not m:
            continue
        name = m.group(1).strip()
        rest = m.group(2).strip()

        entry = {"name": name, "aliases": [], "source": "team.md"}

        # Extract EigenFlux 网名 pattern: 网名"X"(Y)
        alias_match = re.search(r'网名["\u201c](.+?)["\u201d]', rest)
        if alias_match:
            entry["aliases"].append(alias_match.group(1))

        # Extract pinyin/bracket alias: (CiWei)
        paren_match = re.search(r'\(([A-Za-z]+)\)', rest)
        if paren_match:
            entry["aliases"].append(paren_match.group(1))

        # Extract AI assistant name: 助手"X"
        assistant_match = re.search(r'助手["\u201c](.+?)["\u201d]', rest)
        if assistant_match:
            entry["aliases"].append(assistant_match.group(1))

        # Extract agent_id
        aid_match = re.search(r'agent_id:\s*(\d+)', rest)
        if aid_match:
            entry["agent_id"] = aid_match.group(1)

        # Extract agent name from parenthetical: (OpenClaw Assistant, ...)
        agent_name_match = re.search(r'\(([^)]*Assistant[^)]*)', rest)
        if agent_name_match:
            entry["aliases"].append(agent_name_match.group(1).split(",")[0].strip())

        contacts.append(entry)

    return contacts


def build_lookup(contacts: list[dict]) -> list[dict]:
    """Merge contacts.jsonl and team.md entries, dedup by name."""
    seen = {}
    for c in contacts:
        name = c.get("name", "")
        if name in seen:
            # Merge aliases
            existing = seen[name]
            for a in c.get("aliases", []):
                if a not in existing.get("aliases", []):
                    existing.setdefault("aliases", []).append(a)
            if c.get("agent_id") and not existing.get("agent_id"):
                existing["agent_id"] = c["agent_id"]
        else:
            seen[name] = c
    return list(seen.values())


# ── Matching logic ─────────────────────────────────────────────────

def normalize(s: str) -> str:
    return s.lower().strip()


def match_entity(name: str, agent_id: str, contacts: list[dict]) -> dict | None:
    """Try to match an entity against known contacts.

    Returns match dict or None.
    """
    name_n = normalize(name)
    if not name_n and not agent_id:
        return None

    for contact in contacts:
        cname = normalize(contact.get("name", ""))
        aliases = [normalize(a) for a in contact.get("aliases", [])]
        caid = contact.get("agent_id", "")

        # 1. Agent ID exact match (strongest signal)
        if agent_id and caid and str(agent_id) == str(caid):
            return {
                "matched_contact": contact["name"],
                "match_type": "agent_id_exact",
                "confidence": "high",
                "source": contact.get("source", "contacts.jsonl"),
            }

        # 2. Exact name match
        if name_n and name_n == cname:
            return {
                "matched_contact": contact["name"],
                "match_type": "name_exact",
                "confidence": "high",
                "source": contact.get("source", "contacts.jsonl"),
            }

        # 3. Exact alias match
        if name_n and name_n in aliases:
            return {
                "matched_contact": contact["name"],
                "match_type": "alias_exact",
                "confidence": "high",
                "source": contact.get("source", "contacts.jsonl"),
            }

        # 4. Substring match (name contains alias or alias contains name)
        #    Minimum 3 chars to avoid false positives (e.g. "ai" matching everything)
        for alias in aliases:
            if name_n and alias and len(name_n) >= 3 and len(alias) >= 3:
                if name_n in alias or alias in name_n:
                    return {
                        "matched_contact": contact["name"],
                        "match_type": "substring",
                        "confidence": "medium",
                        "matched_alias": alias,
                        "source": contact.get("source", "contacts.jsonl"),
                    }

        # 5. Pinyin reversal: try swapping two halves for 2-syllable pinyin names
        #    e.g., "ciwei" → "weici" ≈ "Weici"
        #    Only attempt for even-length names 4-10 chars (likely 2-syllable pinyin)
        if name_n and 4 <= len(name_n) <= 10 and len(name_n) % 2 == 0:
            mid = len(name_n) // 2
            reversed_name = name_n[mid:] + name_n[:mid]
            if reversed_name == cname or reversed_name in aliases:
                return {
                    "matched_contact": contact["name"],
                    "match_type": "pinyin_reversal",
                    "confidence": "medium",
                    "source": contact.get("source", "contacts.jsonl"),
                }

    return None


# ── Extract entities from various input formats ────────────────────

def extract_entities(data: dict) -> list[dict]:
    """Extract entity names and IDs from different input formats."""
    entities = []

    # Friend requests
    for req in data.get("requests", []):
        entities.append({
            "name": req.get("from_name", ""),
            "agent_id": req.get("from_uid", ""),
            "context": f"greeting: {req.get('greeting', '')}",
        })

    # Messages
    for msg in data.get("messages", []):
        name = msg.get("sender_name", "") or msg.get("author_name", "")
        aid = msg.get("sender_id", "") or msg.get("author_id", "")
        entities.append({
            "name": name,
            "agent_id": aid,
            "context": "message sender",
        })

    # Feed items (list of dicts or JSON lines)
    for item in data.get("items", []):
        name = item.get("publisher_name", "") or item.get("author_name", "")
        aid = item.get("publisher_id", "") or item.get("author_id", "")
        if name or aid:
            entities.append({
                "name": name,
                "agent_id": aid,
                "context": "feed author",
            })

    return entities


# ── Main ───────────────────────────────────────────────────────────

def main():
    raw = sys.stdin.read().strip()
    if not raw:
        sys.exit(0)

    # Try parsing as a single JSON object, or as JSON lines
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try JSON lines (messages format)
        lines = raw.strip().splitlines()
        messages = []
        for line in lines:
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if not messages:
            # Pass through unparseable input
            print(raw)
            sys.exit(0)
        data = {"messages": messages}

    # Friend-request prompts run without personal memory because their greeting
    # is untrusted. Expose only the one owner-controlled policy bit they need;
    # never co-locate the full structured-facts file with external text.
    if isinstance(data, dict) and "requests" in data:
        data["friend_policy"] = {
            "temporary_active": temporary_friend_policy_active(MEMORY_DIR),
        }

    # Load contacts
    file_contacts = load_contacts()
    team_contacts = parse_team_md()
    all_contacts = build_lookup(file_contacts + team_contacts)

    if not all_contacts:
        # No contacts to match against, pass through
        print(json.dumps(data, ensure_ascii=False))
        sys.exit(0)

    # Extract and resolve entities
    entities = extract_entities(data)
    matches = []
    for entity in entities:
        result = match_entity(
            entity.get("name", ""),
            entity.get("agent_id", ""),
            all_contacts,
        )
        if result:
            result["original_name"] = entity.get("name", "")
            result["original_agent_id"] = entity.get("agent_id", "")
            matches.append(result)

    # Append matches to output
    if matches:
        data["entity_matches"] = matches

    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
