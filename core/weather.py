"""Amap weather (高德天气) — REQ-113.

Same API key as core.geo (geo.amap_key / AMAP_KEY env). Provides:

  - get_weather(adcode)      → live conditions + 3-4 day forecast
  - weather_context_line()   → short Chinese one-liner for prompt injection,
                               e.g. "今日示例市：多云 26-33°C". If rain/snow is
                               in today's forecast the line says so, so LLM
                               tasks can adjust outdoor suggestions (游泳/篮球).

adcode resolution for the context line: first configured anchor that has an
adcode → geo.default_adcode from jarvis.yaml → give up (empty line). No
hardcoded personal city — the fallback lives in per-user config only
(jarvis.example.yaml documents default_adcode).

Cache: data/weather_cache.json, TTL 30min, keyed by adcode — multiple
heartbeat tasks within one half-hour share one API call.

Every function degrades gracefully (error dict / empty string, never raises).

CLI:
    python3 -m core.weather context           # one-liner; EMPTY + exit 0
                                              # when no key configured
    python3 -m core.weather get [adcode]      # full JSON
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from core import geo

CACHE_TTL_S = 30 * 60


def _cache_file() -> Path:
    return geo._jarvis_dir() / "data" / "weather_cache.json"


def _read_cache() -> dict:
    f = _cache_file()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_cache(cache: dict) -> None:
    f = _cache_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass  # cache is an optimization — failure to persist must not break


def resolve_adcode(adcode: str = "") -> str:
    """adcode arg → first anchor with an adcode → geo.default_adcode → ''."""
    if adcode and str(adcode).strip():
        return str(adcode).strip()
    for info in geo.load_anchors().values():
        code = str(info.get("adcode") or "").strip() if isinstance(info, dict) else ""
        if code:
            return code
    return str(geo._geo_config().get("default_adcode") or "").strip()


def get_weather(adcode: str = "", force: bool = False) -> dict:
    """Live + forecast weather for adcode (resolved via resolve_adcode).
    → {adcode, city, live: {...}, forecast: [casts...], fetched_at}
    or {"error": ...}. Served from cache within CACHE_TTL_S."""
    if not geo.get_api_key():
        return {"error": geo.NO_KEY_ERROR}
    code = resolve_adcode(adcode)
    if not code:
        return {"error": "no adcode — configure an anchor with adcode or "
                         "geo.default_adcode in jarvis.yaml"}

    cache = _read_cache()
    entry = cache.get(code)
    if (not force and isinstance(entry, dict)
            and isinstance(entry.get("data"), dict)
            and time.time() - float(entry.get("fetched_at", 0)) < CACHE_TTL_S):
        return entry["data"]

    live_body = geo._amap_get("/weather/weatherInfo",
                              {"city": code, "extensions": "base"})
    fc_body = geo._amap_get("/weather/weatherInfo",
                            {"city": code, "extensions": "all"})
    if "error" in live_body and "error" in fc_body:
        # total failure — serve stale cache if we have one, else the error
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            return entry["data"]
        return live_body

    lives = live_body.get("lives") or []
    live = lives[0] if lives and isinstance(lives[0], dict) else {}
    forecasts = fc_body.get("forecasts") or []
    fc0 = forecasts[0] if forecasts and isinstance(forecasts[0], dict) else {}
    casts = fc0.get("casts") or []

    result = {
        "adcode": code,
        "city": live.get("city") or fc0.get("city") or "",
        "live": {
            "weather": live.get("weather", ""),
            "temperature": live.get("temperature", ""),
            "humidity": live.get("humidity", ""),
            "winddirection": live.get("winddirection", ""),
            "windpower": live.get("windpower", ""),
            "reporttime": live.get("reporttime", ""),
        },
        "forecast": [c for c in casts if isinstance(c, dict)],
        "fetched_at": time.time(),
    }
    cache[code] = {"fetched_at": result["fetched_at"], "data": result}
    _write_cache(cache)
    return result


_RAIN_MARKERS = ("雨", "雪", "冰雹")


def _has_precipitation(cast: dict) -> bool:
    text = str(cast.get("dayweather", "")) + str(cast.get("nightweather", ""))
    return any(m in text for m in _RAIN_MARKERS)


def weather_context_line(adcode: str = "") -> str:
    """One-line Chinese weather summary for prompt injection, e.g.
    "今日示例市：多云 26-33°C，白天晴夜间转雷阵雨——有降水，游泳/篮球等户外活动
    建议看时段安排". Returns "" when no key / no adcode / fetch failed —
    callers treat empty as "skip the weather block"."""
    if not geo.get_api_key():
        return ""
    w = get_weather(adcode)
    if "error" in w:
        return ""
    city = w.get("city", "")
    casts = w.get("forecast") or []
    today = casts[0] if casts else {}
    live = w.get("live") or {}

    parts = []
    day_w = str(today.get("dayweather", ""))
    night_w = str(today.get("nightweather", ""))
    day_t = str(today.get("daytemp", ""))
    night_t = str(today.get("nighttemp", ""))
    if day_w and day_t and night_t:
        wx = day_w if day_w == night_w else f"{day_w}转{night_w}"
        lo, hi = sorted([night_t, day_t], key=lambda t: float(t)
                        if t.lstrip("-").isdigit() else 0)
        parts.append(f"{wx} {lo}-{hi}°C")
    elif live.get("weather"):
        parts.append(f"{live['weather']} {live.get('temperature', '?')}°C"
                     if live.get("temperature") else live["weather"])
    if not parts:
        return ""

    line = f"今日{city}：{'，'.join(parts)}" if city else f"今日：{'，'.join(parts)}"
    if today and _has_precipitation(today):
        line += "——今天有降水，游泳/篮球等户外活动建议改期或留意时段"
    elif live.get("weather") and any(m in live["weather"]
                                     for m in _RAIN_MARKERS):
        line += "——当前有降水，户外活动注意"
    return line


# ── CLI ─────────────────────────────────────────────────────────────

USAGE = """usage: python3 -m core.weather <subcommand>
  context [adcode]   # one-line Chinese summary; empty output + exit 0 when unconfigured
  get [adcode]       # full weather JSON"""


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "context":
            line = weather_context_line(args[0] if args else "")
            if line:
                print(line)
            return 0  # empty = not configured / unavailable — NOT an error
        if cmd == "get":
            w = get_weather(args[0] if args else "")
            print(json.dumps(w, ensure_ascii=False, indent=2))
            return 1 if "error" in w else 0
    except Exception as e:  # CLI must never traceback at heartbeat callers
        print(json.dumps({"error": f"{type(e).__name__}: {e}"},
                         ensure_ascii=False))
        return 1
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
