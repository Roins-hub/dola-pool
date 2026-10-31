"""Read accepted Dola conversations without keeping a browser open.

Private cookies and the original proxy stay under the ignored accounts directory.
This module never submits a generation request.
"""
import json
import hashlib
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

import config
import proxy_usage
from account_errors import LoginExpiredError

API_BASE = 'https://www.dola.com'
ACCOUNTS_DIR = Path('accounts')


class PollingResponseError(RuntimeError):
    pass


def _snapshot_path(account, conversation_id):
    if not account or Path(account).name != account or account in ('.', '..'):
        raise ValueError('invalid account')
    if not str(conversation_id).isdigit():
        raise ValueError('invalid conversation_id')
    return ACCOUNTS_DIR / account / 'polling' / f'{conversation_id}.json'


def save_poll_state(account, conversation_id, cookies, user_agent, baseline_media=None, baseline_messages=None):
    path = _snapshot_path(account, conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _, source = config.account_proxy_raw(account)
    state = {'cookies': cookies, 'user_agent': user_agent,
             'proxy_url': config.proxy_url_for(account), 'own_isp': source in ('file', 'map'),
             'baseline_media': list(baseline_media or []), 'baseline_messages': list(baseline_messages or [])}
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def _load_state(account, conversation_id):
    path = _snapshot_path(account, conversation_id)
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    # Tasks accepted before this feature have only the existing saved cookies.
    cookies = json.loads((ACCOUNTS_DIR / account / 'cookies.json').read_text(encoding='utf-8-sig'))
    _, source = config.account_proxy_raw(account)
    return {'cookies': cookies, 'user_agent': '', 'proxy_url': config.proxy_url_for(account),
            'own_isp': source in ('file', 'map')}


def media_key(url):
    parsed = urlsplit(url)
    return hashlib.sha256((parsed.netloc + parsed.path).encode('utf-8')).hexdigest()


def parse_poll_response(data, conversation_id=None, baseline_media=None, baseline_messages=None):
    if not isinstance(data, dict):
        raise PollingResponseError('轮询响应格式错误')
    code = data.get('status_code')
    if code not in (None, 0):
        safe_code = str(code) if isinstance(code, int) else 'invalid'
        raise PollingResponseError(f'Dola 轮询接口状态码 {safe_code}')
    body = data.get('downlink_body')
    chain = body.get('pull_singe_chain_downlink_body') if isinstance(body, dict) else None
    if not isinstance(chain, dict) or not isinstance(chain.get('messages', []), list):
        raise PollingResponseError('轮询响应缺少会话内容')
    if conversation_id and chain.get('conversation_id') and str(chain['conversation_id']) != str(conversation_id):
        raise PollingResponseError('轮询响应会话不匹配，已忽略媒体结果')
    baseline = set(baseline_media or [])
    previous_messages = set(baseline_messages or [])
    result = {'ok': True, 'texts': [], 'assistant_texts': [], 'assistant_messages': [],
              'videos': [], 'videoModels': [], 'mediaKeys': [], 'messageKeys': []}
    for message in chain.get('messages', []):
        if not isinstance(message, dict):
            continue
        if conversation_id and message.get('conversation_id') and str(message['conversation_id']) != str(conversation_id):
            continue
        role = str(message.get('role') or message.get('sender_role') or '').lower()
        is_user = role in ('user', 'human') or message.get('is_user') is True or str(message.get('user_type')) == '1'
        content = message.get('content')
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                continue
        if isinstance(content, dict) and isinstance(content.get('text'), str):
            content = [{'content': {'text_block': {'text': content['text']}}}]
        if not isinstance(content, list):
            continue
        message_key = str(message.get('message_id') or message.get('id') or
                          hashlib.sha256(json.dumps(content, sort_keys=True).encode('utf-8')).hexdigest())
        if message_key in previous_messages:
            continue
        result['messageKeys'].append(message_key)
        assistant_blocks = []
        if not is_user:
            result['assistant_messages'].append(assistant_blocks)
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get('content'), dict):
                continue
            fields = block['content']
            text_block = fields.get('text_block')
            if isinstance(text_block, dict) and isinstance(text_block.get('text'), str):
                result['texts'].append(text_block['text'])
                if not is_user:
                    result['assistant_texts'].append(text_block['text'])
                    assistant_blocks.append(text_block['text'])
            creation = fields.get('creation_block')
            if block.get('block_type') != 2074 or not isinstance(creation, dict):
                continue
            creations = creation.get('creations', [])
            if not isinstance(creations, list):
                continue
            for item in creations:
                if is_user or not isinstance(item, dict) or item.get('type') != 2:
                    continue
                video = item.get('video')
                if not isinstance(video, dict):
                    continue
                url = video.get('download_url')
                model = video.get('video_model')
                if isinstance(model, dict):
                    model = json.dumps(model)
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    # Some responses contain a video_model but no download_url.
                    from video_worker import extract_unwatermarked_url
                    url = extract_unwatermarked_url(model if isinstance(model, str) else '', '')
                if isinstance(url, str) and url.startswith(('http://', 'https://')):
                    key = str(item.get('id') or item.get('creation_id') or media_key(url))
                    if key in baseline or key in result['mediaKeys']:
                        continue
                    result['mediaKeys'].append(key)
                    result['videos'].append(url)
                    result['videoModels'].append(model if isinstance(model, str) else '')
    from dola_expert import parse_expert_messages
    expert = parse_expert_messages(data, conversation_id)
    # A separate field keeps ordinary video receipt and download rules unchanged.
    result['expert'] = expert
    from plugin_expert30 import parse_messages
    result['plugin_expert30'] = parse_messages(data, conversation_id, previous_messages)
    return result


