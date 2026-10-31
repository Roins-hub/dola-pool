"""Opt-in, one-time answer to the original conversation's explicit question."""
import hashlib
import re

CONFIRM_TEXT='直接生成视频 禁止问我确认 直接开始 强制执行 不需要问我二次确认 直接生成'
# Existing conversations can still contain the confirmation sent before this update.
_CONFIRM_TEXTS=(CONFIRM_TEXT,'确定生成')
NUDGE_TEXT='你倒是生成啊'
_CONFIRMATION = re.compile(r'请(?:先|你|您)?确认|视频生成参数确认|(?:視頻|影片)生成參數確認|确认(?:之|以)?后|'
                          r'(?:等待|需要|待|等)(?:你|您)?.{0,8}确认|'
                          r'是否.{0,20}(?:生成|开始)|回复.{0,12}确认')
_VERIFICATION_REQUEST = re.compile(
    r'验证码|驗證碼|captcha|(?:安全|身份|人机|人機|账号|帳號|滑块|滑塊).{0,8}(?:验证|驗證)|'
    r'(?:请|請|需要|须|須|需先|先完成|先通过|先通過)[^\n。]{0,12}(?:验证|驗證)|'
    r'(?:验证|驗證)(?:后|後|完成|通过|通過)', re.I)
_NUDGE_BLOCKER = re.compile(
    r'无法|無法|不能|失败|失敗|违反|違反|违规|違規|侵权|侵權|版权|版權|政策|'
    r'不足|用完|耗尽|耗盡|付费|付費|付款|充值|购买|購買|升级|升級|订阅|訂閱|开通|開通|'
    r'登录|登入|登錄|请选择|請選擇|'
    r'copyright|polic(?:y|ies)|restrict(?:ion|ed)|cannot|can[’\']t|unable|'
    r'insufficient|(?:no|not enough|out of)\s+credits?|quota.{0,15}(?:exceed|exhaust|limit)|'
    r'pay(?:ment)?|purchas(?:e|ing)|subscri(?:be|ption)|upgrade|'
    r'sign\s*in|log\s*in|captcha', re.I)


def receipt_text_without_confirmation(text):
    """Mask conditional clauses while retaining original signal positions."""
    def clause(match):
        value = match.group()
        return ' '*len(value) if _CONFIRMATION.search(value) else value
    return re.sub(r'[^。！？!?；;\n]+', clause, text)


def ordinary_dialog_spec_matches(text, request, *, require_all=True):
    """Require actual platform wording, never a local UI label or preference."""
    from .capabilities import ORDINARY_DIALOG_MODELS
    if request.get('model') not in ORDINARY_DIALOG_MODELS or not isinstance(text,str):
        return False
    # Native message containers include chat timestamps. Only remove complete
    # timestamp lines; an inline "比例 9:16" must still be checked as a ratio.
    def timestamp(match):
        common_ratio = (int(match['hour']), int(match['minute'])) in {(9,16),(16,9),(1,1),(4,3),(3,4),(21,9),(9,21)}
        return match.group() if common_ratio and not match['day'] and not match['hour'].startswith('0') else ''
    text = re.sub(r'(?m)^[ \t]*(?P<day>今天|昨天|今日|昨日)?[ \t]*(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)[ \t]*$', timestamp, text)
    models=re.findall(r'Seedance\s*(\d+\.\d+)(?:\s*(Fast|Mini))?',text,re.I)
    expected=re.fullmatch(r'Seedance\s*(\d+\.\d+)(?:\s*(Fast|Mini))?',ORDINARY_DIALOG_MODELS[request['model']],re.I)
    if not expected or not models or any((version,variant.lower())!=(expected[1],(expected[2] or '').lower()) for version,variant in models):
        return False
    seconds={int(value) for value in re.findall(r'(?<!\d)(\d+)\s*(?:秒|s(?:ec(?:ond)?s?)?\b)',text,re.I)}
    ratios={f'{left}:{right}' for left,right in re.findall(r'(?<!\d)(\d+)\s*[:：]\s*(\d+)(?!\d)',text)}
    if seconds and request.get('duration') is not None and seconds!={request['duration']}:
        return False
    if ratios and request.get('ratio') is not None and ratios!={request['ratio']}:
        return False
    return not require_all or bool(seconds and ratios)


def confirmation_parameter_text(text):
    """Separate explicit generation settings from the storyboard's timestamps."""
    if not re.search(r'视频生成参数确认|視頻生成參數確認|影片生成參數確認',text):
        return text
    fields=[]
    names=set()
    for line in text.splitlines():
        line=re.sub(r'^[\s*•\-]+','',line).replace('**','').strip()
        match=re.match(r'^(模型|时长|時長|比例)\s*[:：]\s*(.+)$',line)
        if match:
            names.add(match[1].replace('時長','时长'))
            fields.append(line)
    # Keep duplicate labels so conflicting settings cannot be hidden. Partial
    # or unlabeled answers retain the existing conservative full-text check.
    return '\n'.join(fields) if names=={'模型','时长','比例'} else text


