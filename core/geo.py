"""Amap (高德地图) Web 服务 API client — REQ-112.

Geocoding, reverse geocoding, POI-around search and routing, plus named
"anchors" (家/办公室…) so heartbeat tasks and bot sessions can answer
"离家多远 / 附近有什么" style questions.

Config (per-user, gitignored jarvis.yaml):

    geo:
      amap_key: "..."          # Amap Web 服务 API key (env AMAP_KEY overrides)
      default_adcode: "310000" # weather fallback adcode
      anchors:                 # optional seed anchors
        home: {address: "...", lat: 31.0, lng: 121.0, adcode: "310000"}

Anchors written at runtime go to data/geo_anchors.json (gitignored via the
root-anchored /data/ rule) and OVERRIDE same-named jarvis.yaml anchors —
we never rewrite jarvis.yaml programmatically.

个人数据=配置原则: no real addresses/coordinates in this file or its tests.

Every public function degrades gracefully: on missing key / HTTP failure /
API error it returns a dict with an "error" key — it never raises.

CLI (run from the repo root, style of `python3 -m core.components`):

    python3 -m core.geo geocode <address>
    python3 -m core.geo regeo <lat> <lng>
    python3 -m core.geo around <lat> <lng> [keywords] [radius_m]
    python3 -m core.geo route <origin> <dest> [driving|walking|transit]
                               # origin/dest = anchor name | "lat,lng" | address
    python3 -m core.geo anchor-set <name> <lat> <lng> [address] [adcode]
    python3 -m core.geo anchor-list
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

AMAP_BASE = "https://restapi.amap.com/v3"
HTTP_TIMEOUT = 15

NO_KEY_ERROR = ("no API key configured — set geo.amap_key in jarvis.yaml "
                "or the AMAP_KEY env var")


# ── paths / config ──────────────────────────────────────────────────

def _jarvis_dir() -> Path:
    """Repo root. JARVIS_DIR env wins (tests + heartbeat set it); read at
    call time, NOT import time — import-time binding polluted production
    files in past test runs (memorial.py lesson)."""
    return Path(os.environ.get("JARVIS_DIR",
                               Path(__file__).resolve().parents[1]))


def _anchors_file() -> Path:
    return _jarvis_dir() / "data" / "geo_anchors.json"


def _geo_config() -> dict:
    """The `geo:` section of jarvis.yaml. Missing/broken config → {}."""
    try:
        from core.config import Config
        cfg = Config(_jarvis_dir() / "jarvis.yaml")
        geo = cfg.get("geo")
        return geo if isinstance(geo, dict) else {}
    except Exception:
        return {}


def get_api_key() -> str:
    """AMAP_KEY env var overrides jarvis.yaml geo.amap_key. '' = unset."""
    env = os.environ.get("AMAP_KEY", "").strip()
    if env:
        return env
    return str(_geo_config().get("amap_key") or "").strip()


# ── HTTP ────────────────────────────────────────────────────────────

def _http_get_json(url: str) -> dict:
    """GET url, parse JSON. Errors → {"error": ...}; never raises.
    Single choke point so tests mock exactly one function."""
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-geo/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code} from Amap"}
    except Exception as e:
        return {"error": f"Amap request failed: {e}"}


def _amap_get(path: str, params: dict) -> dict:
    """Call an Amap v3 endpoint. Handles key injection and Amap's
    status=0 error envelope. Returns parsed body or {"error": ...}."""
    key = get_api_key()
    if not key:
        return {"error": NO_KEY_ERROR}
    q = {"key": key, "output": "json"}
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{AMAP_BASE}{path}?{urllib.parse.urlencode(q)}"
    body = _http_get_json(url)
    if "error" in body:
        return body
    if str(body.get("status", "")) != "1":
        return {"error": f"Amap API error: {body.get('info', 'unknown')} "
                         f"(infocode {body.get('infocode', '?')})"}
    return body


# ── coordinate helpers ──────────────────────────────────────────────

def _parse_latlng(value) -> tuple[float, float] | None:
    """(lat, lng) tuple/list or 'lat,lng' string → (lat, lng) floats.
    Anything unparseable → None."""
    try:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(value[0]), float(value[1])
        if isinstance(value, str) and value.count(",") == 1:
            a, b = value.split(",")
            return float(a.strip()), float(b.strip())
    except (TypeError, ValueError):
        pass
    return None


def _loc_param(lat: float, lng: float) -> str:
    """Amap wants 'lng,lat' (经度在前), max 6 decimals."""
    return f"{float(lng):.6f},{float(lat):.6f}"


def _split_amap_location(loc: str) -> tuple[float, float]:
    """Amap 'lng,lat' string → (lat, lng)."""
    lng_s, lat_s = loc.split(",")[:2]
    return float(lat_s), float(lng_s)


# ── geocoding ───────────────────────────────────────────────────────

def geocode(address: str) -> dict:
    """address → {lat, lng, formatted_address, adcode} or {"error": ...}."""
    if not address or not str(address).strip():
        return {"error": "empty address"}
    body = _amap_get("/geocode/geo", {"address": str(address).strip()})
    if "error" in body:
        return body
    geocodes = body.get("geocodes") or []
    if not geocodes:
        return {"error": f"no geocode result for {address!r}"}
    g = geocodes[0]
    try:
        lat, lng = _split_amap_location(g.get("location", ""))
    except (ValueError, AttributeError):
        return {"error": f"unparseable location in geocode result: "
                         f"{g.get('location')!r}"}
    return {
        "lat": lat,
        "lng": lng,
        "formatted_address": g.get("formatted_address", ""),
        "adcode": g.get("adcode", ""),
    }


def regeo(lat, lng) -> dict:
    """(lat, lng) → address info or {"error": ...}."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"error": f"invalid coordinates: {lat!r}, {lng!r}"}
    body = _amap_get("/geocode/regeo", {"location": _loc_param(lat, lng)})
    if "error" in body:
        return body
    rg = body.get("regeocode") or {}
    comp = rg.get("addressComponent") or {}
    # Amap quirk: single-value fields come back as [] when empty.
    def _s(v):
        return v if isinstance(v, str) else ""
    return {
        "formatted_address": _s(rg.get("formatted_address")),
        "province": _s(comp.get("province")),
        "city": _s(comp.get("city")) or _s(comp.get("province")),
        "district": _s(comp.get("district")),
        "adcode": _s(comp.get("adcode")),
    }


