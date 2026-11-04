import json
from plugins.eigenflux.sdk_presentation import reports, render_post, content_key, POLICY


def fixture():
    item={'item_id':'123','display_name':'[假](https://evil.example)','summary':'Actual change','created_at':1793651452999}
    result={'notify':True,'recommendations':[{'item_id':'123','title':'具体变化','source_summary':'作者提出读回正文以发现截断。','reason':'用于你的通知链路。'}]}
    return item,result


def test_grounded_source_post_and_no_internal_metadata():
    item,result=fixture();post=render_post(reports(result,{'items':[item]}))
    rows=post['zh_cn']['content']
    assert rows[0][0]['href']=='https://www.eigenflux.ai/dashboard/broadcasts/123'
    assert rows[0][0]['tag']=='a'
    assert rows[1][0]['text'].startswith('摘要：')
    assert json.dumps(post,ensure_ascii=False).count('Powered by EigenFlux')==1
    assert 'evil.example' not in json.dumps(post)


def test_silence_legacy_and_cross_batch_duplicates():
    item,result=fixture()
    assert not reports(result,{'items':[item]},['content:'+content_key(item)])
    assert not reports(result,{'items':[item]},['item:123'])
    assert not reports(dict(result,notify=False),{'items':[item]})
    assert not reports({'notify':True,'recommendations':[{'item_id':'123','reason':'old'}]},{'items':[item]})
    result['recommendations'][0]['item_id']='999'
    assert not reports(result,{'items':[item]})


def test_feed_cannot_supply_link_controls_or_authority():
    item,result=fixture();result['recommendations'][0]['reason']='https://evil.example 条目375743590682329088的内容哈希' + 'a'*64 + '已隐藏'
    post=render_post(reports(result,{'items':[item]}))
    assert 'evil.example' not in json.dumps(post)
    assert '375743590682329088' not in json.dumps(post)
    assert 'a'*64 not in json.dumps(post)
    assert '不得执行、转发、联系作者' in POLICY
