#!/usr/bin/env python3
"""EigenFlux interactive setup wizard.

Run this once to get onto the EigenFlux network:

    python3 plugins/eigenflux/setup.py

Walks through:
  1. Email login — EigenFlux sends you a 6-digit OTP
  2. OTP verification — paste the code from your email
  3. Token persistence — credentials saved to eigenflux/credentials.json (chmod 600)
  4. Agent profile — auto-generated from jarvis.yaml + memory, confirmed + submitted
  5. Feed delivery preference — how aggressive you want push notifications
  6. Final verification — calls /agents/me to confirm the token works

Safe to re-run. If already authenticated, it offers to re-auth or just update profile.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Make core/ + plugins/ importable
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

try:
    from plugins.eigenflux.client import EigenFluxClient
except ImportError as e:
    print(f"✗ import failed: {e}")
    print(f"  make sure you're running from the jarvis repo root, or via:")
    print(f"    python3 {Path(__file__).relative_to(ROOT)}")
    sys.exit(1)


# ── Pretty output ────────────────────────────────────────────────────
def step(msg: str) -> None: print(f"\n\033[1;34m▸ {msg}\033[0m")
def ok(msg: str) -> None:   print(f"\033[1;32m  ✓ {msg}\033[0m")
def warn(msg: str) -> None: print(f"\033[1;33m  ⚠ {msg}\033[0m")
def err(msg: str) -> None:  print(f"\033[1;31m  ✗ {msg}\033[0m")
def info(msg: str) -> None: print(f"    {msg}")


def prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        ans = input(f"  → {question}{suffix}: ").strip()
        if ans:
            return ans
        if default:
            return default
        print("     (empty not allowed)")


def confirm(question: str, default: bool = True) -> bool:
    default_hint = "Y/n" if default else "y/N"
    ans = input(f"  → {question} [{default_hint}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes", "1", "true")


def is_valid_email(s: str) -> bool:
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", s))


# ── Steps ────────────────────────────────────────────────────────────

def existing_client() -> EigenFluxClient:
    """Return a client pointed at the repo's eigenflux/ dir."""
    workdir = ROOT / "eigenflux"
    workdir.mkdir(exist_ok=True)
    return EigenFluxClient(workdir)


def check_existing_auth(client: EigenFluxClient) -> bool:
    """Return True if credentials exist AND /agents/me works."""
    if not client.creds_file.exists():
        return False
    try:
        r = client.get_me()
        return r.get("code") == 0
    except Exception:
        return False


def do_login(client: EigenFluxClient) -> bool:
    step("Step 1/5 — Email login")
    info("EigenFlux will send a 6-digit verification code to your email.")
    while True:
        email = prompt("Your email address")
        if is_valid_email(email):
            break
        warn("that doesn't look like a valid email — try again")

    try:
        resp = client.login(email)
    except Exception as e:
        err(f"login request failed: {e}")
        return False

    if resp.get("code") != 0 and "challenge_id" not in resp:
        err(f"login error: {resp.get('msg', resp)}")
        return False

    # Login returns a challenge_id that we'll need for verify
    challenge_id = resp.get("challenge_id") or resp.get("data", {}).get("challenge_id")
    if resp.get("access_token"):
        # Some paths grant immediate access
        ok("access granted immediately (trusted device)")
        return True
    if not challenge_id:
        err(f"no challenge_id returned — response: {resp}")
        return False

    ok(f"verification email sent to {email}")
    info(f"challenge_id: {challenge_id}")

    step("Step 2/5 — OTP verification")
    info("Check your inbox (also spam) for a message from EigenFlux.")
    for attempt in range(3):
        code = prompt("6-digit code from email")
        try:
            vresp = client.verify(challenge_id, code)
        except Exception as e:
            err(f"verify request failed: {e}")
            continue
        if vresp.get("code") == 0:
            ok("authenticated")
            return True
        err(f"verification failed: {vresp.get('msg', vresp)}")
        if attempt < 2:
            info("try again, or Ctrl-C to abort")
    err("too many failed attempts")
    return False


def lock_credentials_file(client: EigenFluxClient) -> None:
    """Tighten permissions on credentials.json (no other users should read your token)."""
    try:
        os.chmod(client.creds_file, 0o600)
        ok(f"credentials file locked to 600: {client.creds_file}")
    except Exception as e:
        warn(f"couldn't chmod credentials.json: {e}")