def around_poi(lat, lng, keywords: str = "", radius: int = 1000):
    """POIs around (lat, lng). Success → list of {name, distance_m, address,
    type, lat, lng}; failure → {"error": ...} dict."""
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"error": f"invalid coordinates: {lat!r}, {lng!r}"}
    body = _amap_get("/place/around", {
        "location": _loc_param(lat, lng),
        "keywords": keywords,
        "radius": int(radius),
        "sortrule": "distance",
        "offset": 20,
    })
    if "error" in body:
        return body
    pois = []
    for p in body.get("pois") or []:
        try:
            plat, plng = _split_amap_location(p.get("location", ""))
        except (ValueError, AttributeError):
            plat = plng = None
        try:
            dist = int(float(p.get("distance") or 0))
        except (TypeError, ValueError):
            dist = 0
        pois.append({
            "name": p.get("name", ""),
            "distance_m": dist,
            "address": p.get("address", "") if isinstance(p.get("address"), str) else "",
            "type": p.get("type", ""),
            "lat": plat,
            "lng": plng,
        })
    return pois


# ── routing ─────────────────────────────────────────────────────────

_ROUTE_PATHS = {
    "driving": "/direction/driving",
    "walking": "/direction/walking",
    "transit": "/direction/transit/integrated",
}


