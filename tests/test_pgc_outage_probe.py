"""The「一手源断连」card investigates itself (2026-08-13「下次直接去查别等了」).

Everything here is synthetic: a local HTTP server stands in for Prometheus /
the crawl host's /metrics, no ssh is ever spawned, no Lark is touched, and
JARVIS_DIR is a tmp dir for the pre/post hook runs.
"""

import json
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from core import pgc_outage_probe as probe

REPO = Path(__file__).resolve().parent.parent
PRE = REPO / "tasks" / "metrics_digest_pre.sh"
POST = REPO / "tasks" / "metrics_digest_post.py"

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 31, 9, 0, tzinfo=TZ)
REC_TS = "2026-08-31T06:56:00+08:00"


def _rec(actual=30, ts=REC_TS, name="pgc_demo", metric="broken_first_party"):
    return {"ts": ts, "date": ts[:10], "kind": "anomaly", "name": name,
            "metric": metric, "actual": actual, "threshold": 1,
            "rule": {"metric": metric, "op": ">=", "value": 1}, "digest_hint": ""}


def _broken_rows():
    rows = [{"source": f"X — Org {i}", "error": "HTTP 403"} for i in range(25)]
    rows += [{"source": f"Paper {i}", "error": "timed out"} for i in range(3)]
    rows += [{"source": "Bank Blog", "error": "HTTP 403"},
             {"source": "Court_Docket", "error": "12 fails"}]
    return rows


def _info_rows():
    return [{"source": f"Paper {i}", "source_url": f"https://p{i}.example/sitemap-news.xml",
             "last_error": "Read timed out"} for i in range(3)] + [
        {"source": "Bank Blog", "source_url": "https://bank.example/rss.xml",
         "last_error": "HTTP 403"}]


def _samples(since_hours_ago=2.07, total_hours=24, step_min=10, value=30):
    end = NOW.timestamp()
    out = []
    t = end - total_hours * 3600
    while t <= end:
        out.append((t, float(value) if t >= end - since_hours_ago * 3600 else 0.0))
        t += step_min * 60
    return out


# ── classification ───────────────────────────────────────────────────


@pytest.mark.parametrize("name,url,fam", [
    ("X — 机构官方一手声明", "", "twitter"),
    ("Some Org", "https://x.com/someorg", "twitter"),
    ("Bluesky - Official", "https://public.api.bsky.app/xrpc/x", "bluesky"),
    ("AP News Latest", "https://apnews.example/sitemap-news.xml", "sitemap"),
    ("Kubernetes Releases", "https://github.com/k/k/releases.atom", "github"),
    ("API 状态 — Vendor", "", "statuspage"),
    ("EDGAR — 8-K", "", "edgar"),
    ("Mistral AI News", "https://mistral.example/rss.xml", "rss"),
    ("全球财经日历", "", "other"),
])
def test_family_of(name, url, fam):
    assert probe.family_of(name, url) == fam


@pytest.mark.parametrize("err,cls", [
    ("HTTP 403", "拒绝访问(403)"),
    ("HTTP 429", "被限流(429)"),
    ("HTTP 404", "页面不存在(404)"),
    ("HTTP 502", "对方服务器报错(502)"),
    ("Read timed out (read timeout=20)", "连接超时"),
    ("SSLError: certificate verify failed", "证书问题"),
    ("ProxyError: Cannot connect to proxy", "代理故障"),
    ("Name or service not known", "域名解析失败"),
    ("Connection refused", "连不上"),
    ("auto-permanent", "已封禁"),
    ("12 fails", "连续失败"),
    ("", "其他错误"),
])
def test_error_class(err, cls):
    assert probe.error_class(err) == cls


# ── analysis + rendering ─────────────────────────────────────────────


