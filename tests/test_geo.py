"""Tests for core.geo — Amap client, anchors, graceful degradation (REQ-112).

All HTTP is mocked at the single choke point core.geo._http_get_json.
All fixtures are FULLY SYNTHETIC (个人数据=配置原则): no real addresses or
coordinates of any actual person. JARVIS_DIR is redirected to tmp_path so
tests never touch the production data/ directory.
"""

import json
from pathlib import Path

import pytest
import yaml

from core import geo


# ── fixtures ────────────────────────────────────────────────────────

@pytest.fixture
def jarvis_dir(tmp_path, monkeypatch):
    """Isolated repo root: JARVIS_DIR → tmp, no AMAP_KEY env leakage."""
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
    """jarvis.yaml with a synthetic key + one yaml anchor."""
    _write_config(jarvis_dir, {
        "amap_key": "test-key-000",
        "default_adcode": "888800",
        "anchors": {
            "yaml_home": {"address": "示例市合成路1号",
                          "lat": 30.0, "lng": 120.0, "adcode": "888800"},
        },
    })
    return jarvis_dir


def _mock_http(monkeypatch, responses):
    """Patch _http_get_json; `responses` maps a URL substring → body (or a
    callable url→body). Records called URLs in the returned list."""
    calls = []

    def fake(url):
        calls.append(url)
        for frag, body in responses.items():
            if frag in url:
                return body(url) if callable(body) else body
        raise AssertionError(f"unexpected URL in test: {url}")

    monkeypatch.setattr(geo, "_http_get_json", fake)
    return calls


# ── no-key graceful degradation ─────────────────────────────────────

def test_no_key_geocode_returns_error_not_crash(jarvis_dir):
    result = geo.geocode("示例市合成路1号")
    assert "error" in result
    assert "API key" in result["error"]


def test_no_key_all_functions_degrade(jarvis_dir):
    assert "error" in geo.regeo(30.0, 120.0)
    assert "error" in geo.around_poi(30.0, 120.0, "游泳馆")
    assert "error" in geo.route((30.0, 120.0), (30.1, 120.1))
    # anchors still work without a key (no HTTP involved)
    assert geo.load_anchors() == {}


def test_env_key_overrides_yaml(configured, monkeypatch):
    assert geo.get_api_key() == "test-key-000"
    monkeypatch.setenv("AMAP_KEY", "env-key-999")
    assert geo.get_api_key() == "env-key-999"


# ── geocode / regeo parsing ─────────────────────────────────────────

GEOCODE_OK = {
    "status": "1", "info": "OK", "infocode": "10000", "count": "1",
    "geocodes": [{
        "formatted_address": "示例省示例市示例区合成路1号",
        "adcode": "888800",
        "location": "120.123456,30.654321",   # Amap order: lng,lat
    }],
}


def test_geocode_parsing(configured, monkeypatch):
    _mock_http(monkeypatch, {"/geocode/geo": GEOCODE_OK})
    r = geo.geocode("合成路1号")
    assert r == {
        "lat": 30.654321, "lng": 120.123456,
        "formatted_address": "示例省示例市示例区合成路1号",
        "adcode": "888800",
    }


def test_geocode_sends_key_and_address(configured, monkeypatch):
    calls = _mock_http(monkeypatch, {"/geocode/geo": GEOCODE_OK})
    geo.geocode("合成路1号")
    assert len(calls) == 1
    assert "key=test-key-000" in calls[0]


def test_geocode_api_error_status(configured, monkeypatch):
    _mock_http(monkeypatch, {"/geocode/geo": {
        "status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"}})
    r = geo.geocode("合成路1号")
    assert "error" in r and "INVALID_USER_KEY" in r["error"]


def test_geocode_no_results(configured, monkeypatch):
    _mock_http(monkeypatch, {"/geocode/geo": {
        "status": "1", "info": "OK", "geocodes": []}})
    assert "error" in geo.geocode("不存在的地方xyz")


def test_geocode_http_failure(configured, monkeypatch):
    _mock_http(monkeypatch, {"/geocode/geo": {"error": "HTTP 500 from Amap"}})
    assert "error" in geo.geocode("合成路1号")


