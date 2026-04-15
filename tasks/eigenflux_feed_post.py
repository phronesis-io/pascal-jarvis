#!/usr/bin/env python3
"""Post-hook: submit feedback to EigenFlux, output user message."""
import sys
import json
import os
import re

JARVIS_DIR = os.environ.get("JARVIS_DIR", os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, JARVIS_DIR)
from plugins.eigenflux.client import EigenFluxClient

raw = sys.stdin.read().strip()
raw = re.sub(r'^```json?\s*', '', raw)
raw = re.sub(r'```\s*$', '', raw)

try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(0)

# Submit feedback scores
fb = data.get("feedback", [])
if fb:
    client = EigenFluxClient(os.path.join(JARVIS_DIR, "eigenflux"))
    items = []
    for i in fb:
        try:
            items.append({"item_id": int(i["item_id"]), "score": i["score"]})
        except (ValueError, KeyError):
            pass
    if items:
        client.submit_feedback(items)
        print(f"[feedback: {len(items)} items scored]", file=sys.stderr)

# Output user message
msg = data.get("user_message", "")
if msg.strip():
    print(msg)