def test_single_shared_cause_diagnosis_and_lines():
    f = probe.analyse(_broken_rows(), _info_rows(), _samples(),
                      threshold=1, now=NOW, host_reachable=True)
    assert f["total"] == 30
    assert f["families"][0] == ("twitter", 25)
    assert [fam for fam, _ in f["families"]] == ["twitter", "sitemap", "rss"]
    assert f["errors"][0] == ("拒绝访问(403)", 26)
    assert "同一个通道" in f["diagnosis"] and "X 推特" in f["diagnosis"]
    lines = probe.render_lines(f, 30, REC_TS, NOW)
    assert 1 <= len(lines) <= 3
    assert lines[0].startswith("30 个一手源从 07:00 起抓不到数据：")   # conclusion first, since from the range
    assert "受影响：X 推特 25、站点地图 3、普通订阅源 1" in lines[1]
    assert "拒绝访问(403) 26" in lines[1]
    assert "例：X — Org 0" in lines[2]
    assert "Court Docket" not in "\n".join(lines) or "_" not in "\n".join(lines)
    assert "要我" not in "\n".join(lines)


def test_unrelated_sources_diagnosis():
    rows = [{"source": "A", "error": "HTTP 403"}, {"source": "B", "error": "timed out"},
            {"source": "C", "error": "HTTP 500"}, {"source": "D", "error": "3 fails"}]
    f = probe.analyse(rows, [], [], threshold=1, now=NOW, host_reachable=True)
    assert "各自出问题" in f["diagnosis"]
    lines = probe.render_lines(f, 4, REC_TS, NOW)
    # no range data → since falls back to the record's own timestamp
    assert lines[0].startswith("4 个一手源从 06:56 起")


def test_host_unreachable_diagnosis_marks_snapshot_as_stale():
    f = probe.analyse(_broken_rows()[:3], [], _samples(), threshold=1, now=NOW,
                      host_reachable=False, stale_minutes=9)
    assert "主机" in f["diagnosis"]
    lines = probe.render_lines(f, 30, REC_TS, NOW)
    assert "现在 3 个一手源（告警时 30 个）" in lines[0]
    assert "最后快照（9 分钟前）" in lines[-1]


def test_since_whole_window_when_broken_all_day():
    f = probe.analyse(_broken_rows(), [], _samples(since_hours_ago=48),
                      threshold=1, now=NOW, host_reachable=True)
    assert f["since_whole_window"]
    assert "至少 24 小时前" in probe.render_lines(f, 30, REC_TS, NOW)[0]


def test_investigate_falls_through_channels_then_reports_honestly():
    calls = []
    raw = {"channel": "metrics", "broken": _broken_rows(), "info": _info_rows(),
           "samples": [], "host_reachable": True, "stale_minutes": None}
    res = probe.investigate({}, _rec(), NOW, channels=(
        lambda: calls.append("a") or None,
        lambda: calls.append("b") or raw))
    assert calls == ["a", "b"] and res["ok"] and res["channel"] == "metrics"
    assert res["signature"] == res["lines"][0]

    res = probe.investigate({"prometheus_timeout": 20, "metrics_timeout": 15},
                            _rec(actual=144), NOW,
                            channels=(lambda: None, lambda: (_ for _ in ()).throw(OSError())))
    assert res["ok"] is False
    assert res["card_body"] == ("144 个一手源从 06:56 起持续抓不到数据。追查没跑通："
                                "监控指标库 20 秒内没回话，抓取主机 15 秒内也连不上。")


# ── transports against a local stand-in server (no ssh) ──────────────


class _Stub(BaseHTTPRequestHandler):
    routes: dict = {}
    hits: list = []

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        self._reply(self.routes.get(("GET", self.path), ("", 404)))

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        query = (form.get("query") or [""])[0]
        type(self).hits.append((self.path, query))
        self._reply(self.routes.get(("POST", self.path, query), ('{"status":"error"}', 200)))

    def _reply(self, spec):
        body, code = spec
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.fixture
def stub_server():
    _Stub.routes, _Stub.hits = {}, []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Stub
    srv.shutdown()


def _vec(rows):
    return json.dumps({"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": r, "value": [NOW.timestamp(), "1"]} for r in rows]}})


def _mat(samples):
    return json.dumps({"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": [[t, str(v)] for t, v in samples]}]}})


def _prom_routes(rows, info, samples, reachable=True):
    q = "/api/v1/query"
    return {
        ("POST", q, probe.COUNT_SERIES): (_vec([{"__name__": probe.COUNT_SERIES}]) if reachable
                                          else _vec([]), 200),
        ("POST", q, probe.BROKEN_SERIES): (_vec(rows if reachable else []), 200),
        ("POST", q, f"last_over_time({probe.BROKEN_SERIES}[3h])"): (_vec(rows), 200),
        ("POST", q, f"last_over_time({probe.INFO_SERIES}[3h])"): (_vec(info), 200),
        ("POST", "/api/v1/query_range", probe.COUNT_SERIES): (_mat(samples), 200),
    }


