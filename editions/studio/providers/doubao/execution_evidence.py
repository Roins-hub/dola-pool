"""Execution proof from observed native chain records, never from chat prose."""
import json
import re

MODEL_IDS = {'dialog_seedance_2.0_fast': 'seedance_v2.0', 'dialog_seedance_2.5': 'seedance_v2.5'}
TASK_MODELS = {'seedance_v20_fast_flow': 'dialog_seedance_2.0_fast'}
SUBMITTED_TITLES = {'影片生成已提交', '视频生成已提交'}


def decoded(value, default):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return default
    return value if isinstance(value, type(default)) else default


def native_execution_evidence(payloads, conversation, baseline, request):
    """Accept only the known /im/chain/single envelope and fresh bot messages.

    User text, quoted JSON, ability parameters echoed by a user message, and
    generic DOM text are deliberately ineligible. Missing records mean unknown.
    """
    records = {}
    for payload in payloads:
        if not isinstance(payload, dict) or payload.get('status_code', 0) not in (0, '0'):
            continue
        body = decoded(decoded(payload.get('downlink_body'), {}).get('pull_singe_chain_downlink_body'), {})
        for message in decoded(body.get('messages'), []):
            if not isinstance(message, dict):
                continue
            mid = str(message.get('message_id', ''))
            if (str(message.get('conversation_id', '')) != str(conversation) or not conversation
                    or message.get('user_type') != 2 or not mid or mid in baseline):
                continue
            records[mid] = message
    proof = {'confirmed': False, 'source': 'native_chain', 'conversation_id': conversation,
             'message_ids': [], 'task_ids': [], 'kinds': [], 'final_text_only': False,
             'parameters_match': None, 'request_parameters_echoed': False, 'echoed_parameters': []}
    plain, opaque, texts, parameters, task_models = [], [], [], [], []
    for mid, message in records.items():
        ext = message.get('ext') if isinstance(message.get('ext'), dict) else {}
        blocks = message.get('content_block') or decoded(message.get('content'), [])
        if not isinstance(blocks, list):
            blocks = []
        contents = [b['content'] for b in blocks if isinstance(b, dict) and isinstance(b.get('content'), dict)]
        texts.extend(c['text_block']['text'] for c in contents if isinstance(c.get('text_block'), dict)
                     and isinstance(c['text_block'].get('text'), str))
        ability = decoded(ext.get('chat_ability'), {})
        params = decoded(ability.get('ability_param'), {})
        if ability.get('ability_type') == 17 and params:
            proof['request_parameters_echoed'] = True
            echoed = {k:params.get(k) for k in ('model','duration','ratio')}
            if echoed not in proof['echoed_parameters']:
                proof['echoed_parameters'].append(echoed)
        # A native submission after a confirmation can omit chat_ability.
        # Its creation flag plus native video-submitted block still proves
        # execution; absent parameters remain unverified, not inherited echoes.
        submission = (ext.get('use_creation') == '1' and ability.get('ability_type') in (None, 17)
                      and any(c.get('thinking_block', {}).get('finish_title') in SUBMITTED_TITLES
                              for c in contents if isinstance(c.get('thinking_block'), dict)))
        tools = decoded(ext.get('ai_creation_tool_list'), [])
        valid_jobs = [t for t in tools if isinstance(t, dict)
                and t.get('tool_name') == 'text_to_video' and re.fullmatch(r'[1-9]\d*', str(t.get('task_id', '')))
                and t.get('fail_code', 0) in (0, '0')]
        jobs = [str(t['task_id']) for t in valid_jobs]
        task_models.extend(TASK_MODELS[t['req_key']] for t in valid_jobs
                           if isinstance(t.get('req_key'), str) and t['req_key'] in TASK_MODELS)
        creations = [v for c in contents for v in decoded(decoded(c.get('creation_block'), {}).get('creations'), [])
                     if isinstance(v, dict)
                     and v.get('type') == 2 and isinstance(v.get('video'), dict) and v.get('id')]
        kinds = (['native_submission'] if submission else []) + (['video_task'] if jobs else []) + (['video_creation'] if creations else [])
        if kinds:
            proof['confirmed'] = True
            proof['message_ids'].append(mid)
            proof['task_ids'].extend(jobs or [str(v['id']) for v in creations])
            proof['kinds'].extend(kinds)
            if ability.get('ability_type') == 17:
                parameters.append(params)
        is_plain = (ext.get('is_finish') == '1' and contents
                    and ext.get('use_creation') != '1' and not tools
                    and all(set(c) <= {'text_block', 'pc_event_block'} and 'text_block' in c for c in contents))
        (plain if is_plain else opaque).append(mid)
    if parameters:
        proof['parameters_match'] = all(
            p.get('model') == MODEL_IDS.get(request.get('model'))
            and str(p.get('duration')) == str(request.get('duration'))
            and p.get('ratio') == request.get('ratio') for p in parameters)
        proof['parameters'] = [{k:p.get(k) for k in ('model','duration','ratio')} for p in parameters]
    if task_models:
        proof['model_matches'] = all(model == request.get('model') for model in task_models)
        proof['model_source'] = 'native_video_task'
    proof['final_text_only'] = bool(plain) and not opaque and not proof['confirmed']
    proof['observed_message_ids'] = list(records)
    proof['task_ids'] = list(dict.fromkeys(proof['task_ids']))
    proof['kinds'] = list(dict.fromkeys(proof['kinds']))
    return proof, texts
