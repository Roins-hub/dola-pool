"""Conservative classification of assistant replies, never user prompt echoes."""
import re

QUOTA_RE = re.compile(
    r'余额不足|餘額不足|额度不足|額度不足|额度耗尽|額度耗盡|残高不足|'
    r'クレジット(?:が|は)?不足|积分不足|insufficient\s+(?:credits?|balance|quota)|'
    r'(?:credits?|quota)\s+(?:exhausted|depleted)|한도.*부족|부족.*한도', re.I)
CONTENT_RE = re.compile(
    r'肖像保[护護]|肖像权|未认证人脸|実在する人物.*(?:対応していません|対応しておりません)|'
    r'审核不通过|未通过.{0,8}审核|内容审核.{0,8}(?:失败|拒绝|违规|不通过)|违反社区|'
    r'copyright.{0,25}(?:infringement|violation|not allowed)|著作権.{0,20}(?:侵害|保護)|portrait protection|'
    r'(?:real people|real person).*(?:not supported|cannot)', re.I | re.S)
MISSING_RE = re.compile(
    r'缺少(?:核心内容|提示词|参考图|必要信息)|请(?:先)?(?:提供|上传|输入).{0,12}(?:提示词|参考图片|主题)|'
    r'(?:please|need to)\s+(?:provide|upload|enter).{0,25}(?:prompt|reference image|subject)|'
    r'内容を入力してください|画像をアップロードしてください', re.I)
FAILED_RE = re.compile(
    r'無法生成|无法生成|不能生成|无法完成|無法完成|生成できません|'
    r'生成することができません|unable to (?:generate|create)|failed to (?:generate|create)|'
    r'video generation (?:is )?not possible|generation failed|動画生成に失敗|'
    r'エラーが発生|サーバーエラー|内部エラー|システムエラー|unexpected error|'
    r'an error occurred|something went wrong|request failed', re.I)
NETWORK_RE = re.compile(r'通信エラー|ネットワークエラー|network error|connection (?:failed|reset)|网络(?:错误|失败)', re.I)


def reply_failure(text):
    """Return an explicit category; an acceptance phrase cannot override a refusal."""
    for category, pattern in [('内容拒绝', CONTENT_RE), ('额度不足', QUOTA_RE),
                              ('需要补充信息', MISSING_RE), ('网络失败', NETWORK_RE),
                              ('上游生成失败', FAILED_RE)]:
        if pattern.search(text or ''):
            return category
    return None


# ai_creation_res_code for input refusals seen in real chains (710082031: 肖像保護).
CONTENT_CODES = {'710082031'}
TOOL_FAILED = 5
NOT_CHARGED_RE = re.compile(
    r'クレジットは使用されませんでした|积分未(?:被)?(?:使用|扣除)|额度未扣除|未扣(?:除)?(?:积分|额度)|'
    r'credits? (?:was|were) not (?:used|deducted|charged)', re.I)


def final_generation_failure(latest, has_video):
    """Return failure details only when the latest finished card says the task failed.

    Text alone is not final: Dola has shown failure/quota wording before a video
    arrived. A failed creation card carries ai_creation_tool_list status 5 and a
    non-zero fail/res code; a successful one has status 4 and a video.
    """
    if has_video or not latest or not latest.get('finished'):
        return None
    tools = latest.get('tools') or []
    if not tools or any(t.get('status') != TOOL_FAILED for t in tools):
        return None
    codes = [c for c in [latest.get('resCode')] + [t.get('failCode') for t in tools]
             if c and c != '0']
    if not codes:
        return None
    text = latest.get('text') or ''
    category = reply_failure(text)
    retryable = codes[0] not in CONTENT_CODES and category not in ('内容拒绝', '需要补充信息', '额度不足')
    return {'code': codes[0], 'text': text, 'retryable': retryable,
            'not_charged': bool(NOT_CHARGED_RE.search(text)),
            'category': category or '上游生成失败'}


_CODE_BLOCK = re.compile(r"```.*?(?:```|$)", re.S)
_RANGE = r"(\d+)(?:\s*[-~～〜到至]\s*(\d+))?"
_WAIT_PATTERNS = (
    (re.compile(_RANGE + r"\s*時間"), 3600),
    (re.compile(_RANGE + r"\s*分(?:分)?\s*(?:後|以内|ほど|程度|くらい|ぐらい|前後|かかり)"), 60),
    (re.compile(r"(?:预计|預計|大约|大約|约|約|需要|等待)\s*" + _RANGE + r"\s*(?:分钟|分鐘)"), 60),
    (re.compile(_RANGE + r"\s*(?:分钟|分鐘)\s*(?:左右|后|後|内|以内)"), 60),
    (re.compile(_RANGE + r"\s*小时"), 3600),
    (re.compile(_RANGE + r"\s*(?:minutes?|mins?)(?![a-z])", re.I), 60),
)