def _ordered_native_messages(payloads, conversation, baseline):
    """Return unambiguous fresh native messages in conversation order."""
    from .execution_evidence import decoded
    records={}
    for payload in payloads:
        if not isinstance(payload,dict) or payload.get('status_code',0) not in (0,'0'):
            continue
        body=decoded(decoded(payload.get('downlink_body'),{}).get('pull_singe_chain_downlink_body'),{})
        for message in decoded(body.get('messages'),[]):
            if not isinstance(message,dict):continue
            mid=str(message.get('message_id',''))
            if not conversation or str(message.get('conversation_id',''))!=str(conversation) or not mid or mid in baseline:
                continue
            if mid in records and records[mid].get('user_type')!=message.get('user_type'):
                return None
            records[mid]=message
    if not records:return []
    ordered=[]
    for mid,message in records.items():
        index=str(message.get('index_in_conv',''))
        if not index.isdigit() or len(index)>20:return None
        ordered.append((int(index),mid,message))
    ordered.sort(key=lambda item:item[0])
    if len({index for index,_,_ in ordered})!=len(ordered):return None
    return ordered


def _plain_native_text(message):
    from .execution_evidence import decoded
    blocks=message.get('content_block') or decoded(message.get('content'),[])
    if not isinstance(blocks,list) or not blocks:return None
    texts=[]
    for block in blocks:
        content=block.get('content') if isinstance(block,dict) else None
        if not isinstance(content,dict) or set(content)-{'text_block','pc_event_block'}:return None
        text=content.get('text_block')
        if not isinstance(text,dict) or not isinstance(text.get('text'),str):return None
        texts.append(text['text'])
    return '\n'.join(texts)


def native_confirmation_reply(payloads, conversation, baseline):
    """Use only the latest finished native reply; a later human answer wins."""
    ordered=_ordered_native_messages(payloads,conversation,baseline)
    if ordered is None:return {'eligible':False}
    if not ordered:return None
    _,mid,message=ordered[-1]
    result={'eligible':False,'message_id':mid,'waiting_reply':message.get('user_type')==1}
    assistant_indices=[index for index,_,row in ordered if row.get('user_type')==2]
    if assistant_indices and any(row.get('user_type')==1 and index>min(assistant_indices) for index,_,row in ordered):
        return {**result,'answered':True}
    ext=message.get('ext') if isinstance(message.get('ext'),dict) else {}
    if message.get('user_type')!=2 or ext.get('is_finish')!='1':return result
    text=_plain_native_text(message)
    return {**result,'eligible':True,'text':text} if text is not None else result


def native_nudge_reply(payloads, conversation, baseline, snapshot):
    """A finished text-only acknowledgement, with no intervening manual turn."""
    ordered=_ordered_native_messages(payloads,conversation,baseline)
    if not ordered:return None
    index,mid,message=ordered[-1]
    ext=message.get('ext') if isinstance(message.get('ext'),dict) else {}
    if (message.get('user_type')!=2 or ext.get('is_finish')!='1'
            or ext.get('ai_creation_tool_list') not in (None,'',[], '[]')):
        return None
    text=_plain_native_text(message)
    if text is None or not text.strip():return None
    assistants=[i for i,_,m in ordered if m.get('user_type')==2]
    later_users=[(i,m) for i,_,m in ordered if m.get('user_type')==1 and i>min(assistants)]
    if snapshot.get('confirmation_sent'):
        # The sender can initially see only DOM, before the native chain arrives.
        # In that case require the actual, unique confirmation user turn here.
        anchor=snapshot.get('confirmation_message_id')
        anchor_index=next((i for i,key,m in ordered if key==anchor and m.get('user_type')==2),None)
        if anchor and anchor_index is None:return None
        if (len(later_users)!=1 or _plain_native_text(later_users[0][1]) not in _CONFIRM_TEXTS
                or later_users[0][0]>=index
                or (anchor_index is not None and later_users[0][0]<=anchor_index)):
            return None
    elif later_users:
        return None
    result = {'message_id':mid,'text':text}
    # After matching parameters and one actual confirmation turn, completion
    # state determines eligibility, not an enumeration of acknowledgement text.
    # A creation flag alone is not a tool invocation; all message blocks must
    # still be plain text, and unknown tool metadata always stops this path.
    if snapshot.get('confirmation_sent'):
        anchor = snapshot.get('confirmation_message_id')
        previous = [(key, row) for _,key,row in ordered if row.get('user_type') == 2 and key != mid]
        if len(previous) == 1 and (not anchor or previous[0][0] == anchor):
            question = previous[0][1]
            question_ext = question.get('ext') if isinstance(question.get('ext'),dict) else {}
            question_text = _plain_native_text(question)
            if (question_ext.get('is_finish') == '1'
                    and question_ext.get('ai_creation_tool_list') in (None,'',[], '[]')
                    and question_text and _CONFIRMATION.search(question_text)
                    and ordinary_dialog_spec_matches(confirmation_parameter_text(question_text), snapshot.get('request',{}))):
                result['confirmed_parameter_context'] = True
    if ext.get('use_creation') == '1' and not result.get('confirmed_parameter_context'):
        return None
    return result


