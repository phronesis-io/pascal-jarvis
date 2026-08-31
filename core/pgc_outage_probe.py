"""Auto-investigation for the「一手源大面积断连」anomaly card.

Pascal, 2026-08-13, on a card that ended with「👉 要我现在查具体是哪些源就说
一声」:「下次直接去查别等了」. So the metrics-digest pre-hook now runs the
read-only investigation itself and the findings ride in the same card —
which source families are down and how many, the dominant error, since
when, and one-line diagnosis (one shared upstream/proxy/key vs. many
unrelated sources). If the investigation cannot run, the card says so in
one honest line; it never asks whether it should go look.

Channels (cheapest first, each bounded by its own timeout):
1. Prometheus HTTP API on the monitoring host — one ssh round trip carrying
   a handful of curls. Observed 2026-08-31: ssh + 5 queries = 6.7 s. It also
   works when the crawl host itself is dead (the exact moment the card
   matters), because Prometheus keeps the last scraped series.
2. The crawl host's own ``/metrics`` exposition text (what the pgc_pulse
   probe script already reads).
3. Neither → ``ok=False`` with a plain-language reason.

Config lives in the gitignored sources.yaml, on the metrics_probe source
whose rule trips (see sources.example.yaml, key ``collect.investigate``);
hosts, URLs and paths are never hardcoded here.

The result is attached to the DATA record as ``investigation`` (the model
copies ``card_body`` verbatim) and staged in
``data/metrics/.investigation_pending.json`` so the post-hook can put the
findings back deterministically if the model dropped them — a card whose
numbers came from the probe must never degrade into a question.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Observed 2026-08-31 from the owner's Mac: one ssh to the monitoring host
# carrying the five queries below = 6.72 s wall (3.6 s of it is the ssh
# handshake; the source-info query alone returns ~19 KB). Budget ≈ 3× that.
DEFAULT_PROMETHEUS_TIMEOUT = 20
# The crawl host's /metrics is what data/probes/pgc_pulse.sh reads with a
# 10 s connect + 8 s read budget; the host was unreachable during this
# feature's build, so this is the existing probe's own budget, not a fresh
# measurement.
DEFAULT_METRICS_TIMEOUT = 15
PENDING_FILE = ".investigation_pending.json"
PENDING_REUSE_H = 2          # a retry within this window reuses the result
SINCE_WINDOW_H = 24
STALE_LOOKBACK = "3h"        # last_over_time window when the target is down
TOP_FAMILIES = 3
TOP_ERRORS = 3
EXAMPLE_NAMES = 3
SINGLE_CAUSE_SHARE = 0.7
PLAIN_QUESTION_RE = re.compile(r"^.*要我.*(查|看|追).*$", re.MULTILINE)

BROKEN_SERIES = "pgc_first_party_feed_broken"
COUNT_SERIES = "pgc_first_party_feeds_broken_count"
INFO_SERIES = "pgc_source_health_problem_source_info"

_FAMILY_LABEL = {
    "twitter": "X 推特", "bluesky": "Bluesky", "mastodon": "Mastodon",
    "newsapi": "NewsAPI", "sitemap": "站点地图", "github": "GitHub 发布",
    "statuspage": "API 状态页", "edgar": "SEC 公告", "fred": "FRED 数据",
    "rss": "普通订阅源", "other": "接口源",
}
_FAMILY_RULES = (
    ("twitter", re.compile(r"^(x|twitter)\s*[—\-/]|(^|[./])(twitter|x)\.com/", re.I)),
    ("bluesky", re.compile(r"^bluesky\b|bsky\.(app|social)", re.I)),
    ("mastodon", re.compile(r"^mastodon\b|mastodon\.", re.I)),
    ("newsapi", re.compile(r"^newsapi\b", re.I)),
    ("statuspage", re.compile(r"^api 状态|statuspage", re.I)),
    ("edgar", re.compile(r"^edgar\b|sec\.gov", re.I)),
    ("fred", re.compile(r"^fred\b|stlouisfed", re.I)),
    ("github", re.compile(r"github\.com/.+/releases", re.I)),
    ("sitemap", re.compile(r"sitemap[^/]*\.xml", re.I)),
)
_ERROR_RULES = (
    ("拒绝访问", re.compile(r"\b40[13]\b|forbidden|unauthori[sz]ed", re.I)),
    ("被限流", re.compile(r"\b429\b|too many|rate.?limit", re.I)),
    ("页面不存在", re.compile(r"\b4(04|10)\b|not found", re.I)),
    ("对方服务器报错", re.compile(
        r"\b5\d\d\b|bad gateway|gateway time|service unavailable|internal server", re.I)),
    ("证书问题", re.compile(r"\bssl\b|\btls\b|certificate", re.I)),
    ("代理故障", re.compile(r"proxy|socks|tunnel", re.I)),
    ("域名解析失败", re.compile(r"\bdns\b|getaddrinfo|name resolution|name or service", re.I)),
    ("连接超时", re.compile(r"time[d ]?out|etimedout", re.I)),
    ("连不上", re.compile(
        r"connection (refused|reset|error|aborted)|econn|remote end closed|unreachable", re.I)),
    ("已封禁", re.compile(r"blocked|auto-permanent|manual", re.I)),
    ("空内容", re.compile(r"\bempty\b|no (entries|items)|0 entries", re.I)),
    ("连续失败", re.compile(r"\d+ fails", re.I)),
)
_SHARED_CAUSE_CLASSES = {"拒绝访问", "被限流", "代理故障", "证书问题",
                         "域名解析失败", "连接超时", "连不上"}


# ── config ───────────────────────────────────────────────────────────


def load_investigate_cfg(jarvis_dir: Path, name: str, metric: str) -> dict | None:
    """The ``collect.investigate`` block of the enabled metrics_probe source
    called ``name`` in sources.yaml, if it targets ``metric``."""
    path = Path(jarvis_dir) / "sources.yaml"
    if not path.exists():
        return None
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    for src in ((data.get("perception") or {}).get("sources") or []):
        if not isinstance(src, dict) or src.get("type") != "metrics_probe":
            continue
        if not src.get("enabled", True):
            continue
        collect = src.get("collect") or {}
        if str(collect.get("name") or "") != name:
            continue
        inv = collect.get("investigate")
        if isinstance(inv, dict) and str(inv.get("metric") or "") == metric:
            return dict(inv)
        return None
    return None


def _timeout(cfg: dict, key: str, default: float) -> float:
    v = cfg.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        return default
    return float(v)


# ── transport ────────────────────────────────────────────────────────


def _run(cmd: list[str], timeout: float, stdin: str | None = None) -> tuple[int, str]:
    """(returncode, stdout). Timeouts and launch failures return (-1, "")."""
    try:
        proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return -1, ""
    return proc.returncode, proc.stdout or ""


_MARK = "@@JARVIS_REQ@@"


def _fetch_many(cfg: dict, requests: list[tuple[str, dict]], timeout: float,
                ssh_key: str, url_key: str, url_default: str) -> list[str | None]:
    """POST each (path, form) to the configured base URL, either through one
    ssh session (all requests in a single round trip) or locally via urllib.
    Returns one body per request, None where the fetch failed."""
    base = str(cfg.get(url_key) or url_default).rstrip("/")
    host = str(cfg.get(ssh_key) or "").strip()
    # Per-request cap: a 2 s cap tripped on a real run (curl gave up while
    # the batch as a whole had 12 s left), so never below 4 s per request.
    per_req = max(4.0, min(8.0, timeout / max(1, len(requests))))
    if host:
        script = ["set +e"]
        for path, form in requests:
            parts = ["curl", "-s", "--max-time", str(int(per_req)), "-X", "POST",
                     shlex.quote(base + path)]
            for k, v in form.items():
                parts += ["--data-urlencode", shlex.quote(f"{k}={v}")]
            script.append(f"printf '\\n{_MARK}\\n'")
            script.append(" ".join(parts))
        script.append(f"printf '\\n{_MARK}\\n'")
        connect = max(3, int(min(10, timeout / 2)))
        rc, out = _run(["ssh", "-o", "BatchMode=yes",
                        "-o", f"ConnectTimeout={connect}", host, "bash", "-s"],
                       timeout, stdin="\n".join(script) + "\n")
        if rc != 0 and not out.strip():
            return [None] * len(requests)
        chunks = out.split(_MARK)[1:]
        bodies: list[str | None] = []
        for i in range(len(requests)):
            body = chunks[i].strip() if i < len(chunks) else ""
            bodies.append(body or None)
        return bodies
    bodies = []
    for path, form in requests:
        try:
            data = urllib.parse.urlencode(form).encode()
            with urllib.request.urlopen(base + path, data=data, timeout=per_req) as r:
                bodies.append(r.read().decode("utf-8", "replace"))
        except Exception:
            bodies.append(None)
    return bodies


# ── parsing ──────────────────────────────────────────────────────────


def _vector(body: str | None) -> list[dict]:
    """Prometheus instant-query result → list of label dicts (value ignored)."""
    if not body:
        return []
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    if payload.get("status") != "success":
        return []
    out = []
    for item in (payload.get("data") or {}).get("result") or []:
        metric = item.get("metric") if isinstance(item, dict) else None
        if isinstance(metric, dict):
            out.append(dict(metric))
    return out


def _is_success(body: str | None) -> bool:
    if not body:
        return False
    try:
        return json.loads(body).get("status") == "success"
    except (ValueError, AttributeError):
        return False


def _matrix_values(body: str | None) -> list[tuple[float, float]]:
    """Prometheus range-query result (first series) → [(ts, value)]."""
    if not body:
        return []
    try:
        payload = json.loads(body)
    except ValueError:
        return []
    if payload.get("status") != "success":
        return []
    result = (payload.get("data") or {}).get("result") or []
    if not result:
        return []
    out = []
    for ts, val in result[0].get("values") or []:
        try:
            out.append((float(ts), float(val)))
        except (TypeError, ValueError):
            continue
    return out


_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


def _exposition_rows(text: str, series: str) -> list[dict]:
    """Rows of one gauge from Prometheus exposition text."""
    rows = []
    for line in text.splitlines():
        if not line.startswith(series + "{"):
            continue
        labels = {k: v.encode().decode("unicode_escape") if "\\" in v else v
                  for k, v in _LABEL_RE.findall(line.split("}", 1)[0])}
        rows.append(labels)
    return rows


# ── classification ───────────────────────────────────────────────────


def family_of(name: str, url: str = "") -> str:
    probe = f"{name}\n{url}"
    for fam, rx in _FAMILY_RULES:
        if rx.search(probe):
            return fam
    return "rss" if url.startswith("http") else "other"


def error_class(err: str) -> str:
    text = str(err or "").strip()
    if not text:
        return "其他错误"
    for cls, rx in _ERROR_RULES:
        if rx.search(text):
            if cls == "拒绝访问" or cls == "被限流" or cls == "页面不存在" \
                    or cls == "对方服务器报错":
                code = re.search(r"\b[45]\d\d\b", text)
                if code:
                    return f"{cls}({code.group(0)})"
            return cls
    return "其他错误"


def _display_name(name: str) -> str:
    # The post-hook rewrites any underscore_identifier as「相关指标」— keep
    # source names readable by spacing them out.
    return str(name).replace("_", " ").strip()[:30]


# ── analysis ─────────────────────────────────────────────────────────


def analyse(broken: list[dict], info: list[dict], count_samples: list[tuple[float, float]],
            *, threshold: float, now: datetime, host_reachable: bool,
            stale_minutes: int | None = None) -> dict:
    """Pure function: rows → structured findings (no rendering)."""
    info_by = {str(r.get("source") or ""): r for r in info}
    fams: Counter = Counter()
    errs: Counter = Counter()
    pair: Counter = Counter()
    names: list[str] = []
    for row in broken:
        name = str(row.get("source") or "").strip()
        if not name:
            continue
        meta = info_by.get(name) or {}
        fam = family_of(name, str(meta.get("source_url") or ""))
        err = error_class(str(meta.get("last_error") or row.get("error") or ""))
        fams[fam] += 1
        errs[err] += 1
        pair[(fam, err)] += 1
        names.append(name)
    total = len(names)
    since_ts = None
    if count_samples:
        run_start = None
        for ts, val in reversed(count_samples):
            if val >= threshold:
                run_start = ts
            else:
                break
        since_ts = run_start
        whole_window = run_start is not None and run_start == count_samples[0][0]
    else:
        whole_window = False
    diagnosis = ""
    if not host_reachable:
        diagnosis = "抓取主机本身没响应，所有源一起断——先看主机，不是源的问题"
    elif total:
        top_err, top_err_n = errs.most_common(1)[0]
        top_fam, top_fam_n = fams.most_common(1)[0]
        err_base = top_err.split("(")[0]
        if top_err_n / total >= SINGLE_CAUSE_SHARE and err_base in _SHARED_CAUSE_CLASSES:
            where = (f"集中在 {_FAMILY_LABEL.get(top_fam, top_fam)} 一族，"
                     if top_fam_n / total >= SINGLE_CAUSE_SHARE else "")
            diagnosis = (f"错误几乎一样（{top_err} {top_err_n}/{total}），{where}"
                         "像是同一个通道、代理或密钥出问题，不是各家源各自坏")
        else:
            diagnosis = "错误各不相同，更像多家源各自出问题，没有共同通道"
    return {
        "total": total,
        "families": [(f, n) for f, n in fams.most_common(TOP_FAMILIES)],
        "errors": [(e, n) for e, n in errs.most_common(TOP_ERRORS)],
        "examples": [_display_name(n) for n in names[:EXAMPLE_NAMES]],
        "since_ts": since_ts,
        "since_whole_window": whole_window,
        "diagnosis": diagnosis,
        "host_reachable": host_reachable,
        "stale_minutes": stale_minutes,
    }


# ── channels ─────────────────────────────────────────────────────────


def _via_prometheus(cfg: dict, now: datetime) -> dict | None:
    timeout = _timeout(cfg, "prometheus_timeout", DEFAULT_PROMETHEUS_TIMEOUT)
    end = now.timestamp()
    start = end - SINCE_WINDOW_H * 3600
    reqs = [
        ("/api/v1/query", {"query": COUNT_SERIES}),
        ("/api/v1/query", {"query": BROKEN_SERIES}),
        ("/api/v1/query", {"query": f"last_over_time({BROKEN_SERIES}[{STALE_LOOKBACK}])"}),
        ("/api/v1/query", {"query": f"last_over_time({INFO_SERIES}[{STALE_LOOKBACK}])"}),
        ("/api/v1/query_range", {"query": COUNT_SERIES, "start": f"{start:.0f}",
                                 "end": f"{end:.0f}", "step": "600"}),
    ]
    bodies = _fetch_many(cfg, reqs, timeout, "prometheus_ssh_host",
                         "prometheus_url", "http://127.0.0.1:9090")
    if not any(_is_success(b) for b in bodies):
        return None  # nothing Prometheus-shaped came back — not this channel
    count_now = _vector(bodies[0])
    broken_now = _vector(bodies[1])
    broken_stale = _vector(bodies[2])
    info = _vector(bodies[3])
    samples = _matrix_values(bodies[4])
    host_reachable = bool(count_now)
    stale_minutes = None
    broken = broken_now
    if not host_reachable:
        broken = broken_stale
        if samples:
            stale_minutes = max(0, int((end - samples[-1][0]) // 60))
    return {"channel": "prometheus", "broken": broken, "info": info,
            "samples": samples, "host_reachable": host_reachable,
            "stale_minutes": stale_minutes}


def _via_metrics_text(cfg: dict) -> dict | None:
    timeout = _timeout(cfg, "metrics_timeout", DEFAULT_METRICS_TIMEOUT)
    host = str(cfg.get("metrics_ssh_host") or "").strip()
    url = str(cfg.get("metrics_url") or "http://127.0.0.1:9090/metrics")
    if host:
        connect = max(3, int(min(10, timeout / 2)))
        rc, out = _run(["ssh", "-o", "BatchMode=yes",
                        "-o", f"ConnectTimeout={connect}", host,
                        f"curl -s --max-time {int(max(2, timeout - connect))} "
                        f"{shlex.quote(url)}"], timeout)
        if rc != 0 or not out.strip():
            return None
    else:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                out = r.read().decode("utf-8", "replace")
        except Exception:
            return None
    if COUNT_SERIES not in out:
        return None
    return {"channel": "metrics", "broken": _exposition_rows(out, BROKEN_SERIES),
            "info": _exposition_rows(out, INFO_SERIES), "samples": [],
            "host_reachable": True, "stale_minutes": None}


# ── rendering ────────────────────────────────────────────────────────


def _fmt_since(findings: dict, record_ts: str, now: datetime) -> str:
    ts = findings.get("since_ts")
    if findings.get("since_whole_window"):
        return f"至少 {SINCE_WINDOW_H} 小时前"
    dt = None
    if ts:
        dt = datetime.fromtimestamp(float(ts), tz=now.tzinfo)
    else:
        try:
            dt = datetime.fromisoformat(str(record_ts))
        except ValueError:
            return "今天"
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=now.tzinfo)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%m/%d %H:%M")


def render_lines(findings: dict, actual, record_ts: str, now: datetime) -> list[str]:
    """≤3 plain-language lines; the first sentence is the conclusion."""
    total = findings.get("total") or 0
    shown = total or actual
    since = _fmt_since(findings, record_ts, now)
    count_txt = f"{shown} 个一手源"
    if total and actual not in (None, total):
        count_txt = f"现在 {total} 个一手源（告警时 {actual} 个）"
    lead = f"{count_txt}从 {since} 起抓不到数据"
    diag = findings.get("diagnosis") or ""
    line1 = f"{lead}：{diag}。" if diag else f"{lead}。"
    lines = [line1]
    fams = "、".join(f"{_FAMILY_LABEL.get(f, f)} {n}" for f, n in findings.get("families") or [])
    errs = "、".join(f"{e} {n}" for e, n in findings.get("errors") or [])
    if fams or errs:
        parts = []
        if fams:
            parts.append(f"受影响：{fams}")
        if errs:
            parts.append(f"错误：{errs}")
        lines.append("；".join(parts) + "。")
    tail = []
    if findings.get("examples"):
        tail.append("例：" + "、".join(findings["examples"]))
    stale = findings.get("stale_minutes")
    if not findings.get("host_reachable"):
        if stale is not None:
            tail.append(f"名单是主机失联前的最后快照（{stale} 分钟前）")
        else:
            tail.append("名单是主机失联前的最后快照")
    if tail:
        lines.append("；".join(tail) + "。")
    return lines[:3]


def _failure_lines(actual, record_ts: str, now: datetime, reason: str) -> list[str]:
    since = _fmt_since({}, record_ts, now)
    return [f"{actual} 个一手源从 {since} 起持续抓不到数据。追查没跑通：{reason}。"]


# ── entry points ─────────────────────────────────────────────────────


def investigate(cfg: dict, record: dict, now: datetime,
                channels: tuple | None = None) -> dict:
    """Run the bounded read-only investigation for one anomaly record.

    Always returns a dict with ``ok``, ``card_body`` (rendered lines joined
    by newlines) and ``signature`` (the first line — what the post-hook
    looks for to decide whether the model kept the findings).
    ``channels`` lets tests inject fetchers; each is ``fn() -> dict|None``.
    """
    actual = record.get("actual")
    record_ts = str(record.get("ts") or "")
    rule = record.get("rule") if isinstance(record.get("rule"), dict) else {}
    threshold = rule.get("value") if isinstance(rule.get("value"), (int, float)) else 1
    if channels is None:
        channels = (lambda: _via_prometheus(cfg, now),
                    lambda: _via_metrics_text(cfg))
    raw = None
    for fn in channels:
        try:
            raw = fn()
        except Exception:
            raw = None
        if raw:
            break
    if not raw:
        # No host names on the card (personal config) — just which hop
        # failed and the budget it was given, so「没跑通」is verifiable.
        prom_s = int(_timeout(cfg, "prometheus_timeout", DEFAULT_PROMETHEUS_TIMEOUT))
        metr_s = int(_timeout(cfg, "metrics_timeout", DEFAULT_METRICS_TIMEOUT))
        reason = f"监控指标库 {prom_s} 秒内没回话，抓取主机 {metr_s} 秒内也连不上"
        lines = _failure_lines(actual, record_ts, now, reason)
        return {"ok": False, "reason": reason, "lines": lines,
                "card_body": "\n".join(lines), "signature": lines[0]}
    findings = analyse(raw["broken"], raw["info"], raw["samples"],
                       threshold=float(threshold), now=now,
                       host_reachable=raw["host_reachable"],
                       stale_minutes=raw.get("stale_minutes"))
    lines = render_lines(findings, actual, record_ts, now)
    return {
        "ok": True, "channel": raw["channel"], "total": findings["total"],
        "families": findings["families"], "errors": findings["errors"],
        "diagnosis": findings["diagnosis"], "host_reachable": findings["host_reachable"],
        "lines": lines, "card_body": "\n".join(lines), "signature": lines[0],
    }


def attach(jarvis_dir: Path, mdir: Path, record: dict, now: datetime) -> dict | None:
    """Pre-hook entry: investigate a configured anomaly record, attach the
    result to it, and stage it for the post-hook. A retry of the same
    record within PENDING_REUSE_H reuses the staged result instead of
    hitting the hosts again."""
    if record.get("kind") != "anomaly":
        return None
    cfg = load_investigate_cfg(jarvis_dir, str(record.get("name") or ""),
                               str(record.get("metric") or ""))
    if not cfg:
        return None
    pending_path = Path(mdir) / PENDING_FILE
    staged = _load_pending(pending_path)
    if (staged and staged.get("record_ts") == record.get("ts")
            and _fresh(staged.get("created"), now)):
        result = staged.get("result") or {}
    else:
        result = investigate(cfg, record, now)
        try:
            Path(mdir).mkdir(parents=True, exist_ok=True)
            tmp = pending_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "record_ts": record.get("ts"),
                "key": f"{record.get('name', '')}:{record.get('metric', '')}",
                "created": now.isoformat(timespec="seconds"),
                "result": result,
            }, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, pending_path)
        except OSError:
            pass
    record["investigation"] = {k: result.get(k) for k in
                               ("ok", "card_body", "diagnosis", "reason") if k in result}
    return result


def _load_pending(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fresh(created, now: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return (now - dt) < timedelta(hours=PENDING_REUSE_H)


FOLLOWUP_HEADER = "⚠️ 一手源断连追查结果"
_MATCH_RE = re.compile(r"一手")


def apply_backstop(mdir: Path, cards: list[tuple[str, str]],
                   consume: bool = True) -> list[tuple[str, str]]:
    """Post-hook entry. ``cards`` are (header, body) pairs the model
    produced. If an investigation is staged for this cycle, make sure its
    findings reach the user: the matching card keeps its header and gets
    the findings as its body when the model dropped them (and any
    「要我…查」line is removed — the answer is already here); with no
    matching card at all, a follow-up card carries the findings. Consumes
    the staged file so the next cycle starts clean."""
    pending_path = Path(mdir) / PENDING_FILE
    staged = _load_pending(pending_path)
    if not staged:
        return list(cards)
    result = staged.get("result") or {}
    body_lines = str(result.get("card_body") or "").strip()
    signature = str(result.get("signature") or "").strip()
    if not body_lines:
        return list(cards)
    out: list[tuple[str, str]] = []
    placed = False
    for header, body in cards:
        if not placed and _MATCH_RE.search(header + "\n" + body):
            body = PLAIN_QUESTION_RE.sub("", body).strip()
            body = re.sub(r"\n{2,}", "\n", body)
            if signature and signature not in body:
                body = body_lines
            placed = True
        out.append((header, body))
    if not placed:
        out.append((FOLLOWUP_HEADER, body_lines))
    if consume:
        try:
            pending_path.unlink()
        except OSError:
            pass
    return out