def wait_seconds_from_reply(text):
    """Dola's own estimate in an assistant reply ("5分後に生成されます", "1-3分以内", "约 5 分钟"), in seconds.

    Takes the upper end of a range. Code blocks are dropped first: Dola now echoes the
    prompt inside a JSON plan, and a prompt saying "3分钟" is not a wait estimate.
    """
    clean = _CODE_BLOCK.sub(" ", str(text or "")).replace("**", "")
    for pattern, unit in _WAIT_PATTERNS:
        match = pattern.search(clean)
        if match:
            value = int(match.group(2) or match.group(1))
            if 0 < value * unit <= 6 * 3600:
                return value * unit
    return 0


def duration_reply(text, duration, ratio=None):
    """Reply only to an explicit duration question/options in the latest assistant turn."""
    if not text or not duration or reply_failure(text):
        return None
    clean = text.replace('**', '')
    options = list(re.finditer(r'(?<!\w)([A-D])\s*[.．、)）]\s*(\d+)\s*(?:秒|s\b|seconds?\b)', clean, re.I))
    asks = re.search(r'選んで|選択|指定|请选择|选择|确认|choose|select|which|何秒|几秒|時長|时长|秒数', clean, re.I)
    if options and asks:
        for option in options:
            if int(option[2]) == duration:
                return f'{option[1].upper()}. {duration}秒'
        return None
    if re.search(r'(?:秒数|時長|时长|duration).{0,35}(?:指定|選択|選んで|请选择|选择|确认|choose|select)|'
                 r'(?:choose|select|specify).{0,20}duration|何秒(?:に|で)|需要几秒', clean, re.I):
        return f'{ratio or "16:9"}、{duration}秒'
    return None


# 2026-10-30 线上：「この動画は 10 秒を指定されていますが、現在の動画生成は 4～15 秒に対応しています。
# 直接生成を開始してよろしいですか？」——请求时长本来就在支持范围内，dola 只是问一句要不要直接开始。
# 不回话就永远不出片（任务一直停在「已取得上游会话」）。
_START_QUESTION = re.compile(
    r'よろしいですか|よろしいでしょうか|いいですか|開始しますか|生成しますか|進めますか|始めますか|'
    r'是否(?:直接)?(?:开始|生成|继续)|要不要(?:直接)?(?:开始|生成)|(?:开始|直接)生成吗|可以吗|'
    r'do you want me to (?:proceed|start|generate|go ahead)|shall i (?:proceed|start|generate)|'
    r'would you like me to (?:proceed|start|generate)|(?:ok|okay) to (?:proceed|start)', re.I)
_SECONDS = re.compile(r'(\d+)\s*(?:秒|seconds?\b|secs?\b|s\b)', re.I)
_SECOND_RANGE = re.compile(r'(\d+)\s*(?:秒|s)?\s*[~～〜\-–到至]\s*(\d+)\s*(?:秒|seconds?\b|s\b)', re.I)
MAX_SINGLE_SECONDS = 15


def start_confirm_reply(text, duration):
    """dola 只是在确认「按请求的时长直接开始生成吗」→ 返回一句确认；否则 None。

    只在请求时长 ≤ 15 秒、回复里没有提到别的时长（除了支持范围的上下限）时才确认，
    免得把「改成 15 秒可以吗」这类改时长的提议也答应下来。
    """
    if not text or not duration or reply_failure(text):
        return None
    duration = int(duration)
    if duration > MAX_SINGLE_SECONDS or not _START_QUESTION.search(text):
        return None
    clean = text.replace('**', '')
    allowed = {duration}
    for low, high in _SECOND_RANGE.findall(clean):
        if not int(low) <= duration <= int(high):
            return None
        allowed |= {int(low), int(high)}
    mentioned = {int(n) for n in _SECONDS.findall(clean)}
    if duration not in mentioned and not _SECOND_RANGE.search(clean):
        return None
    if mentioned - allowed:
        return None
    if re.search(r'[\u3040-\u30ff]', clean):
        return f'はい、{duration}秒で生成を開始してください。'
    if re.search(r'[\u4e00-\u9fff]', clean):
        return f'确认，直接按{duration}秒开始生成。'
    return f'Yes, please start generating the {duration}-second video now.'