def test_prometheus_channel_parses_live_and_stale_states(stub_server):
    base, stub = stub_server
    stub.routes = _prom_routes(_broken_rows(), _info_rows(), _samples())
    cfg = {"prometheus_url": base}
    raw = probe._via_prometheus(cfg, NOW)
    assert raw["host_reachable"] and len(raw["broken"]) == 30 and len(raw["info"]) == 4
    assert len(raw["samples"]) > 100
    res = probe.investigate(cfg, _rec(), NOW)
    assert res["ok"] and res["families"][0] == ["twitter", 25] or res["families"][0] == ("twitter", 25)

    stub.routes = _prom_routes(_broken_rows(), _info_rows(), _samples(), reachable=False)
    raw = probe._via_prometheus(cfg, NOW)
    assert raw["host_reachable"] is False and len(raw["broken"]) == 30
    assert raw["stale_minutes"] is not None


def test_prometheus_channel_none_when_nothing_prometheus_shaped(stub_server):
    base, stub = stub_server
    stub.routes = {}   # every query answers {"status":"error"}
    assert probe._via_prometheus({"prometheus_url": base}, NOW) is None
    assert probe._via_prometheus({"prometheus_url": "http://127.0.0.1:9"}, NOW) is None


def test_metrics_text_channel_parses_exposition(stub_server):
    base, stub = stub_server
    text = "\n".join([
        f'{probe.BROKEN_SERIES}{{error="HTTP 403",source="X — Org 1"}} 1.0',
        f'{probe.BROKEN_SERIES}{{error="timed out",source="Paper \\"A\\""}} 1.0',
        f'{probe.INFO_SERIES}{{category="c",detail="d",issue="i",last_error="HTTP 403",'
        f'source="X — Org 1",source_tier="T0",source_url="https://x.com/org1"}} 1.0',
        f"{probe.COUNT_SERIES} 2.0",
    ])
    stub.routes = {("GET", "/metrics"): (text, 200)}
    raw = probe._via_metrics_text({"metrics_url": base + "/metrics"})
    assert raw["channel"] == "metrics" and raw["host_reachable"]
    assert raw["broken"][1]["source"] == 'Paper "A"'
    assert raw["info"][0]["source_url"] == "https://x.com/org1"
    stub.routes = {("GET", "/metrics"): ("unrelated 1", 200)}
    assert probe._via_metrics_text({"metrics_url": base + "/metrics"}) is None


def test_ssh_transport_batches_requests_in_one_session(monkeypatch):
    seen = []

    def fake_run(cmd, timeout, stdin=None):
        seen.append((cmd, timeout, stdin))
        m = probe._MARK
        return 0, f"\n{m}\n{_vec([])}\n{m}\n{_vec([{'source': 'A', 'error': 'HTTP 403'}])}\n{m}\n"

    monkeypatch.setattr(probe, "_run", fake_run)
    bodies = probe._fetch_many({"prometheus_ssh_host": "mon"},
                               [("/api/v1/query", {"query": "a"}),
                                ("/api/v1/query", {"query": "b(c[3h])"})],
                               20, "prometheus_ssh_host", "prometheus_url", "http://127.0.0.1:9090")
    assert len(seen) == 1                       # one ssh round trip for both
    cmd, timeout, stdin = seen[0]
    assert cmd[:4] == ["ssh", "-o", "BatchMode=yes", "-o"] and "mon" in cmd
    assert timeout == 20 and "--data-urlencode 'query=b(c[3h])'" in stdin
    assert len(bodies) == 2 and '"source": "A"' in bodies[1]

    monkeypatch.setattr(probe, "_run", lambda cmd, timeout, stdin=None: (-1, ""))
    assert probe._fetch_many({"prometheus_ssh_host": "mon"}, [("/x", {"query": "a"})],
                             20, "prometheus_ssh_host", "prometheus_url", "u") == [None]


# ── config ───────────────────────────────────────────────────────────


