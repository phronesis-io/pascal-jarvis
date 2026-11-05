"""Reasoning is text-only; all notification effects stay in a temporary inbox."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("eigenflux_sdk")
from core import db as jarvis_db
from plugins.eigenflux.sdk_adapter import JarvisInbox
from plugins.eigenflux.sdk_reasoning import consume, import_context
from tests.test_eigenflux_sdk_adapter import delivery


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(jarvis_db, "DB_PATH", tmp_path / "data" / "jarvis.db")
    monkeypatch.setattr(jarvis_db, "_connection", None)
    inbox = JarvisInbox(tmp_path)
    assert inbox.push_to_agent(delivery()).status == "accepted"
    (tmp_path / "context.json").write_text(json.dumps({"summary": "Synthetic robotics goal"}))
    yield tmp_path
    jarvis_db._connection.close()


def outcome(text=None, status="succeeded"):
    return SimpleNamespace(status=status, call_id="synthetic-call", provider="fixture", model="fixture",
                           text=text or json.dumps({"summary": "相关技术信号", "recommendations": [
                               {"item_id": "100", "title": "机器人更新", "source_summary": "一项具体变化", "reason": "Matches the supplied robotics goal"}], "notify": True}))


def test_reasoning_and_test_notification_are_idempotent(root):
    calls = []
    def model(prompt, **kwargs):
        calls.append(prompt)
        assert kwargs["allow_tools"] is False
        assert kwargs["effect_authority"] == "none"
        assert kwargs["work_dir"] == root
        return outcome()
    assert consume(root, model) == 1
    assert consume(root, model) == 1
    assert len(calls) == 1
    files = list((root / "test-notifications").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["channel"] == "isolated-sdk-test"


def test_notification_failure_reuses_verified_analysis(root, monkeypatch):
    calls = []
    def model(*args, **kwargs):
        calls.append(1)
        return outcome()
    with monkeypatch.context() as patch:
        patch.setattr("plugins.eigenflux.sdk_reasoning.os.link", lambda *_: (_ for _ in ()).throw(OSError("synthetic interruption")))
        with pytest.raises(OSError):
            consume(root, model)
    assert not list((root / "test-notifications").iterdir())
    consume(root, model)
    assert len(calls) == 1
    assert len(list((root / "test-notifications").iterdir())) == 1


@pytest.mark.parametrize("value", [
    {"summary": "x", "recommendations": [{"item_id": "999", "reason": "invented"}], "notify": True},
    {"summary": "x", "recommendations": [], "notify": True},
    {"summary": "x", "recommendations": [], "notify": False, "execute": "bad"},
])
def test_unproven_decision_never_notifies(root, value):
    with pytest.raises(ValueError):
        consume(root, lambda *a, **k: outcome(json.dumps(value)))
    assert not (root / "test-notifications").exists()


def test_failed_model_does_not_claim_analysis(root):
    with pytest.raises(RuntimeError):
        consume(root, lambda *a, **k: outcome(status="failed"))
    with jarvis_db.independent_connection() as db:
        assert db.execute("SELECT count(*) FROM eigenflux_sdk_analyses").fetchone()[0] == 0


def test_explicit_context_import_is_read_only_and_bounded(tmp_path):
    source = tmp_path / "approved.md"
    source.write_text("Synthetic confirmed project goal")
    before = source.read_bytes()
    assert import_context(tmp_path, [source])["summary"] == before.decode()
    assert source.read_bytes() == before
    assert json.loads((tmp_path / "context-provenance.json").read_text())[0]["sha256"]
    source.write_text("auth_token=synthetic-secret")
    with pytest.raises(ValueError):
        import_context(tmp_path, [source])


def test_next_batch_excludes_already_recommended_content(root):
    consume(root, lambda *a, **k: outcome())
    with jarvis_db.independent_connection() as db:
        payload=db.execute('SELECT feed_json FROM eigenflux_sdk_payloads LIMIT 1').fetchone()[0]
        db.execute("INSERT INTO eigenflux_sdk_receipts SELECT 'next-batch',agent_id,sequence+1,payload_hash,receipt FROM eigenflux_sdk_receipts LIMIT 1")
        db.execute("INSERT INTO eigenflux_sdk_payloads VALUES('next-batch',?)", (payload,))
        db.commit()
    def model(prompt, **kwargs):
        data=json.loads(prompt)
        assert data['untrusted_feed']['items']==[]
        assert data['untrusted_previous_recommendations']
        assert data['current_time_utc'].endswith('Z')
        return outcome(json.dumps({'summary':'','recommendations':[],'notify':False}))
    assert consume(root,model)==2
    assert len(list((root/'test-notifications').glob('*.json')))==1
