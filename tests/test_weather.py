"""Tests for core.weather — Amap weather, cache TTL, context line (REQ-113).

HTTP is mocked at core.geo._http_get_json (weather routes through
geo._amap_get). Fixtures are fully synthetic — no real personal locations.
JARVIS_DIR is redirected to tmp_path so the production data/weather_cache.json
is never touched.
"""

import json
import time
from pathlib import Path

import pytest
import yaml

from core import geo, weather


# ── fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def jarvis_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DIR", str(tmp_path))
    monkeypatch.delenv("AMAP_KEY", raising=False)
    return tmp_path


def _write_config(jarvis_dir: Path, geo_section: dict):
    (jarvis_dir / "jarvis.yaml").write_text(
        yaml.safe_dump({"data_dir": str(jarvis_dir / ".jarvis"),
                        "geo": geo_section}, allow_unicode=True),
        encoding="utf-8")


@pytest.fixture
def configured(jarvis_dir):
    _write_config(jarvis_dir, {"amap_key": "test-key-000",
                               "default_adcode": "888800"})
    return jarvis_dir


def _live_body(weather_txt="多云", temp="28"):
    return {
        "status": "1", "info": "OK",
        "lives": [{"province": "示例省", "city": "示例市", "adcode": "888800",
                   "weather": weather_txt, "temperature": temp,
                   "humidity": "60", "winddirection": "东南", "windpower": "≤3",
                   "reporttime": "2026-07-21 10:00:00"}],
    }


def _forecast_body(day="多云", night="多云", daytemp="33", nighttemp="26"):
    return {
        "status": "1", "info": "OK",
        "forecasts": [{
            "city": "示例市", "adcode": "888800",
            "casts": [
                {"date": "2026-07-21", "dayweather": day,
                 "nightweather": night, "daytemp": daytemp,
                 "nighttemp": nighttemp},
                {"date": "2026-07-22", "dayweather": "晴",
                 "nightweather": "晴", "daytemp": "34", "nighttemp": "27"},
            ],
        }],
    }


def _mock_weather_http(monkeypatch, live=None, forecast=None):
    """Route extensions=base → live body, extensions=all → forecast body.
    Returns the list of called URLs."""
    live = live if live is not None else _live_body()
    forecast = forecast if forecast is not None else _forecast_body()
    calls = []

    def fake(url):
        calls.append(url)
        assert "/weather/weatherInfo" in url, f"unexpected URL: {url}"
        return forecast if "extensions=all" in url else live

    monkeypatch.setattr(geo, "_http_get_json", fake)
    return calls


# ── get_weather ─────────────────────────────────────────────────────

def test_get_weather_parsing(configured, monkeypatch):
    _mock_weather_http(monkeypatch)
    w = weather.get_weather("888800")
    assert w["adcode"] == "888800"
    assert w["city"] == "示例市"
    assert w["live"]["weather"] == "多云"
    assert w["live"]["temperature"] == "28"
    assert len(w["forecast"]) == 2
    assert w["forecast"][0]["daytemp"] == "33"
    assert w["fetched_at"] > 0


def test_get_weather_no_key(jarvis_dir):
    w = weather.get_weather("888800")
    assert "error" in w and "API key" in w["error"]


def test_get_weather_no_adcode_anywhere(jarvis_dir, monkeypatch):
    _write_config(jarvis_dir, {"amap_key": "test-key-000"})  # no default_adcode
    w = weather.get_weather()
    assert "error" in w and "adcode" in w["error"]


def test_adcode_resolution_prefers_anchor(configured, monkeypatch):
    # first anchor with an adcode beats geo.default_adcode
    geo.set_anchor("home", 30.0, 120.0, adcode="777700")
    assert weather.resolve_adcode() == "777700"
    # explicit arg beats everything
    assert weather.resolve_adcode("666600") == "666600"


def test_adcode_resolution_falls_back_to_config_default(configured):
    assert weather.resolve_adcode() == "888800"


# ── cache TTL ───────────────────────────────────────────────────────

def test_cache_second_call_within_ttl_skips_http(configured, monkeypatch):
    calls = _mock_weather_http(monkeypatch)
    w1 = weather.get_weather("888800")
    assert len(calls) == 2          # base + all
    w2 = weather.get_weather("888800")
    assert len(calls) == 2          # served from cache — no new HTTP
    assert w2["fetched_at"] == w1["fetched_at"]
    cache_file = configured / "data" / "weather_cache.json"
    assert cache_file.exists()


