import re
import failure_text as ft
from protocol import dola_pure_api as dola
from store import TaskStore
from test_studio_batch_progress import studio
import server
import pytest

CJK=re.compile(r'[\u3400-\u9fff]')

@pytest.mark.parametrize('has_video',[True,False])
def test_delivered_clip_survives_other_generation_failures(monkeypatch,has_video):
    refusal='你的内容已生成完毕，其中 2 个视频生成失败，失败视频的生成额度未扣除。'
    data={'text_block':{'text':refusal}}
    if has_video:
        data['video']={'vid':'vfixture','download_url':'https://fixture.invalid/video.mp4'}
    monkeypatch.setattr(dola,'fetch_single_chain',lambda *args:data)
    result=dola.inspect_video_once(None,{},'existing-conversation')
    assert result.status==('succeeded' if has_video else 'generation_voided')
    assert refusal in result.failure_reasons
    if has_video:assert result.vids==['vfixture'] and result.urls

def test_protocol_keeps_original_and_does_not_inject_chinese():
    for original in ('Mèo chạy qua sân.', 'A black cat walks past a mirror.'):
        enriched=dola.enrich_multi_image_prompt(original,2)
        submitted=dola.build_prompt_text(enriched,'9:16')
        assert original in submitted and not CJK.search(submitted)
        assert 'tiếng Việt' in submitted and '9:16' in submitted
        assert dola.is_prompt_echo_text(submitted)
        payload,_,_=dola.build_payload([],original,{'fp':'fixture'},30,'seedance-2.5','9:16')
        assert payload['chat_ability']==dola.build_ability('seedance-2.5',30,'9:16')
    assert dola.is_prompt_echo_text('生成视频：old conversation')
    original='猫在窗边'
    assert original in dola.build_prompt_text(original,'16:9')

def test_actual_quota_refusal_reports_required_and_remaining_in_vietnamese():
    text='本次视频生成需要消耗 24 个视频生成额度，今日剩余 2 个视频生成额度 ，无法生成该视频。请尝试降低配置后重试。'
    result=ft.present(text,category='unknown')
    assert result['category']=='quota' and '24' in result['message_vi'] and '2' in result['message_vi']
    assert not CJK.search(result['message_vi']+result['next_action_vi'])
    assert ft.present_update({'status':'rate_limited','texts':[text],'failure_reasons':[text]})['category']=='quota'

def test_voided_generation_and_ack_translate_actual_provider_details():
    result=ft.present('Dola đã kết thúc tác vụ: 视频生成失败，生成额度未扣除。')
    assert 'chưa trừ' in result['message_vi'] and result['category']=='generation'
    ack=ft.present_update({'status':'pending','texts':['本次使用 **Seedance 2.0 Fast** 生成，将消耗 3 个视频生成额度，预计等待 10 分钟。今日剩余 1 个视频生成额度。'],'wait_minutes':'10分钟'})
    assert '3' in ack['message_vi'] and '1' in ack['message_vi'] and ack['wait_vi']=='10 phút'
    assert not CJK.search(ack['message_vi'])

@pytest.mark.asyncio
async def test_running_job_does_not_disappear_behind_newer_history(studio):
    store,_=studio
    store.create('running','seedance-2.5','original','9:16',30)
    store.update('running',status='processing',phase='generating',conversation_id='old-conversation',account='one')
    for i in range(4):
        store.create('new'+str(i),'seedance-2.0','new draft','16:9',5)
        store.update('new'+str(i),status='completed')
    response=await server.admin_tasks(limit=1,x_admin_key='test')
    assert [r['id'] for r in response['running_tasks']]==['running']
    assert response['running_tasks'][0]['duration']==30 and response['running_tasks'][0]['ratio']=='9:16'
