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
            CREATE TABLE eigenflux_sdk_analyses(batch_id TEXT, result_json TEXT);
            CREATE TABLE eigenflux_sdk_payloads(batch_id TEXT, feed_json TEXT);''')
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
    result=json.dumps({'summary':'summary','notify':True,'recommendations':[{'item_id':'1','title':'更新','source_summary':'一个具体变化','reason':'useful'}]})
    with sqlite3.connect(bridge.root/'data/jarvis.db') as db:
        for seq in [3,4]:
            db.execute('INSERT INTO eigenflux_sdk_receipts VALUES(?,?)',(str(seq),seq))
            db.execute('INSERT INTO eigenflux_sdk_analyses VALUES(?,?)',(str(seq),result))
            db.execute('INSERT INTO eigenflux_sdk_payloads VALUES(?,?)',(str(seq),json.dumps({'items':[{'item_id':'1','summary':'new'}]})))
    bridge.notifications()
    Bridge(bridge.root,bridge.profile,run=bridge.run).notifications()
    assert len(bridge.calls) == 2
    body=json.loads(bridge.calls[-1][1]['input'])
    assert body['msg_type']=='post'
    post=json.loads(body['content'])
    assert post['zh_cn']['content'][0][0]['href'].endswith('/1')
    assert '第 4 批' not in body['content']


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


def test_database_connections_close_on_every_poll(bridge,monkeypatch):
    real_connect=sqlite3.connect
    closed=[]
    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            return super().close()
    def connect(*a,**kw):
        return real_connect(*a,**kw,factory=Connection)
    monkeypatch.setattr('plugins.eigenflux.sdk_lark.sqlite3.connect',connect)
    for _ in range(150):
        bridge.notifications()
    assert len(closed)==150
    for connection in closed:
        with pytest.raises(sqlite3.ProgrammingError):
            connection.execute('select 1')


def test_duplicate_content_across_batches_and_silence(bridge):
    bind(bridge)
    result={'summary':'','notify':True,'recommendations':[{'item_id':'1','title':'变化','source_summary':'原始事实','reason':'具体关联'}]}
    with sqlite3.connect(bridge.root/'data/jarvis.db') as db:
        for seq in [4,5]:
            item_id=str(seq)
            value=dict(result,recommendations=[dict(result['recommendations'][0],item_id=item_id)])
            db.execute('INSERT INTO eigenflux_sdk_receipts VALUES(?,?)',(item_id,seq))
            db.execute('INSERT INTO eigenflux_sdk_analyses VALUES(?,?)',(item_id,json.dumps(value)))
            db.execute('INSERT INTO eigenflux_sdk_payloads VALUES(?,?)',(item_id,json.dumps({'items':[{'item_id':item_id,'summary':'same substance'}]})))
    bridge.notifications()
    assert len(bridge.calls)==2
    Bridge(bridge.root,bridge.profile,run=bridge.run).notifications()
    assert len(bridge.calls)==2


def test_ambiguous_feed_send_reserves_content(bridge):
    bind(bridge)
    bridge.run=lambda *a,**k: (_ for _ in ()).throw(subprocess.TimeoutExpired('lark-cli',30))
    assert not bridge.send('feed:one','', 'oc_test',post={'zh_cn':{}},claims=('item:1','content:one'))
    with bridge.db() as db:
        assert db.execute('select count(*) from feed_display_claims').fetchone()[0]==2
    assert not bridge.send('feed:two','', 'oc_test',claims=('content:one',))