class HttpVideoSession:
    def __init__(self, account, conversation_id):
        self.account = account
        self.conversation_id = str(conversation_id)
        self.state = _load_state(account, self.conversation_id)
        self.proxy_url = self.state.get('proxy_url')
        self.own_isp = self.state.get('own_isp', False)
        config.check_saved_isp(self.proxy_url, self.own_isp)

    async def __aenter__(self):
        cookies = self.state['cookies']
        headers = {
            'Cookie': '; '.join(f"{c['name']}={c['value']}" for c in cookies if c.get('name')),
            'Content-Type': 'application/json; encoding=utf-8', 'agw-js-conv': 'str',
            'Accept': '*/*', 'Referer': f'{API_BASE}/chat/{self.conversation_id}',
        }
        if self.state.get('user_agent'):
            headers['User-Agent'] = self.state['user_agent']
        self.session = aiohttp.ClientSession(headers=headers, trust_env=False,
                                            timeout=aiohttp.ClientTimeout(total=30))
        return self

    async def __aexit__(self, *exc):
        await self.session.close()

    async def poll(self):
        params = {
            'version_code': '20800', 'language': config.BROWSER_LANGUAGE,
            'device_platform': 'web', 'doubao_device_platform': 'web', 'aid': '495671',
            'real_aid': '495671', 'pkg_type': 'release_version', 'pc_version': '3.32.61',
            'doubao_pc_version': '3.32.61', 'region': config.BROWSER_REGION,
            'sys_region': config.BROWSER_REGION, 'samantha_web': '1', 'web_platform': 'browser',
            'use-olympus-account': '1', 'web_tab_id': str(uuid.uuid4()),
        }
        body = {'cmd': 3100, 'uplink_body': {'pull_singe_chain_uplink_body': {
            'conversation_id': self.conversation_id, 'anchor_index': 9007199254740991,
            'conversation_type': 3, 'direction': 1, 'limit': 20, 'ext': {},
            'filter': {'index_list': []}, 'evaluate_ab_params': '', 'evaluate_common_params': '',
        }}, 'sequence_id': str(uuid.uuid4()), 'channel': 2, 'version': '1'}
        if self.proxy_url and self.own_isp:
            proxy_usage.usage.reserve(config._parse_proxy(self.proxy_url), 1)
        async with self.session.post(f'{API_BASE}/im/chain/single', params=params, json=body,
                                     proxy=self.proxy_url, allow_redirects=False) as response:
            if response.status == 401 or response.headers.get('x-tt-agw-login', '').strip() == '0':
                raise LoginExpiredError('后台查询时 Dola 登录已失效，请重新登录或导入新 CK')
            if response.status != 200:
                raise PollingResponseError(f'轮询 HTTP {response.status}')
            return parse_poll_response(await response.json(content_type=None), self.conversation_id,
                                       self.state.get('baseline_media'), self.state.get('baseline_messages'))