def route(origin_latlng, dest_latlng, mode: str = "driving",
          city: str = "") -> dict:
    """Route between two (lat, lng) points.
    → {distance_m, duration_s, summary, mode} or {"error": ...}.
    `city` (adcode) is required by Amap for transit; defaults to
    geo.default_adcode from config."""
    mode = (mode or "driving").strip().lower()
    if mode not in _ROUTE_PATHS:
        return {"error": f"unknown mode {mode!r} "
                         f"(use driving|transit|walking)"}
    o = _parse_latlng(origin_latlng)
    d = _parse_latlng(dest_latlng)
    if o is None or d is None:
        return {"error": f"invalid origin/dest: "
                         f"{origin_latlng!r} → {dest_latlng!r}"}
    params = {
        "origin": _loc_param(*o),
        "destination": _loc_param(*d),
    }
    if mode == "transit":
        params["city"] = city or str(_geo_config().get("default_adcode") or "")
        if not params["city"]:
            return {"error": "transit routing needs a city adcode — "
                             "set geo.default_adcode or pass city"}
    body = _amap_get(_ROUTE_PATHS[mode], params)
    if "error" in body:
        return body
    rt = body.get("route") or {}

    def _num(v, cast):
        try:
            return cast(float(v))
        except (TypeError, ValueError):
            return 0

    if mode == "transit":
        transits = rt.get("transits") or []
        if not transits:
            return {"error": "no transit route found"}
        t0 = transits[0]
        distance_m = _num(t0.get("distance") or rt.get("distance"), int)
        duration_s = _num(t0.get("duration"), int)
    else:
        paths = rt.get("paths") or []
        if not paths:
            return {"error": f"no {mode} route found"}
        p0 = paths[0]
        distance_m = _num(p0.get("distance"), int)
        duration_s = _num(p0.get("duration"), int)

    mode_cn = {"driving": "驾车", "transit": "公交", "walking": "步行"}[mode]
    if distance_m >= 1000:
        dist_txt = f"{distance_m / 1000:.1f}km"
    else:
        dist_txt = f"{distance_m}m"
    summary = f"{mode_cn} {dist_txt} 约{max(1, round(duration_s / 60))}分钟"
    return {"distance_m": distance_m, "duration_s": duration_s,
            "summary": summary, "mode": mode}


# ── anchors ─────────────────────────────────────────────────────────

def load_anchors() -> dict:
    """Named anchors, merged: jarvis.yaml geo.anchors ← data/geo_anchors.json
    (json wins per name). Always returns a dict; never raises."""
    anchors: dict = {}
    yaml_anchors = _geo_config().get("anchors")
    if isinstance(yaml_anchors, dict):
        for name, info in yaml_anchors.items():
            if isinstance(info, dict):
                anchors[str(name)] = dict(info)
    f = _anchors_file()
    if f.exists():
        try:
            file_anchors = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(file_anchors, dict):
                for name, info in file_anchors.items():
                    if isinstance(info, dict):
                        anchors[str(name)] = dict(info)
        except (OSError, json.JSONDecodeError):
            pass  # corrupt override file must not break anchor lookup
    return anchors


