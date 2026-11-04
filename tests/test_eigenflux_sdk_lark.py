import hashlib
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from plugins.eigenflux.sdk_lark import Bridge


def event(text, mid='om_one', owner='ou_owner', chat='oc_test', kind='p2p'):
    return {'header': {'event_type': 'im.message.receive_v1'}, 'event': {
        'sender': {'sender_type': 'user', 'sender_id': {'open_id': owner}},
        'message': {'message_id': mid, 'chat_id': chat, 'chat_type': kind,
                    'message_type': 'text', 'content': json.dumps({'text': text})}}}


@pytest.fixture
def bridge(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'sdk').mkdir()
    (tmp_path / 'sdk/state.json').write_text('{"last_sequence":3}')
    (tmp_path / 'context.json').write_text('{"summary":"SDK testing"}')
    (tmp_path / 'lark-pairing.json').write_text(json.dumps({
        'code_hash': hashlib.sha256(b'private-code').hexdigest(), 'expires_at': 200}))
    with sqlite3.connect(tmp_path / 'data/jarvis.db') as db:
        db.executescript('''CREATE TABLE eigenflux_sdk_receipts(batch_id TEXT, sequence INTEGER);
            CREATE TABLE eigenflux_sdk_analyses(batch_id TEXT, result_json TEXT);''')
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"code":0,"data":{"message_id":"om_receipt"}}')
    b = Bridge(tmp_path, 'test-profile', run=run, clock=lambda:100)
    b.calls = calls
    return b


def bind(b):
    assert b.handle(event('/bind private-code'))


def test_pairing_requires_secret_and_single_private_owner(bridge):
    b = bridge
    assert not b.handle(event('hello'))
    assert not b.handle(event('/bind wrong'))
    assert not b.handle(event('/bind private-code', kind='group'))
    assert b.bound() is None and not b.calls
    bind(b)
    assert b.bound()['owner'] == 'ou_owner' and b.bound()['baseline'] == 3
    assert not b.handle(event('/bind private-code'))
    assert not b.handle(event('hello', owner='ou_other'))
    assert not b.handle(event('hello', chat='oc_other'))
    assert len(b.calls) == 1


def test_expired_pairing_and_malformed_events(bridge):
    bridge.clock = lambda:201
    assert not bridge.handle(event('/bind private-code'))
    assert not bridge.handle({})
    e = event('hello'); e['event']['message']['content'] = '{'
    assert not bridge.handle(e)
    assert not bridge.handle(event('x'*5001))


def test_status_duplicate_and_private_body(bridge):
    bind(bridge)
    assert bridge.handle(event('/status', mid='om_status'))
    assert not bridge.handle(event('/status', mid='om_status'))
    args, kwargs = bridge.calls[-1]
    assert args[1:3] == ['--profile','test-profile']
    assert kwargs['input'] not in ' '.join(args)
    assert '/open-apis/im/v1/messages/om_status/reply' in args
    assert json.loads(kwargs['input'])['uuid']
    with bridge.db() as db:
        assert db.execute("SELECT message_id FROM effects WHERE key='reply:om_status'").fetchone()[0] == 'om_receipt'


@pytest.mark.parametrize('failure', ['timeout','invalid','missing_receipt','rejected'])
def test_ambiguous_send_never_automatically_repeats(bridge, failure):
    bind(bridge)
    calls = []
    def bad(*args, **kwargs):
        calls.append(1)
        if failure == 'timeout': raise subprocess.TimeoutExpired('lark-cli',30)
        return SimpleNamespace(returncode=0,stdout={'invalid':'not-json','missing_receipt':'{"code":0}',
            'rejected':'{"code":1,"data":{"message_id":"om_bad"}}'}[failure])
    bridge.run = bad
    assert not bridge.send('one', 'text', 'oc_test')
    assert not bridge.send('one', 'text', 'oc_test')
    assert len(calls) == 1
    with bridge.db() as db:
        assert db.execute("SELECT status FROM effects WHERE key='one'").fetchone()[0] == 'unknown'


def test_interrupted_intent_is_held_on_restart(bridge):
    with bridge.db() as db:
        db.execute("INSERT INTO effects VALUES('one','unknown','')")
    b = Bridge(bridge.root,bridge.profile,run=bridge.run)
    assert not b.send('one','text','oc_test')
    assert not bridge.calls


def test_text_model_receipt_and_duplicate_event(bridge):
    bind(bridge)
    model_calls = []
    def model(prompt, **kwargs):
        model_calls.append(prompt)
        return SimpleNamespace(status='succeeded',call_id='synthetic-call',text='文字回复')
    bridge.model = model
    assert bridge.handle(event('hello',mid='om_chat'))
    assert not bridge.handle(event('hello',mid='om_chat'))
    assert len(model_calls) == 1
    with bridge.db() as db:
        assert db.execute('SELECT call_id FROM turns').fetchone()[0] == 'synthetic-call'


def test_model_failure_never_claims_success(bridge):
    bind(bridge)
    bridge.model = lambda *a,**kw:SimpleNamespace(status='failed',call_id='',text='')
    assert bridge.handle(event('hello',mid='om_failed'))
    with bridge.db() as db:
        assert db.execute('SELECT count(*) FROM turns').fetchone()[0] == 0
    assert '未成功' in json.loads(json.loads(bridge.calls[-1][1]['input'])['content'])['text']


def test_notifications_skip_history_and_deduplicate_after_restart(bridge):
    bind(bridge)
    result=json.dumps({'summary':'summary','notify':True,'recommendations':[{'item_id':'1','reason':'useful'}]})
    with sqlite3.connect(bridge.root/'data/jarvis.db') as db:
        for seq in [3,4]:
            db.execute('INSERT INTO eigenflux_sdk_receipts VALUES(?,?)',(str(seq),seq))
            db.execute('INSERT INTO eigenflux_sdk_analyses VALUES(?,?)',(str(seq),result))
    bridge.notifications()
    Bridge(bridge.root,bridge.profile,run=bridge.run).notifications()
    assert len(bridge.calls) == 2
    assert '第 4 批' in json.loads(json.loads(bridge.calls[-1][1]['input'])['content'])['text']


def test_feishu_rich_text_binding_and_reply(bridge):
    e = event('unused')
    e['event']['message'].update(message_type='post',content=json.dumps({'title':'','content':[[{'tag':'text','text':'/bind private-code'}]],'content_v2':[]}))
    assert bridge.handle(e)
    e = event('unused',mid='om_post')
    e['event']['message'].update(message_type='post',content=json.dumps({'title':'','content':[[{'tag':'text','text':'/status'}]]}))
    assert bridge.handle(e)
    assert len(bridge.calls) == 2


@pytest.mark.parametrize('content', [
    {'content':[[{'tag':'img','image_key':'synthetic'}]]},
    {'content':[[{'tag':'text','text':123}]]},
    {'content':'invalid'},
])
def test_unsupported_post_elements_fail_closed(bridge,content):
    e=event('unused');e['event']['message'].update(message_type='post',content=json.dumps(content))
    assert not bridge.handle(e)
    assert not bridge.calls
