#!/usr/bin/env python3
"""Post-hook: publish to EigenFlux if Claude decided to."""
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
    if data.get("should_publish"):
        client = EigenFluxClient(os.path.join(JARVIS_DIR, "eigenflux"))
        client.publish(data["content"], data["notes"])
        print("[published to EigenFlux]", file=sys.stderr)
except Exception:
    pass