def test_cache_expired_entry_refetches(configured, monkeypatch):
    calls = _mock_weather_http(monkeypatch)
    weather.get_weather("888800")
    assert len(calls) == 2
    # age the cache entry past the 30min TTL
    cache_file = configured / "data" / "weather_cache.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8"))
    cache["888800"]["fetched_at"] = time.time() - weather.CACHE_TTL_S - 1
    cache_file.write_text(json.dumps(cache), encoding="utf-8")
    weather.get_weather("888800")
    assert len(calls) == 4          # refetched


def test_cache_is_per_adcode(configured, monkeypatch):
    calls = _mock_weather_http(monkeypatch)
    weather.get_weather("888800")
    weather.get_weather("777700")   # different city → its own fetch
    assert len(calls) == 4


def test_cache_force_bypasses(configured, monkeypatch):
    calls = _mock_weather_http(monkeypatch)
    weather.get_weather("888800")
    weather.get_weather("888800", force=True)
    assert len(calls) == 4


def test_stale_cache_served_on_total_http_failure(configured, monkeypatch):
    _mock_weather_http(monkeypatch)
    w1 = weather.get_weather("888800")
    # expire the entry, then make HTTP fail entirely
    cache_file = configured / "data" / "weather_cache.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8"))
    cache["888800"]["fetched_at"] = time.time() - weather.CACHE_TTL_S - 1
    cache_file.write_text(json.dumps(cache), encoding="utf-8")
    monkeypatch.setattr(geo, "_http_get_json",
                        lambda url: {"error": "Amap request failed: boom"})
    w2 = weather.get_weather("888800")
    assert "error" not in w2 and w2["city"] == w1["city"]


def test_corrupt_cache_file_ignored(configured, monkeypatch):
    cache_file = configured / "data" / "weather_cache.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text("{corrupt", encoding="utf-8")
    calls = _mock_weather_http(monkeypatch)
    w = weather.get_weather("888800")
    assert "error" not in w and len(calls) == 2


# ── context line ────────────────────────────────────────────────────

def test_context_line_basic(configured, monkeypatch):
    _mock_weather_http(monkeypatch)
    line = weather.weather_context_line()
    assert line.startswith("今日示例市：")
    assert "多云" in line and "26-33°C" in line
    assert "\n" not in line          # one-liner contract
    assert "降水" not in line        # no rain → no hint


def test_context_line_rain_hint(configured, monkeypatch):
    _mock_weather_http(
        monkeypatch,
        forecast=_forecast_body(day="多云", night="雷阵雨"))
    line = weather.weather_context_line()
    assert "多云转雷阵雨" in line
    assert "降水" in line            # outdoor-activity hint present
    assert "游泳" in line or "户外" in line


def test_context_line_snow_counts_as_precipitation(configured, monkeypatch):
    _mock_weather_http(
        monkeypatch,
        forecast=_forecast_body(day="小雪", night="小雪",
                                daytemp="2", nighttemp="-3"))
    line = weather.weather_context_line()
    assert "降水" in line
    assert "-3-2°C" in line          # negative temps sort correctly


def test_context_line_no_key_returns_empty(jarvis_dir):
    assert weather.weather_context_line() == ""


def test_context_line_fetch_failure_returns_empty(configured, monkeypatch):
    monkeypatch.setattr(geo, "_http_get_json",
                        lambda url: {"error": "Amap request failed: boom"})
    assert weather.weather_context_line() == ""


def test_context_line_live_only_fallback(configured, monkeypatch):
    # forecast endpoint empty → falls back to live conditions
    _mock_weather_http(
        monkeypatch,
        live=_live_body("小雨", "22"),
        forecast={"status": "1", "info": "OK", "forecasts": []})
    line = weather.weather_context_line()
    assert "小雨" in line and "22°C" in line
    assert "户外" in line            # live rain also hints


# ── CLI ─────────────────────────────────────────────────────────────

def test_cli_context_no_key_empty_output_exit_zero(jarvis_dir, capsys):
    """The pre-script contract: unconfigured → print NOTHING, exit 0."""
    rc = weather.main(["context"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cli_context_prints_line(configured, monkeypatch, capsys):
    _mock_weather_http(monkeypatch)
    assert weather.main(["context"]) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("今日示例市：")


def test_cli_get_json(configured, monkeypatch, capsys):
    _mock_weather_http(monkeypatch)
    assert weather.main(["get", "888800"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["adcode"] == "888800"


def test_cli_usage(jarvis_dir, capsys):
    assert weather.main([]) == 2
    assert weather.main(["frobnicate"]) == 2