def test_regeo_parsing(configured, monkeypatch):
    _mock_http(monkeypatch, {"/geocode/regeo": {
        "status": "1", "info": "OK",
        "regeocode": {
            "formatted_address": "示例省示例市示例区合成街道",
            "addressComponent": {
                "province": "示例省",
                "city": [],          # Amap quirk: empty fields come back as []
                "district": "示例区",
                "adcode": "888801",
            },
        },
    }})
    r = geo.regeo(30.0, 120.0)
    assert r["formatted_address"] == "示例省示例市示例区合成街道"
    assert r["adcode"] == "888801"
    assert r["city"] == "示例省"     # [] city falls back to province
    assert r["district"] == "示例区"


# ── around POI ──────────────────────────────────────────────────────

def test_around_poi_parsing(configured, monkeypatch):
    _mock_http(monkeypatch, {"/place/around": {
        "status": "1", "info": "OK",
        "pois": [
            {"name": "合成游泳馆", "distance": "350",
             "address": "合成路2号", "type": "体育休闲服务",
             "location": "120.130000,30.660000"},
            {"name": "示例篮球公园", "distance": "820.5",
             "address": [], "type": "体育休闲服务",
             "location": "120.140000,30.670000"},
        ],
    }})
    pois = geo.around_poi(30.654321, 120.123456, "游泳馆", 1000)
    assert isinstance(pois, list) and len(pois) == 2
    assert pois[0]["name"] == "合成游泳馆"
    assert pois[0]["distance_m"] == 350
    assert pois[0]["lat"] == 30.66
    assert pois[1]["distance_m"] == 820
    assert pois[1]["address"] == ""   # [] address normalized to ""


def test_around_poi_sends_lnglat_order(configured, monkeypatch):
    calls = _mock_http(monkeypatch, {"/place/around": {
        "status": "1", "pois": []}})
    geo.around_poi(30.5, 120.5)
    # Amap wants location=lng,lat
    assert "location=120.500000%2C30.500000" in calls[0]


# ── routing ─────────────────────────────────────────────────────────

DRIVING_OK = {
    "status": "1", "info": "OK",
    "route": {"paths": [{"distance": "12345", "duration": "1800"}]},
}


def test_route_driving_parsing(configured, monkeypatch):
    _mock_http(monkeypatch, {"/direction/driving": DRIVING_OK})
    r = geo.route((30.0, 120.0), (30.1, 120.1), mode="driving")
    assert r["distance_m"] == 12345
    assert r["duration_s"] == 1800
    assert "12.3km" in r["summary"] and "30分钟" in r["summary"]
    assert "驾车" in r["summary"]


def test_route_walking_short_distance_in_meters(configured, monkeypatch):
    _mock_http(monkeypatch, {"/direction/walking": {
        "status": "1",
        "route": {"paths": [{"distance": "800", "duration": "600"}]}}})
    r = geo.route("30.0,120.0", "30.01,120.01", mode="walking")
    assert r["distance_m"] == 800
    assert "800m" in r["summary"] and "步行" in r["summary"]


def test_route_transit_parsing_and_city_param(configured, monkeypatch):
    calls = _mock_http(monkeypatch, {"/direction/transit/integrated": {
        "status": "1",
        "route": {"distance": "15000",
                  "transits": [{"duration": "2400", "distance": "14000"}]}}})
    r = geo.route((30.0, 120.0), (30.2, 120.2), mode="transit")
    assert r["distance_m"] == 14000
    assert r["duration_s"] == 2400
    assert "公交" in r["summary"]
    # transit requires city — must fall back to geo.default_adcode
    assert "city=888800" in calls[0]


def test_route_unknown_mode(configured):
    r = geo.route((30.0, 120.0), (30.1, 120.1), mode="teleport")
    assert "error" in r


def test_route_no_paths(configured, monkeypatch):
    _mock_http(monkeypatch, {"/direction/driving": {
        "status": "1", "route": {"paths": []}}})
    assert "error" in geo.route((30.0, 120.0), (30.1, 120.1))


# ── anchors: set / load / override order ────────────────────────────

