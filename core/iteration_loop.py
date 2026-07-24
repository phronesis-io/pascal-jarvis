"""L3 observation, proposal review, and post-release outcome verification.

Signals are evidence, not tasks.  Repeated or critical signals may become a
pending Proposal, but only an explicit owner review can place work in the L2
Taskline queue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Callable


PROPOSAL_STATES = {
    "pending",
    "approved",
    "queued",
    "shipped",
    "verified",
    "needs_followup",
    "rejected",
}
SEVERITIES = {"info", "minor", "major", "critical"}


class IterationError(RuntimeError):
    """Invalid L3 lifecycle operation."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _fingerprint(source: str, category: str, key: str) -> str:
    return hashlib.sha256(f"{source}\0{category}\0{key}".encode("utf-8")).hexdigest()


def _safe(value: Any, field: str, limit: int = 1000) -> str:
    result = str(value or "").strip()
    if len(result) > limit:
        raise IterationError(f"{field} exceeds {limit} characters")
    lowered = result.casefold()
    if any(
        marker in lowered
        for marker in ("authorization:", "bearer ", "access_token", "api_key", "cookie")
    ):
        raise IterationError(f"{field} appears to contain a secret")
    return result


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class IterationStore:
    def __init__(
        self,
        root: str | Path | None = None,
        db_path: str | Path | None = None,
        now: Callable[[], float] = time.time,
    ):
        self.root = Path(
            root
            or os.environ.get("JARVIS_DIR")
            or Path(__file__).resolve().parent.parent
        )
        self.db_path = Path(
            db_path
            or os.environ.get("JARVIS_DB_PATH")
            or self.root / "data" / "jarvis.db"
        )
        self.now = now
        self._ensure()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(self.db_path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def _ensure(self) -> None:
        with closing(self._connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS iteration_signals (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    occurrence_count INTEGER NOT NULL DEFAULT 1,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                );
                CREATE INDEX IF NOT EXISTS idx_iteration_signals_open
                    ON iteration_signals(status,severity,last_seen);

                CREATE TABLE IF NOT EXISTS iteration_proposals (
                    id TEXT PRIMARY KEY,
                    signal_fingerprint TEXT NOT NULL,
                    title TEXT NOT NULL,
                    problem TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    non_goals_json TEXT NOT NULL DEFAULT '[]',
                    product_direction TEXT NOT NULL DEFAULT '',
                    technical_direction TEXT NOT NULL DEFAULT '',
                    acceptance_json TEXT NOT NULL DEFAULT '[]',
                    priority INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    taskline_id TEXT NOT NULL DEFAULT '',
                    item_id TEXT NOT NULL DEFAULT '',
                    review_reason TEXT NOT NULL DEFAULT '',
                    release_sha TEXT NOT NULL DEFAULT '',
                    baseline_json TEXT NOT NULL DEFAULT '{}',
                    expected_json TEXT NOT NULL DEFAULT '{}',
                    actual_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    reviewed_at REAL,
                    queued_at REAL,
                    shipped_at REAL,
                    verified_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_iteration_proposals_state
                    ON iteration_proposals(status,priority,updated_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_iteration_proposals_pending
                    ON iteration_proposals(signal_fingerprint)
                    WHERE status='pending';

                CREATE TABLE IF NOT EXISTS iteration_events (
                    id TEXT PRIMARY KEY,
                    proposal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    from_status TEXT NOT NULL DEFAULT '',
                    to_status TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_iteration_events
                    ON iteration_events(proposal_id,created_at);
                """
            )
            columns = {
                str(row["name"])
                for row in db.execute(
                    "PRAGMA table_info(iteration_proposals)"
                ).fetchall()
            }
            if "item_id" not in columns:
                db.execute(
                    "ALTER TABLE iteration_proposals "
                    "ADD COLUMN item_id TEXT NOT NULL DEFAULT ''"
                )

    def record_signal(
        self,
        *,
        source: str,
        category: str,
        key: str,
        severity: str,
        summary: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        source = _safe(source, "source", 100)
        category = _safe(category, "category", 100)
        key = _safe(key, "key", 300)
        summary = _safe(summary, "summary", 500)
        if severity not in SEVERITIES:
            raise IterationError("invalid severity")
        encoded = _json(evidence)
        if len(encoded) > 10_000:
            raise IterationError("evidence exceeds 10KB")
        fingerprint = _fingerprint(source, category, key)
        now = self.now()
        with closing(self._connect()) as db, db:
            existing = db.execute(
                "SELECT * FROM iteration_signals WHERE fingerprint=?",
                (fingerprint,),
            ).fetchone()
            if existing is None:
                signal_id = _new_id("sig")
                db.execute(
                    """
                    INSERT INTO iteration_signals(
                        id,fingerprint,source,category,severity,summary,
                        evidence_json,first_seen,last_seen
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        signal_id,
                        fingerprint,
                        source,
                        category,
                        severity,
                        summary,
                        encoded,
                        now,
                        now,
                    ),
                )
            else:
                signal_id = str(existing["id"])
                db.execute(
                    """
                    UPDATE iteration_signals
                       SET severity=?,summary=?,evidence_json=?,
                           occurrence_count=occurrence_count+1,last_seen=?,
                           status='open'
                     WHERE id=?
                    """,
                    (severity, summary, encoded, now, signal_id),
                )
            result = db.execute(
                "SELECT * FROM iteration_signals WHERE id=?", (signal_id,)
            ).fetchone()
        return self._decode_signal(result)

    @staticmethod
    def _decode_signal(row: sqlite3.Row) -> dict[str, Any]:
        result = _row(row) or {}
        result["evidence"] = json.loads(result.pop("evidence_json"))
        return result

    def create_proposal(
        self,
        *,
        signal_fingerprint: str,
        title: str,
        problem: str,
        goal: str,
        non_goals: list[str],
        product_direction: str,
        technical_direction: str,
        acceptance: list[dict[str, Any] | str],
        priority: int,
        baseline: dict[str, Any] | None = None,
        expected: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        title = _safe(title, "title", 300)
        problem = _safe(problem, "problem", 2000)
        goal = _safe(goal, "goal", 1000)
        product_direction = _safe(product_direction, "product_direction", 2000)
        technical_direction = _safe(technical_direction, "technical_direction", 2000)
        signal_fingerprint = _safe(
            signal_fingerprint, "signal_fingerprint", 100
        )
        priority = max(0, min(int(priority), 100))
        now = self.now()
        proposal_id = _new_id("prp")
        with closing(self._connect()) as db, db:
            signal = db.execute(
                "SELECT * FROM iteration_signals WHERE fingerprint=?",
                (signal_fingerprint,),
            ).fetchone()
            if signal is None:
                raise IterationError("proposal signal does not exist")
            existing = db.execute(
                """
                SELECT * FROM iteration_proposals
                 WHERE signal_fingerprint=? AND status='pending'
                """,
                (signal_fingerprint,),
            ).fetchone()
            if existing is not None:
                return self._decode_proposal(db, existing), False
            db.execute(
                """
                INSERT INTO iteration_proposals(
                    id,signal_fingerprint,title,problem,goal,non_goals_json,
                    product_direction,technical_direction,acceptance_json,
                    priority,status,baseline_json,expected_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?)
                """,
                (
                    proposal_id,
                    signal_fingerprint,
                    title,
                    problem,
                    goal,
                    _json(non_goals),
                    product_direction,
                    technical_direction,
                    _json(acceptance),
                    priority,
                    _json(baseline or {}),
                    _json(expected or {}),
                    now,
                    now,
                ),
            )
            self._event(
                db,
                proposal_id,
                "proposal.created",
                actor="observer",
                to_status="pending",
            )
            result = db.execute(
                "SELECT * FROM iteration_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        return self._decode_proposal_from_row(result), True

    def propose_from_signal(
        self, signal: dict[str, Any]
    ) -> tuple[dict[str, Any], bool] | None:
        """Promote only critical-once or major-repeated evidence."""
        if signal["severity"] == "critical":
            priority = 100
        elif signal["severity"] == "major" and signal["occurrence_count"] >= 2:
            priority = 80
        else:
            return None
        return self.create_proposal(
            signal_fingerprint=signal["fingerprint"],
            title=signal["summary"],
            problem=(
                f"{signal['source']} 持续观测到 {signal['category']}："
                f"{signal['summary']}（{signal['occurrence_count']} 次）。"
            ),
            goal="消除该真实故障，并用发布后的同源指标证明结果。",
            non_goals=["不因单次噪声扩大功能范围", "不降低现有安全和隐私门槛"],
            product_direction="只在问题真正需要 Pascal 判断时创建一个聚合事项。",
            technical_direction="先补确定性复现和观测，再修复并保留发布后回读。",
            acceptance=[
                "原始信号不再出现",
                "相关回归测试通过",
                "发布后同源观测达到预期",
            ],
            priority=priority,
            baseline={
                "occurrence_count": signal["occurrence_count"],
                "severity": signal["severity"],
            },
            expected={"signal_open": False},
        )

    def review(
        self,
        proposal_id: str,
        *,
        approved: bool,
        actor: str,
        reason: str = "",
        queue: bool = True,
    ) -> dict[str, Any]:
        reason = _safe(reason, "reason", 1000)
        actor = _safe(actor, "actor", 200)
        now = self.now()
        with closing(self._connect()) as db, db:
            current = self._require_proposal(db, proposal_id)
            if current["status"] != "pending":
                raise IterationError("only a pending proposal can be reviewed")
            status = "approved" if approved else "rejected"
            db.execute(
                """
                UPDATE iteration_proposals
                   SET status=?,review_reason=?,reviewed_at=?,updated_at=?
                 WHERE id=?
                """,
                (status, reason, now, now, proposal_id),
            )
            self._event(
                db,
                proposal_id,
                f"proposal.{status}",
                actor=actor,
                from_status="pending",
                to_status=status,
                metadata={"reason": reason},
            )
        if approved and queue:
            return self.queue(proposal_id)
        return self.get(proposal_id)

    def set_item(self, proposal_id: str, item_id: str) -> None:
        item_id = _safe(item_id, "item_id", 200)
        with closing(self._connect()) as db, db:
            self._require_proposal(db, proposal_id)
            db.execute(
                "UPDATE iteration_proposals SET item_id=?,updated_at=? WHERE id=?",
                (item_id, self.now(), proposal_id),
            )

    def queue(self, proposal_id: str) -> dict[str, Any]:
        detail = self.get(proposal_id)
        if detail["status"] != "approved":
            raise IterationError("only an approved proposal can enter Taskline")
        description = "\n\n".join(
            (
                f"Problem: {detail['problem']}",
                f"Goal: {detail['goal']}",
                f"Product direction: {detail['product_direction']}",
                f"Technical direction: {detail['technical_direction']}",
                "Acceptance:\n"
                + "\n".join(f"- {item}" for item in detail["acceptance"]),
                f"Jarvis proposal: {detail['id']}",
            )
        )
        command = [
            "taskline",
            "task",
            "create",
            "--project",
            "pascal-jarvis",
            "--title",
            detail["title"],
            "--description",
            description,
            "--type",
            "bug",
            "--priority",
            str(detail["priority"]),
            "--label",
            "l3-proposal",
            "--label",
            detail["id"],
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise IterationError(f"Taskline queue failed: {exc}") from exc
        if result.returncode != 0:
            raise IterationError(
                (result.stderr or result.stdout or "Taskline queue failed").strip()[:300]
            )
        try:
            task = json.loads(result.stdout)
            task_id = str(task["id"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise IterationError("Taskline returned invalid task receipt") from exc
        now = self.now()
        with closing(self._connect()) as db, db:
            current = self._require_proposal(db, proposal_id)
            if current["status"] != "approved":
                raise IterationError("proposal changed before Taskline receipt")
            db.execute(
                """
                UPDATE iteration_proposals
                   SET status='queued',taskline_id=?,queued_at=?,updated_at=?
                 WHERE id=?
                """,
                (task_id, now, now, proposal_id),
            )
            self._event(
                db,
                proposal_id,
                "proposal.queued",
                actor="taskline",
                from_status="approved",
                to_status="queued",
                metadata={"taskline_id": task_id},
            )
        return self.get(proposal_id)

    def mark_shipped(
        self, proposal_id: str, *, release_sha: str, actor: str
    ) -> dict[str, Any]:
        release_sha = _safe(release_sha, "release_sha", 100)
        if len(release_sha) < 7:
            raise IterationError("release_sha is invalid")
        now = self.now()
        with closing(self._connect()) as db, db:
            current = self._require_proposal(db, proposal_id)
            if current["status"] != "queued":
                raise IterationError("only queued work can be marked shipped")
            db.execute(
                """
                UPDATE iteration_proposals
                   SET status='shipped',release_sha=?,shipped_at=?,updated_at=?
                 WHERE id=?
                """,
                (release_sha, now, now, proposal_id),
            )
            self._event(
                db,
                proposal_id,
                "proposal.shipped",
                actor=actor,
                from_status="queued",
                to_status="shipped",
                metadata={"release_sha": release_sha},
            )
        return self.get(proposal_id)

    def verify_outcome(
        self,
        proposal_id: str,
        *,
        actual: dict[str, Any],
        matched: bool,
        actor: str = "observer",
    ) -> dict[str, Any]:
        now = self.now()
        with closing(self._connect()) as db, db:
            current = self._require_proposal(db, proposal_id)
            if current["status"] != "shipped":
                raise IterationError("only shipped work can verify outcomes")
            status = "verified" if matched else "needs_followup"
            db.execute(
                """
                UPDATE iteration_proposals
                   SET status=?,actual_json=?,verified_at=?,updated_at=?
                 WHERE id=?
                """,
                (status, _json(actual), now, now, proposal_id),
            )
            self._event(
                db,
                proposal_id,
                "proposal.outcome_verified",
                actor=actor,
                from_status="shipped",
                to_status=status,
                metadata={"matched": bool(matched)},
            )
            if matched:
                db.execute(
                    """
                    UPDATE iteration_signals SET status='resolved'
                     WHERE fingerprint=?
                    """,
                    (current["signal_fingerprint"],),
                )
        return self.get(proposal_id)

    def get(self, proposal_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = self._require_proposal(db, proposal_id)
            return self._decode_proposal(db, row)

    def list(
        self, *, status: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        if status and status not in PROPOSAL_STATES:
            raise IterationError("invalid proposal status")
        where = "WHERE status=?" if status else ""
        values: list[Any] = [status] if status else []
        values.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as db:
            rows = db.execute(
                f"""
                SELECT * FROM iteration_proposals {where}
                 ORDER BY priority DESC,updated_at DESC LIMIT ?
                """,
                values,
            ).fetchall()
            return [self._decode_proposal(db, row) for row in rows]

    def _decode_proposal(
        self, db: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        result = self._decode_proposal_from_row(row)
        result["events"] = [
            {
                **dict(event),
                "metadata": json.loads(event["metadata_json"]),
            }
            for event in db.execute(
                """
                SELECT * FROM iteration_events WHERE proposal_id=?
                 ORDER BY created_at,rowid
                """,
                (result["id"],),
            ).fetchall()
        ]
        for event in result["events"]:
            event.pop("metadata_json", None)
        return result

    @staticmethod
    def _decode_proposal_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = _row(row) or {}
        for field in ("non_goals", "acceptance", "baseline", "expected", "actual"):
            result[field] = json.loads(result.pop(f"{field}_json"))
        return result

    @staticmethod
    def _require_proposal(db: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM iteration_proposals WHERE id=?", (proposal_id,)
        ).fetchone()
        if row is None:
            raise IterationError("proposal not found")
        return row

    def _event(
        self,
        db: sqlite3.Connection,
        proposal_id: str,
        event_type: str,
        *,
        actor: str,
        from_status: str = "",
        to_status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            """
            INSERT INTO iteration_events(
                id,proposal_id,event_type,actor,from_status,to_status,
                metadata_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                _new_id("ite"),
                proposal_id,
                event_type,
                actor,
                from_status,
                to_status,
                _json(metadata or {}),
                self.now(),
            ),
        )


class DailyObserver:
    """Collect bounded evidence from existing feedback and operations stores."""

    def __init__(self, store: IterationStore | None = None):
        self.store = store or IterationStore()

    def _component_signals(self) -> list[dict[str, Any]]:
        from core.components import check_components

        signals = []
        for row in check_components(root=self.store.root):
            if row["ok"]:
                continue
            severity = "critical" if row.get("critical") else "major"
            signals.append(
                {
                    "source": "components",
                    "category": "component_health",
                    "key": row["name"],
                    "severity": severity,
                    "summary": f"{row['name']} 未达到运行要求",
                    "evidence": {"component": row["name"], "detail": row["detail"][:300]},
                }
            )
        return signals

    def _delegation_signals(self) -> list[dict[str, Any]]:
        from core.delegations import DelegationStore

        metrics = DelegationStore(
            root=self.store.root, db_path=self.store.db_path
        ).metrics()
        signals = []
        if metrics["overdue_active"]:
            signals.append(
                {
                    "source": "delegations",
                    "category": "overdue",
                    "key": "overdue_active",
                    "severity": "major",
                    "summary": f"{metrics['overdue_active']} 个委托超过约定时间",
                    "evidence": metrics,
                }
            )
        if metrics["duplicate_idempotency_keys"]:
            signals.append(
                {
                    "source": "delegations",
                    "category": "duplicate_action",
                    "key": "duplicate_idempotency",
                    "severity": "critical",
                    "summary": "检测到重复委托动作身份",
                    "evidence": metrics,
                }
            )
        return signals

    def _conversation_signals(self) -> list[dict[str, Any]]:
        path = self.store.root / "data" / "conversation_audit.db"
        if not path.is_file():
            return []
        try:
            db = sqlite3.connect(str(path), timeout=3)
            db.row_factory = sqlite3.Row
            run = db.execute(
                "SELECT id FROM audit_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if run is None:
                return []
            rows = db.execute(
                """
                SELECT severity,issue_type,title,evidence,recommendation
                  FROM audit_issues WHERE run_id=? AND status='open'
                """,
                (run["id"],),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            if "db" in locals():
                db.close()
        result = []
        for row in rows[:50]:
            raw = str(row["severity"] or "").lower()
            severity = (
                "critical"
                if raw in {"critical", "p0"}
                else "major" if raw in {"high", "major", "p1"} else "minor"
            )
            result.append(
                {
                    "source": "conversation_audit",
                    "category": str(row["issue_type"] or "interaction"),
                    "key": f"{row['issue_type']}:{row['title']}",
                    "severity": severity,
                    "summary": _safe(row["title"], "audit title", 300),
                    "evidence": {
                        "recommendation": _safe(
                            row["recommendation"], "recommendation", 500
                        ),
                        "evidence_digest": hashlib.sha256(
                            str(row["evidence"] or "").encode("utf-8")
                        ).hexdigest(),
                    },
                }
            )
        return result

    def run(self, *, create_proposals: bool = True) -> dict[str, Any]:
        raw = (
            self._component_signals()
            + self._delegation_signals()
            + self._conversation_signals()
        )
        signals = [self.store.record_signal(**item) for item in raw]
        proposals = []
        if create_proposals:
            for signal in signals:
                proposal = self.store.propose_from_signal(signal)
                if proposal:
                    proposals.append(proposal[0])
                    sync_proposal_item(
                        proposal[0], store=self.store, send=True
                    )
        return {
            "observed": len(raw),
            "signals": len(signals),
            "proposals": [proposal["id"] for proposal in proposals],
        }


def sync_proposal_item(
    proposal: dict[str, Any],
    *,
    store: IterationStore,
    send: bool = True,
) -> str:
    """Project one pending L3 judgment into the existing Item surface."""
    from core import memorial

    item_id = str(proposal.get("item_id") or "")
    if proposal["status"] != "pending":
        if item_id:
            memorial.resolve(
                item_id,
                "已进入研发队列" if proposal["status"] in {"approved", "queued"} else "不做",
                proposal["review_reason"],
            )
        return item_id
    if item_id and memorial.get_memorial(item_id) is not None:
        return item_id
    options = [
        {
            "key": "approve",
            "label": "进入研发队列",
            "action": {
                "type": "iteration_approve",
                "params": {"id": proposal["id"]},
            },
        },
        {
            "key": "reject",
            "label": "不做",
            "action": {
                "type": "iteration_reject",
                "params": {"id": proposal["id"]},
            },
        },
    ]
    body = (
        f"{proposal['problem']}\n\n"
        f"目标：{proposal['goal']}\n"
        f"产品方向：{proposal['product_direction']}\n"
        f"工程方向：{proposal['technical_direction']}"
    )
    item_id, _ = memorial.create(
        source="iteration-observe",
        title=proposal["title"],
        body=body,
        options=options,
        dedup_key=f"iteration-proposal:{proposal['id']}",
        context=_json(
            {"kind": "iteration_proposal", "proposal_id": proposal["id"]}
        ),
        send=send,
    )
    store.set_item(proposal["id"], item_id)
    return item_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis L3 iteration loop")
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe")
    observe.add_argument("--no-proposals", action="store_true")
    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--status", default="")
    list_cmd.add_argument("--limit", type=int, default=100)
    review = sub.add_parser("review")
    review.add_argument("id")
    review.add_argument("--approve", action="store_true")
    review.add_argument("--reject", action="store_true")
    review.add_argument("--actor", default="owner")
    review.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    try:
        store = IterationStore()
        if args.command == "observe":
            result = DailyObserver(store).run(
                create_proposals=not args.no_proposals
            )
        elif args.command == "list":
            result = {"items": store.list(status=args.status, limit=args.limit)}
        else:
            if args.approve == args.reject:
                raise IterationError("choose exactly one of --approve/--reject")
            result = store.review(
                args.id,
                approved=args.approve,
                actor=args.actor,
                reason=args.reason,
            )
    except IterationError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
