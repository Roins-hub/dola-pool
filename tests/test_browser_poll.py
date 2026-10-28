"""Execute the actual browser poll parser with synthetic upstream responses."""
import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def poll_messages(messages):
    node = shutil.which('node')
    if not node:
        pytest.skip('node required')
    tree = ast.parse((ROOT / 'video_worker.py').read_text(encoding='utf-8'))
    code = next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'POLL_JS' for t in n.targets))
    payload = {'downlink_body': {'pull_singe_chain_downlink_body': {'messages': messages}}}
    script = (
        'globalThis.crypto = require("node:crypto").webcrypto;\n'
        'globalThis.fetch = async () => ({ok:true,status:200,json:async()=>('
        + json.dumps(payload) + ')});\n'
        + '(' + code + ')({conversationId:"test"})'
        '.then(r=>process.stdout.write(JSON.stringify(r)))'
        '.catch(e=>{console.error(e);process.exit(1)});'
    )
    result = subprocess.run([node], input=script, text=True, encoding='utf-8',
                            capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)


def text_block(text):
    return {'block_type': 10000, 'content': {'text_block': {'text': text}}}


def test_latest_assistant_uses_message_index_not_array_order_or_user_echo():
    result = poll_messages([
        {'user_type': 2, 'index_in_conv': '9007199254740994', 'content_block': [text_block('最新回复')]},
        {'user_type': 1, 'index_in_conv': '9007199254740992', 'content_block': [text_block('请选择 5 秒')]},
        {'user_type': 2, 'index_in_conv': '9007199254740993', 'content_block': [text_block('旧回复')]},
    ])
    assert result['latestAssistant']['text'] == '最新回复'


def test_new_user_turn_prevents_replaying_previous_assistant_question():
    result = poll_messages([
        {'user_type': 2, 'index_in_conv': 1, 'content_block': [text_block('请选择时长：A. 5秒 B. 10秒')]},
        {'user_type': 1, 'index_in_conv': 2, 'content_block': [text_block('A. 5秒')]},
    ])
    assert result['latestAssistant'] is None


def test_unordered_or_empty_latest_message_cannot_reuse_old_question():
    assert poll_messages([{'user_type': 2, 'content_block': [text_block('旧时长问题')]}])['latestAssistant'] is None
    result = poll_messages([
        {'user_type': 2, 'index_in_conv': 1, 'content_block': [text_block('旧时长问题')]},
        {'user_type': 2, 'index_in_conv': 2, 'content_block': []},
    ])
    assert result['latestAssistant']['text'] == ''


@pytest.mark.parametrize('message', [
    {'user_type': 2, 'content_block': [text_block('正在生成，预计等待 3 分钟')]},
    {'user_type': '2', 'content': '', 'content_block': [text_block('正在生成，预计等待 3 分钟')]},
    {'user_type': 2, 'content': '{invalid', 'content_block': [text_block('正在生成，预计等待 3 分钟')]},
    {'user_type': 2, 'content': json.dumps([text_block('正在生成，预计等待 3 分钟')])},
    {'role': 'assistant', 'content': [text_block('正在生成，预计等待 3 分钟')]},
    {'sender': {'role': 'assistant'}, 'content': [text_block('正在生成，预计等待 3 分钟')]},
    {'user_type': 2, 'brief': '正在生成，预计等待 3 分钟'},
    {'user_type': 2, 'ext': {'brief': '正在生成，预计等待 3 分钟'}},
])
def test_poll_recognizes_assistant_replies(message):
    result = poll_messages([message])
    assert result['assistantTexts'] == ['正在生成，预计等待 3 分钟']
    assert result['texts'] == result['assistantTexts']


def test_poll_excludes_user_and_unmarked_text_from_assistant_notes():
    result = poll_messages([
        {'user_type': 1, 'role': 'assistant', 'content_block': [text_block('用户提示词')]},
        {'content': [text_block('未标记文本')]},
        {'user_type': 1, 'brief': '用户摘要'},
        {'user_type': 2, 'content_block': [text_block('Dola 回复')], 'brief': '截断摘要'},
    ])
    assert result['assistantTexts'] == ['Dola 回复']
    assert '用户提示词' in result['texts']
    assert '截断摘要' not in result['texts']


def test_poll_keeps_videos_and_deduplicates_modern_and_legacy_blocks():
    def video(url, model):
        return {'block_type': 2074, 'content': {'creation_block': {'creations': [
            {'type': 2, 'video': {'download_url': url, 'video_model': model}}]}}}
    blocks = [text_block('已生成'), video('https://example.invalid/one.mp4', 'model-one')]
    result = poll_messages([{'user_type': 2, 'content_block': blocks,
                            'content': json.dumps(blocks + [video('https://example.invalid/two.mp4', 'model-two')])}])
    assert result['assistantTexts'] == ['已生成']
    assert result['texts'] == ['已生成']
    assert result['videos'] == ['https://example.invalid/one.mp4', 'https://example.invalid/two.mp4']
    assert result['videoModels'] == ['model-one', 'model-two']