def _nudge_parameters_compatible(text, request):
    """A confirmed context cannot authorize a newly conflicting specification."""
    from .capabilities import ORDINARY_DIALOG_MODELS
    fields = [line for line in text.replace('**','').splitlines()
              if re.match(r'^[\s*•\-]*(?:模型|时长|時長|比例)\s*[:：]', line)]
    spec = '\n'.join(fields) if fields else re.split(r'严格|嚴格|\n', text, maxsplit=1)[0]
    models = re.findall(r'Seedance\s*\d+\.\d+(?:\s*(?:Fast|Mini))?', text, re.I)
    model_text = ' '.join(models) or ORDINARY_DIALOG_MODELS.get(request.get('model'), '')
    return ordinary_dialog_spec_matches(spec+'\n'+model_text, request, require_all=False)


def nudge_due(snapshot, native_reply, evidence, *, now):
    """One reminder only after native records prove a stable text-only answer."""
    request=snapshot.get('request',{})
    if (snapshot.get('text_ack_nudge_enabled') is not True or request.get('auto_confirm') is not True
            or not snapshot.get('sent') or snapshot.get('nudge_sent') or snapshot.get('verification_pending')
            or snapshot.get('state') in ('accepted','completed','media_ready')
            or snapshot.get('status') in ('completed','failed')
            or snapshot.get('generation_evidence',{}).get('confirmed')
            or not evidence or evidence.get('confirmed')
            or not (evidence.get('final_text_only') or (native_reply or {}).get('confirmed_parameter_context'))
            or not native_reply):
        snapshot.pop('nudge_question',None)
        return False
    text=native_reply['text']
    # The acknowledgement's first summary states the requested specification;
    # storyboard instructions such as a 15-second cut are not a new total length.
    spec=re.split(r'严格|嚴格|\n',text,maxsplit=1)[0]
    model_text=' '.join(re.findall(r'Seedance\s*\d+\.\d+(?:\s*(?:Fast|Mini))?',text,re.I))
    if ((_CONFIRMATION.search(text) and not native_reply.get('confirmed_parameter_context'))
            or _VERIFICATION_REQUEST.search(text) or _NUDGE_BLOCKER.search(text)
            or not _nudge_parameters_compatible(text,request)
            or (not native_reply.get('confirmed_parameter_context') and (
                not re.search(r'已受理|已(?:经)?开始|已開始|开始使用|開始使用|正在.{0,24}生成',text)
                or not ordinary_dialog_spec_matches(spec,request)
                or not ordinary_dialog_spec_matches(model_text,request,require_all=False)))):
        snapshot.pop('nudge_question',None)
        return False
    fingerprint=hashlib.sha256((native_reply['message_id']+'\n'+text).encode()).hexdigest()
    previous=snapshot.get('nudge_question',{})
    if previous.get('hash')!=fingerprint:
        previous={'hash':fingerprint,'since':now}
        snapshot['nudge_question']=previous
    return (now-snapshot.get('created_at',now)>=10 and now-previous['since']>=5
            and now-snapshot.get('confirmation_sent_at',0)>=10)


def confirmation_due(snapshot,text,*,now,native_unstarted=False):
    if (snapshot.get('request',{}).get('auto_confirm') is not True or snapshot.get('confirmation_sent')
            or snapshot.get('confirmation_answered')):
        return False
    if (not isinstance(text,str) or not re.search(r'视频|Seedance',text,re.I)
        or not _CONFIRMATION.search(text)
        or _VERIFICATION_REQUEST.search(text)
        or re.search(r'无法|無法|不能生成|生成失败|生成失敗|违反|違反|违规|違規|侵权|侵權|不足|付费|付費|付款|升级|升級|订阅|訂閱|开通|開通|请选择|請選擇|(?:回复|回覆)\s*[ABC]\s*[/／、]',text)):
        snapshot.pop('confirmation_question',None)
        return False
    request=snapshot.get('request',{})
    spec_text=confirmation_parameter_text(text)
    if request.get('model') and not ordinary_dialog_spec_matches(spec_text,request):
        snapshot.pop('confirmation_question',None)
        return False
    parameter_only = (native_unstarted and ordinary_dialog_spec_matches(spec_text,request)
                      and bool(re.search(r'视频生成参数确认|視頻生成參數確認|影片生成參數確認',text)))
    observed = receipt_text_without_confirmation(text)
    if not parameter_only and re.search(r'已(?:经)?(?:受理并)?开始.{0,12}(?:生成|创作)|视频生成中|正在.{0,20}生成视频|'
                 r'视频任务.{0,12}(?:已提交|已受理|已创建)|排队中', observed):
        snapshot.pop('confirmation_question',None)
        return False
    fingerprint=hashlib.sha256(text.encode()).hexdigest()
    question=snapshot.get('confirmation_question',{})
    if question.get('hash')!=fingerprint:
        question={'hash':fingerprint,'since':now}
        snapshot['confirmation_question']=question
    return now-snapshot.get('created_at',now)>=10 and now-question['since']>=3