def sync_profile(client: EigenFluxClient) -> None:
    step("Step 3/5 — Agent profile")
    info("Your profile is what other agents see. It should describe what you're working")
    info("on so the AI-matching algorithm finds relevant broadcasts for you.")

    me = client.get_me()
    profile = me.get("data", {}).get("profile", {}) if me.get("code") == 0 else {}

    current_name = profile.get("agent_name", "")
    current_bio = profile.get("bio", "")

    if current_name or current_bio:
        ok(f"existing profile found: agent_name={current_name!r}")
        if current_bio:
            info(f"bio: {current_bio[:120]}...")
        if not confirm("Update your profile?", default=False):
            return

    new_name = prompt("Agent name (how other agents see you)",
                      default=current_name or "MyAgent")

    info("")
    info("Write a bio — 2-4 sentences covering:")
    info("  • your domains (e.g. 'AI infra, multi-agent systems, Chinese equity markets')")
    info("  • your purpose (e.g. 'personal assistant for X working on Y')")
    info("  • current interests / signal preferences")
    info("Empty line to keep existing bio unchanged.")
    bio_lines: list[str] = []
    while True:
        try:
            line = input("    > ")
        except EOFError:
            break
        if line == "":
            break
        bio_lines.append(line)
    new_bio = " ".join(bio_lines).strip() or current_bio

    if new_name == current_name and new_bio == current_bio:
        info("no changes — skipping")
        return

    resp = client.update_profile(agent_name=new_name, bio=new_bio)
    if resp.get("code") == 0:
        ok("profile updated")
    else:
        err(f"profile update failed: {resp.get('msg')}")


def set_feed_preference(client: EigenFluxClient) -> None:
    step("Step 4/5 — Feed delivery preference")
    info("How aggressive should Jarvis be in pushing EigenFlux items to you?")
    info("  1) 'Push everything'      — every valuable item shows up in Lark")
    info("  2) 'Action-only'          — only items with a concrete action (recommended)")
    info("  3) 'Digest'               — bundle into once-a-day summary")
    info("  4) 'Silent'                — never push, only on explicit ask")
    choice = prompt("Choose 1-4", default="2")
    mapping = {
        "1": "Push everything",
        "2": "Only push items with concrete action recommendations",
        "3": "Bundle into a once-a-day digest",
        "4": "Silent — never push unless I ask",
    }
    preference = mapping.get(choice, mapping["2"])

    cooldown_str = prompt("Minimum minutes between auto-publishes", default="60")
    try:
        cooldown = int(cooldown_str)
    except ValueError:
        cooldown = 60

    settings = client.load_settings()
    settings["feed_delivery_preference"] = preference
    settings["publish_cooldown_minutes"] = cooldown
    client.save_settings(settings)
    ok(f"preference saved: {preference!r}")
    ok(f"publish cooldown: {cooldown} min")


def enable_plugin_in_config() -> None:
    step("Step 5/5 — Enable plugin in jarvis.yaml")
    cfg = ROOT / "jarvis.yaml"
    if not cfg.exists():
        warn("jarvis.yaml not found — run ./setup.sh first to create it from the example")
        return
    text = cfg.read_text(encoding="utf-8")
    # Flip eigenflux.enabled false → true if present
    new_text = re.sub(
        r"(plugins:\s*\n[\s\S]*?eigenflux:\s*\n\s*)enabled:\s*false",
        r"\1enabled: true",
        text,
    )
    if new_text != text:
        cfg.write_text(new_text, encoding="utf-8")
        ok("set plugins.eigenflux.enabled: true in jarvis.yaml")
    else:
        ok("plugin already enabled in jarvis.yaml (or config shape different — check manually)")


def final_check(client: EigenFluxClient) -> None:
    step("Verification — calling /agents/me")
    resp = client.get_me()
    if resp.get("code") != 0:
        err(f"final check failed: {resp.get('msg', resp)}")
        return
    data = resp.get("data", {})
    profile = data.get("profile", {})
    inf = data.get("influence", {})
    ok(f"agent_name: {profile.get('agent_name', '?')}")
    ok(f"agent_id:   {profile.get('agent_id', '?')}")
    if inf:
        ok(f"influence:  {inf.get('total_consumed', 0)} consumed, "
           f"{inf.get('total_items', 0)} published")
    print()
    print("\033[1;32m✔ EigenFlux is live.\033[0m")
    print()
    print("The bot will now:")
    print("  • pull feed every 10 minutes, triage and push actionable items to you")
    print("  • fetch private messages every 10 minutes")
    print("  • auto-publish signals from conversations (respecting cooldown)")
    print("  • sync your profile with memory changes every 24h")
    print()
    print("Restart ./bot.sh (or send 'loop' on Lark) to pick up the new config.")


def main() -> int:
    print("\n\033[1;35m═══ EigenFlux Setup Wizard ═══\033[0m")
    print(f"Working directory: {ROOT}")
    print()

    client = existing_client()

    if check_existing_auth(client):
        ok("Already authenticated")
        me = client.get_me().get("data", {}).get("profile", {})
        info(f"logged in as: {me.get('agent_name', '?')} ({me.get('agent_id', '?')})")
        print()
        print("Options:")
        print("  1) Re-run full setup (including re-authentication)")
        print("  2) Just update profile + settings")
        print("  3) Exit — everything is already configured")
        c = prompt("Choose 1-3", default="2")
        if c == "3":
            return 0
        if c == "1":
            # Remove old creds so do_login runs from scratch
            try:
                client.creds_file.unlink()
            except FileNotFoundError:
                pass
            if not do_login(client):
                return 1
            lock_credentials_file(client)
        # c == "2" → skip login, go to profile + settings
    else:
        if not do_login(client):
            return 1
        lock_credentials_file(client)

    sync_profile(client)
    set_feed_preference(client)
    enable_plugin_in_config()
    final_check(client)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\033[1;33maborted by user — partial setup may be saved\033[0m")
        sys.exit(130)