def test_set_anchor_persists_to_json_not_yaml(configured):
    r = geo.set_anchor("gym", 30.2, 120.2, address="合成健身房", adcode="888800")
    assert "error" not in r
    f = configured / "data" / "geo_anchors.json"
    assert f.exists()
    stored = json.loads(f.read_text(encoding="utf-8"))
    assert stored["gym"]["lat"] == 30.2
    # jarvis.yaml untouched
    cfg = yaml.safe_load((configured / "jarvis.yaml").read_text())
    assert "gym" not in cfg["geo"]["anchors"]


def test_load_anchors_json_overrides_yaml(configured):
    anchors = geo.load_anchors()
    assert anchors["yaml_home"]["lat"] == 30.0   # from yaml
    # json override for the SAME name wins
    geo.set_anchor("yaml_home", 31.5, 121.5, address="搬家后的合成地址")
    anchors = geo.load_anchors()
    assert anchors["yaml_home"]["lat"] == 31.5
    assert anchors["yaml_home"]["address"] == "搬家后的合成地址"
    # yaml-only anchor list still merged with json-only anchors
    geo.set_anchor("office", 30.9, 120.9)
    anchors = geo.load_anchors()
    assert set(anchors) == {"yaml_home", "office"}


def test_set_anchor_keeps_old_adcode_when_omitted(configured):
    geo.set_anchor("spot", 30.0, 120.0, adcode="888800")
    geo.set_anchor("spot", 30.5, 120.5)   # update coords, omit adcode
    anchors = geo.load_anchors()
    assert anchors["spot"]["lat"] == 30.5
    assert anchors["spot"]["adcode"] == "888800"


def test_set_anchor_invalid(configured):
    assert "error" in geo.set_anchor("", 30.0, 120.0)
    assert "error" in geo.set_anchor("bad", "not-a-number", 120.0)


def test_load_anchors_corrupt_json_ignored(configured):
    f = configured / "data" / "geo_anchors.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{corrupt", encoding="utf-8")
    anchors = geo.load_anchors()   # must not raise; yaml anchors survive
    assert "yaml_home" in anchors


def test_load_anchors_no_config_no_file(jarvis_dir):
    assert geo.load_anchors() == {}


# ── distance_from_anchor ────────────────────────────────────────────

def test_distance_from_anchor_with_address(configured, monkeypatch):
    _mock_http(monkeypatch, {
        "/geocode/geo": GEOCODE_OK,
        "/direction/driving": DRIVING_OK,
    })
    r = geo.distance_from_anchor("yaml_home", "示例区合成路1号")
    assert r["distance_m"] == 12345
    assert r["anchor"] == "yaml_home"
    assert r["dest"] == "示例省示例市示例区合成路1号"


def test_distance_from_anchor_with_latlng_skips_geocode(configured, monkeypatch):
    calls = _mock_http(monkeypatch, {"/direction/driving": DRIVING_OK})
    r = geo.distance_from_anchor("yaml_home", "30.9,120.9")
    assert r["distance_m"] == 12345
    assert all("/geocode/geo" not in u for u in calls)


def test_distance_from_unknown_anchor(configured):
    r = geo.distance_from_anchor("nonexistent", "30.9,120.9")
    assert "error" in r and "nonexistent" in r["error"]


# ── CLI ─────────────────────────────────────────────────────────────

def test_cli_anchor_set_and_list(configured, capsys):
    assert geo.main(["anchor-set", "cafe", "30.3", "120.3", "合成咖啡馆"]) == 0
    capsys.readouterr()
    assert geo.main(["anchor-list"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["cafe"]["address"] == "合成咖啡馆"


def test_cli_geocode_no_key_reports_error_json(jarvis_dir, capsys):
    rc = geo.main(["geocode", "合成路1号"])
    assert rc == 1   # error surfaced as exit code, but no traceback
    out = json.loads(capsys.readouterr().out)
    assert "error" in out


def test_cli_usage_on_bad_args(jarvis_dir, capsys):
    assert geo.main([]) == 2
    assert geo.main(["geocode"]) == 2
    assert geo.main(["frobnicate"]) == 2
