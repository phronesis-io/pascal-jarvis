#!/usr/bin/env python3
"""Post-hook: update EigenFlux profile if Claude decided to."""
import sys
import json
import os
import re

JARVIS_DIR = os.environ.get("JARVIS_DIR", os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, JARVIS_DIR)
from plugins.eigenflux.client import EigenFluxClient

raw = sys.stdin.read().strip()
if not raw or "HEARTBEAT_OK" in raw:
    sys.exit(0)

raw = re.sub(r'^```json?\s*', '', raw)
raw = re.sub(r'```\s*$', '', raw)

try:
    data = json.loads(raw)
except json.JSONDecodeError:
    sys.exit(0)

if not data.get("should_update"):
    sys.exit(0)

agent_name = data.get("agent_name")
bio = data.get("bio")
if not agent_name and not bio:
    sys.exit(0)

client = EigenFluxClient(os.path.join(JARVIS_DIR, "eigenflux"))
result = client.update_profile(agent_name=agent_name, bio=bio)
if result.get("code") == 0:
    print(f"EigenFlux profile updated. {data.get('reason', '')}")
else:
    print(f"[profile] Update failed: {result.get('msg')}", file=sys.stderr)