def _write_sources(tmp_path, base_url, enabled=True, metric="broken_first_party"):
    (tmp_path / "sources.yaml").write_text(json.dumps({"perception": {"sources": [
        {"id": "pgc_demo", "type": "metrics_probe", "enabled": enabled,
         "collect": {"name": "pgc_demo", "command": "true",
                     "investigate": {"metric": metric, "prometheus_url": base_url,
                                     "metrics_url": base_url + "/metrics"}},
         "schedule": {"interval": "2h"}}]}}), encoding="utf-8")


def test_load_investigate_cfg_matches_name_and_metric(tmp_path):
    _write_sources(tmp_path, "http://127.0.0.1:1")
    cfg = probe.load_investigate_cfg(tmp_path, "pgc_demo", "broken_first_party")
    assert cfg and cfg["prometheus_url"] == "http://127.0.0.1:1"
    assert probe.load_investigate_cfg(tmp_path, "pgc_demo", "other") is None
    assert probe.load_investigate_cfg(tmp_path, "nope", "broken_first_party") is None
    _write_sources(tmp_path, "http://127.0.0.1:1", enabled=False)
    assert probe.load_investigate_cfg(tmp_path, "pgc_demo", "broken_first_party") is None
    assert probe.load_investigate_cfg(tmp_path / "missing", "pgc_demo", "x") is None


# ── pre-hook integration ─────────────────────────────────────────────


def _env(tmp_path):
    import os
    return {**os.environ, "JARVIS_DIR": str(tmp_path)}


def _write_records(tmp_path, records, name="pgc_demo"):
    mdir = tmp_path / "data" / "metrics"
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / f"{name}.jsonl", "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return mdir


def _run_pre(tmp_path):
    return subprocess.run(["bash", str(PRE)], env=_env(tmp_path),
                          capture_output=True, text=True, timeout=60)


def _run_post(tmp_path, payload):
    return subprocess.run([sys.executable, str(POST)], input=payload,
                          env=_env(tmp_path), capture_output=True, text=True, timeout=60)


def test_pre_attaches_findings_and_reuses_them_on_retry(tmp_path, stub_server):
    base, stub = stub_server
    stub.routes = _prom_routes(_broken_rows(), _info_rows(), _samples())
    _write_sources(tmp_path, base)
    mdir = _write_records(tmp_path, [_rec()])
    r = _run_pre(tmp_path)
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout.splitlines()[1])
    inv = rec["investigation"]
    assert inv["ok"] is True
    assert "受影响：X 推特 25、站点地图 3、普通订阅源 1" in inv["card_body"]
    assert "同一个通道" in inv["diagnosis"]
    assert "要我" not in inv["card_body"]
    staged = json.loads((mdir / probe.PENDING_FILE).read_text())
    assert staged["record_ts"] == REC_TS and staged["result"]["card_body"] == inv["card_body"]
    hits = len(stub.hits)
    # failed render → pre re-collects the same record: findings are reused,
    # the hosts are not hit again
    r = _run_pre(tmp_path)
    assert json.loads(r.stdout.splitlines()[1])["investigation"]["card_body"] == inv["card_body"]
    assert len(stub.hits) == hits


def test_pre_without_investigate_config_is_unchanged(tmp_path):
    mdir = _write_records(tmp_path, [_rec()])
    r = _run_pre(tmp_path)
    assert '"kind": "anomaly"' in r.stdout and "investigation" not in r.stdout
    assert not (mdir / probe.PENDING_FILE).exists()


def test_pre_reports_failed_investigation_honestly(tmp_path):
    _write_sources(tmp_path, "http://127.0.0.1:9")   # nothing listens
    _write_records(tmp_path, [_rec(actual=144)])
    r = _run_pre(tmp_path)
    inv = json.loads(r.stdout.splitlines()[1])["investigation"]
    assert inv["ok"] is False
    assert inv["card_body"].startswith("144 个一手源从 06:56 起持续抓不到数据。追查没跑通：")
    assert "要我" not in inv["card_body"]


# ── post-hook backstop ───────────────────────────────────────────────

FINDINGS = ("30 个一手源从 06:56 起抓不到数据：错误几乎一样（拒绝访问(403) 26/30），"
            "集中在X 推特一族，像是同一个通道、代理或密钥出问题，不是各家源各自坏。\n"
            "受影响：X 推特 25、站点地图 3、普通订阅源 1；错误：拒绝访问(403) 26、连接超时 3、连续失败 1。\n"
            "例：X — Org 0、X — Org 1、X — Org 2。")