def set_anchor(name: str, lat, lng, address: str = "",
               adcode: str = "") -> dict:
    """Persist/update an anchor in data/geo_anchors.json (NOT jarvis.yaml).
    Returns the stored record or {"error": ...}."""
    name = str(name or "").strip()
    if not name:
        return {"error": "anchor name required"}
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return {"error": f"invalid coordinates: {lat!r}, {lng!r}"}
    record = {"lat": lat, "lng": lng, "address": str(address or ""),
              "adcode": str(adcode or "")}
    f = _anchors_file()
    existing: dict = {}
    if f.exists():
        try:
            loaded = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            pass
    # keep a previously-stored adcode/address if the update omits them
    old = existing.get(name)
    if isinstance(old, dict):
        if not record["adcode"] and old.get("adcode"):
            record["adcode"] = str(old["adcode"])
        if not record["address"] and old.get("address"):
            record["address"] = str(old["address"])
    existing[name] = record
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(existing, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(f)
    except OSError as e:
        return {"error": f"failed to write {f}: {e}"}
    return {"name": name, **record}


def distance_from_anchor(anchor_name: str, address_or_latlng,
                         mode: str = "driving") -> dict:
    """Convenience: route from a named anchor to an address or (lat, lng).
    → route() result + {anchor, dest} or {"error": ...}."""
    anchors = load_anchors()
    anchor = anchors.get(str(anchor_name or "").strip())
    if not isinstance(anchor, dict) or "lat" not in anchor or "lng" not in anchor:
        known = ", ".join(sorted(anchors)) or "(none configured)"
        return {"error": f"unknown anchor {anchor_name!r} — known: {known}"}

    dest = _parse_latlng(address_or_latlng)
    dest_desc = None
    if dest is None:
        g = geocode(str(address_or_latlng))
        if "error" in g:
            return g
        dest = (g["lat"], g["lng"])
        dest_desc = g.get("formatted_address") or str(address_or_latlng)
    r = route((anchor["lat"], anchor["lng"]), dest, mode=mode,
              city=str(anchor.get("adcode") or ""))
    if "error" in r:
        return r
    r["anchor"] = anchor_name
    r["dest"] = dest_desc or f"{dest[0]},{dest[1]}"
    return r


# ── CLI ─────────────────────────────────────────────────────────────

def _resolve_place(token: str) -> tuple[float, float] | dict:
    """CLI helper: anchor name | 'lat,lng' | free-text address → (lat, lng)
    or an {"error": ...} dict."""
    anchors = load_anchors()
    a = anchors.get(token)
    if isinstance(a, dict) and "lat" in a and "lng" in a:
        return float(a["lat"]), float(a["lng"])
    ll = _parse_latlng(token)
    if ll is not None:
        return ll
    g = geocode(token)
    if "error" in g:
        return g
    return g["lat"], g["lng"]


def _print(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 1 if isinstance(obj, dict) and "error" in obj else 0


USAGE = """usage: python3 -m core.geo <subcommand>
  geocode <address>
  regeo <lat> <lng>
  around <lat> <lng> [keywords] [radius_m]
  route <origin> <dest> [driving|walking|transit]   # anchor|lat,lng|address
  anchor-set <name> <lat> <lng> [address] [adcode]
  anchor-list"""


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 2
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "geocode":
            if not args:
                print(USAGE); return 2
            return _print(geocode(" ".join(args)))
        if cmd == "regeo":
            if len(args) < 2:
                print(USAGE); return 2
            return _print(regeo(args[0], args[1]))
        if cmd == "around":
            if len(args) < 2:
                print(USAGE); return 2
            kw = args[2] if len(args) > 2 else ""
            radius = int(args[3]) if len(args) > 3 else 1000
            return _print(around_poi(args[0], args[1], kw, radius))
        if cmd == "route":
            if len(args) < 2:
                print(USAGE); return 2
            mode = args[2] if len(args) > 2 else "driving"
            o = _resolve_place(args[0])
            if isinstance(o, dict):
                return _print(o)
            d = _resolve_place(args[1])
            if isinstance(d, dict):
                return _print(d)
            return _print(route(o, d, mode=mode))
        if cmd == "anchor-set":
            if len(args) < 3:
                print(USAGE); return 2
            address = args[3] if len(args) > 3 else ""
            adcode = args[4] if len(args) > 4 else ""
            return _print(set_anchor(args[0], args[1], args[2],
                                     address, adcode))
        if cmd == "anchor-list":
            return _print(load_anchors())
    except Exception as e:  # CLI must never traceback at heartbeat callers
        return _print({"error": f"{type(e).__name__}: {e}"})
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
