"""Terminal review receipts, scoped to the original conversation's latest reply."""
import re
from .confirmation import _ordered_native_messages, _plain_native_text

_REFUSAL = re.compile(
    r'(?:生成的?(?:视频|影片|視頻)|(?:视频|影片|視頻)(?:生成)?).{0,18}'
    r'(?:涉及|触发|觸發).{0,12}(?:版权|版權|内容政策|內容政策|内容安全|內容安全).{0,8}'
    r'(?:限制|违规|違規|不符|风险|風險)|'
    r'(?:视频|影片|視頻|生成|内容|內容).{0,12}(?:未通过|未通過|未能通过|未能通過).{0,8}(?:审核|審核)|'
    r'(?:内容|內容|版权|版權|安全)(?:审核|審核)(?:失败|失敗|不通过|不通過)|'
    r'(?:can[’\']t|cannot|unable to)\s+generate.{0,80}(?:copyright|polic(?:y|ies))', re.I)


def review_rejection(text):
    # Quoted past errors, instructions and hypothetical warnings are not a new
    # rejection. Keep pending review and plain mentions of copyright neutral.
    text=re.sub(r'```[\s\S]*?```|“[^”]*”|「[^」]*」|"[^"\n]*"', '', text)
    for sentence in re.split(r'[。！？!?；;\n]',text):
        if re.search(r'如果|假如|倘若|万一|萬一|如遇|\bif\b',sentence,re.I):
            continue
        if _REFUSAL.search(sentence):return True
    return False


def latest_review_rejection(payloads, conversation, baseline):
    ordered=_ordered_native_messages(payloads,conversation,baseline)
    if not ordered:return None
    _,mid,message=ordered[-1]
    ext=message.get('ext') if isinstance(message.get('ext'),dict) else {}
    if message.get('user_type')!=2 or str(ext.get('is_finish'))!='1':return None
    text=_plain_native_text(message)
    if text and review_rejection(text):
        return {'text':text,'message_id':mid,'conversation_id':conversation}
    return None