def _stage(tmp_path, card_body=FINDINGS):
    mdir = tmp_path / "data" / "metrics"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / ".digest_pending.json").write_text(json.dumps({"ts": REC_TS}))
    (mdir / probe.PENDING_FILE).write_text(json.dumps({
        "record_ts": REC_TS, "key": "pgc_demo:broken_first_party",
        "created": NOW.isoformat(), "result": {
            "ok": True, "card_body": card_body,
            "signature": card_body.split("\n")[0]}}, ensure_ascii=False))
    return mdir


def _cards(stdout):
    return [json.loads(l) for l in stdout.splitlines() if l.startswith('{"config":')]


def _text(card):
    return json.dumps(card, ensure_ascii=False)


def test_post_keeps_verbatim_findings_and_strips_dead_end_question(tmp_path):
    mdir = _stage(tmp_path)
    payload = json.dumps({"cards": [{"header": "⚠️ 一手源大面积断连",
                                     "body": FINDINGS + "\n👉 要我现在查具体是哪些源就说一声"}]},
                         ensure_ascii=False)
    r = _run_post(tmp_path, payload)
    cards = _cards(r.stdout)
    assert len(cards) == 1
    text = _text(cards[0])
    assert "一手源大面积断连" in text and "受影响：X 推特 25" in text
    assert "要我" not in text
    assert not (mdir / probe.PENDING_FILE).exists()          # consumed
    assert (mdir / ".digest_watermark.json").exists()


def test_post_restores_findings_when_model_paraphrased(tmp_path):
    _stage(tmp_path)
    payload = json.dumps({"cards": [
        {"header": "📈 别的指标", "body": "无关"},
        {"header": "⚠️ 一手源大面积断连",
         "body": "30 个一手内容源抓不到数据。👉 要我现在查就说一声"}]}, ensure_ascii=False)
    cards = _cards(_run_post(tmp_path, payload).stdout)
    assert len(cards) == 2
    text = _text(cards[1])
    assert "同一个通道、代理或密钥出问题" in text and "例：X — Org 0" in text
    assert "要我" not in text
    assert "一手" not in _text(cards[0])


def test_post_emits_followup_card_when_no_card_matches(tmp_path):
    _stage(tmp_path)
    payload = json.dumps({"cards": [{"header": "📈 别的指标", "body": "无关"}]})
    cards = _cards(_run_post(tmp_path, payload).stdout)
    assert len(cards) == 2
    text = _text(cards[1])
    assert probe.FOLLOWUP_HEADER in text and "受影响：X 推特 25" in text


def test_post_sentinel_still_delivers_staged_findings(tmp_path):
    mdir = _stage(tmp_path)
    r = _run_post(tmp_path, "HEARTBEAT_OK")
    cards = _cards(r.stdout)
    assert len(cards) == 1 and probe.FOLLOWUP_HEADER in _text(cards[0])
    assert (mdir / ".digest_watermark.json").exists()
    assert not (mdir / probe.PENDING_FILE).exists()


def test_post_without_staged_investigation_is_unchanged(tmp_path):
    mdir = tmp_path / "data" / "metrics"
    mdir.mkdir(parents=True)
    payload = json.dumps({"cards": [{"header": "⚠️ 一手源大面积断连", "body": "x"}]},
                         ensure_ascii=False)
    cards = _cards(_run_post(tmp_path, payload).stdout)
    assert len(cards) == 1 and probe.FOLLOWUP_HEADER not in _text(cards[0])


def test_rendered_card_meets_style_contract(tmp_path):
    """First sentence = conclusion, ≤3 body lines, no dead-end question."""
    _stage(tmp_path)
    payload = json.dumps({"cards": [{"header": "⚠️ 一手源大面积断连", "body": "改写了"}]},
                         ensure_ascii=False)
    card = _cards(_run_post(tmp_path, payload).stdout)[0]
    body = next(e["text"]["content"] for e in card["elements"] if e.get("tag") == "div")
    lines = [l for l in body.split("\n") if l.strip()]
    assert lines[0].startswith("30 个一手源从 06:56 起抓不到数据：")
    assert len(lines) <= 3
    assert "要我" not in body and "_" not in body
