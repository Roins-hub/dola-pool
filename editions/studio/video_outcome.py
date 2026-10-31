"""Classify original-conversation evidence independently of local request auditing."""
import re

from video_evidence import matches_submitted_prompt, safe_text
from video_capabilities import normalize_model

GENERATION_RECEIPT_PATTERN = re.compile(
    r'Seedance\s*(?:v)?2[.,]0.*?(?:\d+\s*个?\s*(?:视频生成|生成|视频)?额度|\d+\s+(?:credits?|Guthaben)|'
    r'\bIt\s+will\s+use\s+\d+\s+points?\b|'
    r'\d+\s+Punkte\s+(?:verwendet|verbraucht|abgezogen|eingesetzt)\b)|'
    r'将消耗\s*\d+\s*个视频生成额度.*预计等待', re.I | re.S,
)
FAST_MODEL_ETA_PATTERN = re.compile(
    r'^\s*(?:我将按你的要求直接生成一条15秒横屏16:9视频，完整使用你给出的提示词，不修改内容。\s+)?'
    r'(?:(?:This|The)\s+video\s+will\s+be\s+generated\s+using\s+(?:the\s+)?'
    r'(?:Dreamina\s+)?Seedance\s*(?:v)?2[.,]0(?:\s+Fast)?(?:\s+model)?\s+in\s+\d+\s+minutes?\b|'
    r'Dieses\s+Video\s+wird\s+mithilfe\s+des\s+Seedance\s*2[.,]0\s+Modells\s+in\s+'
    r'\d+\s+Minuten?\s+generiert\b|'
    # Native German Fast receipt sometimes repeats the translated time unit.
    r'Das\s+Video\s+wird\s+mit\s+dem\s+(?:Dreamina\s+)?Seedance\s*2[.,]0\s+'
    r'Fast[-‑– ]Modell\s+in\s+\d+\s+Minuten?(?:\s+Minuten?)?\s+generiert\b)', re.I)
FAST_ETA_EXAMPLE_PATTERN = re.compile(
    r'^\s*[>"“”]|示例|例子|引用|\b(?:examples?|samples?|quotes?|quoted|quotation|Zitat|Beispiele?)\b',
    re.I | re.M)
SEEDANCE10_RECEIPT_PATTERN = re.compile(
    r'Seedance\s*(?:v)?1[.,]0\b.*?(?:\d+\s*个?\s*(?:视频生成|生成|视频)?额度|'
    r'\bIt\s+will\s+use\s+\d+\s+(?:points?|credits?)\b|'
    r'\d+\s+Punkt(?:e)?\s+(?:verwendet|verbraucht|abgezogen|eingesetzt)\b)', re.I | re.S)
VIDEO_CREDIT_ETA_PATTERN = re.compile(
    r'\bThe\s+video\s+will\s+use\s+\d+\s+credits?\s+and\s+be\s+ready\s+in\s+\d+\s+minutes?\b', re.I)
EXPLICIT_RECEIPT_MODEL_PATTERN = re.compile(r'\bSeedance\s*(?:v)?(\d+[.,]\d+)\b', re.I)
CREDIT_ETA_NEGATION_PATTERN = re.compile(
    r'\b(?:not|never)\s+(?:yet\s+)?(?:started|begun|generated|used|consumed|charged)\b|'
    r'\b(?:hasn|haven|isn|aren)[\'’]t\s+(?:yet\s+)?(?:started|begun|generat\w*)\b|'
    r'\bNo\s+credits?\s+(?:have\s+been|were|are|will\s+be)\s+(?:used|charged|consumed)\b', re.I)
SEEDANCE25_RECEIPT_MODEL = re.compile(r'Seedance\s*(?:v)?2[.,]5\b', re.I)
TWO_CREDIT_RECEIPT = re.compile(
    r'(?<!\d)2\s*个?\s*(?:视频生成|视频|生成)?额度|'
    r'(?<!\d)2\s+(?:video\s+generation\s+)?(?:credits?|Guthaben|Videogenerierungen)\b', re.I)
RECEIPT_PENDING_PATTERN = re.compile(
    r'请选择|请确认(?:是否|要|继续|开始)?|是否(?:确认|要|继续|开始)|要不要(?:开始|生成)|'
    r'(?:尚未|没有|未曾)(?:开始|进入|提交|启动)(?:视频)?生成|'
    r'(?:不会|没有|未曾).{0,12}(?:扣除|消耗)|'
    r'would\s+you\s+like|please\s+(?:choose|confirm)|shall\s+I|'
    r'(?:will\s+not|won.t|have\s+not|not\s+yet).{0,20}(?:charg|consum|start|generat)|'
    r'soll\s+ich|bitte.{0,20}(?:wählen|bestätig(?:en|e))|'
    r'(?:keine|nicht).{0,20}(?:verbraucht|abgezogen|gestartet|verwendet|generiert)', re.I)
EXPLICIT_CONFIRMATION_PATTERN = re.compile(
    r'(?:请选择|请确认(?:是否|要|继续|开始)?|是否(?:确认|要|继续|开始)|要不要(?:开始|生成))'
    r'.{0,100}(?:生成|时长|\d+\s*秒)|'
    r'(?:please\s+(?:choose|confirm)|shall\s+I|would\s+you\s+like)'
    r'.{0,100}(?:generat(?:e|ion|ing)|duration|\d+\s*seconds?)|'
    r'(?:soll\s+ich|bitte.{0,12}(?:wählen|bestätig(?:en|e)))'
    r'.{0,100}(?:generier\w*|dauer|\d+\s*sekunden?)', re.I | re.S)
GENERATION_STARTED_PATTERN = re.compile(
    r'(?:正在|已经|已)(?:开始)?生成|'
    r'\b(?:is|are)\s+(?:being\s+)?generated\b|'
    r'\bwird\b.{0,160}\bgeneriert\b', re.I | re.S)

# Only confirmed platform refusal sentences; prompt keywords are not terminal evidence.
AUDIO_COPYRIGHT = re.compile(
    r'^\s*Zum Schutz des Urheberrechts kann ich dir das generierte Video aufgrund seines Audios nicht anzeigen\b', re.I)
COPYRIGHT = re.compile(
    r'^\s*Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen\b', re.I)
CONTENT_POLICY_REJECTION = re.compile(
    r"^\s*(?:I\s+(?:can['’]t|cannot)\s+show\s+you\s+the\s+generated\s+video\s+"
    r'because\s+it\s+may\s+contain\s+copyrighted\s+or\s+policy[-‑ ]violating\s+content\b|'
    r"I\s+(?:can['’]t|cannot)\s+generate\s+the\s+video\s+because\s+your\s+input\s+contains\s+"
    r'content\s+that\s+may\s+violate\s+our\s+policies\b|'
    r'Ich\s+kann\s+dir\s+das\s+generierte\s+Video\s+nicht\s+anzeigen,\s+da\s+es\s+'
    r'möglicherweise\s+urheberrechtlich\s+geschützte\s+oder\s+richtlinienwidrige\s+Inhalte\s+enthält\b)', re.I)
INPUT_POLICY_REJECTION = re.compile(
    r"^I\s+(?:can['’]t|cannot)\s+generate\s+the\s+video\s+because\s+your\s+input\s+contains\s+"
    r'content\s+that\s+may\s+violate\s+our\s+policies(?:[.!?]|$)', re.I)
NO_CHARGE_STATEMENT = re.compile(
    r'(?:^|[.!?]\s+|\n\s*)(?:No\s+credits?\s+were\s+used\s+for\s+this\s+video|'
    r'Für\s+dieses\s+Video\s+wurden\s+keine\s+Credits\s+verwendet)(?:[.!?]|$)|'
    r"^I\s+(?:can['’]t|cannot)\s+generate\s+the\s+video\s+and\s+"
    r"did(?:\s+not|n['’]t)\s+use\s+any\s+credits?\s+for\s+this\s+generation(?:[.!?]|$)", re.I)
REJECTION = re.compile(
    r'^\s*(?:Ich kann das Video nicht generieren\b|'
    r"I\s+(?:can['’]t|cannot)\s+generate\s+the\s+video\s+and\s+"
    r"did(?:\s+not|n['’]t)\s+use\s+any\s+credits?\s+for\s+this\s+generation\b)", re.I)
REAL_PERSON_IMAGE_REJECTION = re.compile(
    r'^\s*(?:Zum Schutz des Erscheinungsbilds unterstützt Dreamina Seedance\s*2[.,]0\s+'
    r'das Generieren von Videos aus Bildern von realen Personen nicht\b|'
    r"For likeness protection, Dreamina Seedance\s*2[.,]0\s+doesn['’]t support generating videos "
    r'from images of real people\b)', re.I)
GENERATION_FAIL_PATTERN = re.compile(
    r'^\s*(?:Ich kann das Video nicht generieren\b|'
    r'Zum Schutz des Urheberrechts kann ich dir das generierte Video '
    r'(?:aufgrund seines Audios )?nicht anzeigen\b)', re.I)
DAILY_VIDEO_LIMIT = re.compile(
    r'^\s*Du\s+hast\s+das\s+tägliche\s+kostenlose\s+Nutzungslimit\s+für\s+'
    r'(?:die\s+)?Videogenerierung\s+erreicht(?:[.!?]|$)', re.I)


class PlatformVideoError(RuntimeError):
    def __init__(self, code, label, reply):
        self.task_phase = code
        super().__init__(f'Dola 返回无法生成视频（{label}），未重新提交：{safe_text(reply)}')


class PlatformNoChargeError(PlatformVideoError):
    task_phase = 'platform_rejected_no_charge'
    quota_not_consumed = True

    def __init__(self, reply):
        self.reply = safe_text(reply)
        super().__init__(self.task_phase, '平台拒绝，未消耗生成额度', self.reply)


class PlatformDailyLimitError(PlatformVideoError):
    def __init__(self, reply):
        self.reply = safe_text(reply)
        super().__init__('platform_daily_limit', '每日免费视频额度已用完', self.reply)


class PlatformModelMismatchError(PlatformVideoError):
    task_phase = 'request_mismatch'

    def __init__(self, reply):
        RuntimeError.__init__(self, f'Dola 已受理其他模型，与 Seedance 1.0 请求不符；已停止换号，请查看原会话：{safe_text(reply)}')


class VideoQueryTimeout(TimeoutError):
    task_phase = 'query_timeout'


class ExplicitNotAcceptedError(RuntimeError):
    task_phase = 'not_accepted'

    def __init__(self, reply):
        self.reply = safe_text(reply)
        super().__init__(f'平台明确尚未开始生成，需确认时长或选项：{self.reply}')


def is_seedance25_receipt(text):
    return bool(not RECEIPT_PENDING_PATTERN.search(text) and
                SEEDANCE25_RECEIPT_MODEL.search(text) and TWO_CREDIT_RECEIPT.search(text))


def is_fast_receipt(text):
    return bool(not RECEIPT_PENDING_PATTERN.search(text) and GENERATION_RECEIPT_PATTERN.search(text))


def is_seedance10_receipt(text):
    return bool(not RECEIPT_PENDING_PATTERN.search(text) and SEEDANCE10_RECEIPT_PATTERN.search(text))


def _is_credit_eta_receipt(text):
    return bool(not RECEIPT_PENDING_PATTERN.search(text) and not CREDIT_ETA_NEGATION_PATTERN.search(text)
                and VIDEO_CREDIT_ETA_PATTERN.search(text))


def _is_fast_model_eta_receipt(blocks):
    text = '\n'.join(blocks)
    if '```' in text or '~~~' in text:
        return False
    blocks = [re.sub(r'[*_`]', '', block) for block in blocks]
    text = '\n'.join(blocks)
    return bool(any(FAST_MODEL_ETA_PATTERN.search(block) for block in blocks)
                and not FAST_ETA_EXAMPLE_PATTERN.search(text) and not RECEIPT_PENDING_PATTERN.search(text)
                and not CREDIT_ETA_NEGATION_PATTERN.search(text)
                and not re.search(r'\bnicht\s+begonnen\b', text, re.I)
                and {version.replace(',', '.') for version in EXPLICIT_RECEIPT_MODEL_PATTERN.findall(text)} == {'2.0'})


def _platform_refusal(blocks):
    raw = '\n'.join(blocks)
    if '```' in raw or '~~~' in raw or FAST_ETA_EXAMPLE_PATTERN.search(raw):
        return None
    plain = re.sub(r'[*_]', '', raw)
    # A single refusal can span native text blocks. Inspect only block boundaries,
    # while its explicit no-credit statement must belong to this same message.
    for index in range(len(blocks)):
        text = re.sub(r'\s+', ' ', re.sub(r'[*_]', '', '\n'.join(blocks[index:]))).strip()
        if DAILY_VIDEO_LIMIT.search(text):
            return PlatformDailyLimitError(raw)
        for pattern, code, label in ((AUDIO_COPYRIGHT, 'platform_audio_copyright', '平台音频版权限制'),
                                     (COPYRIGHT, 'platform_copyright', '平台版权限制'),
                                     (CONTENT_POLICY_REJECTION, 'platform_rejected', '平台版权或内容政策限制'),
                                     (REAL_PERSON_IMAGE_REJECTION, 'platform_rejected', '该模型不支持真人参考图，请更换图片或使用纯文字生成'),
                                     (REJECTION, 'platform_rejected', '平台拒绝生成')):
            if pattern.search(text):
                if INPUT_POLICY_REJECTION.search(text) or NO_CHARGE_STATEMENT.search(plain):
                    return PlatformNoChargeError(raw)
                return PlatformVideoError(code, label, raw)
    return None


def conversation_outcome(poll, model=None, submitted_prompt=None):
    messages = poll.get('assistant_messages')
    if messages is None:
        messages = [poll.get('assistant_texts', poll.get('texts', []))]
    groups = [[text for text in message
               if isinstance(text, str) and not text.lstrip().startswith('Generiertes Video: ')
               and not (submitted_prompt and matches_submitted_prompt(text, submitted_prompt))]
              for message in messages if isinstance(message, list)]
    texts = [text for group in groups for text in group]
    result = {'texts': texts, 'reply': safe_text('\n\n'.join(texts)),
              'video_count': len(poll.get('videos', [])), 'state': 'awaiting_receipt', 'error': None}
    if result['video_count']:
        result['state'] = 'completed'
        return result
    refusal = None
    # Flat legacy texts do not carry message boundaries: never combine them for a refund.
    refusal_groups = groups if poll.get('assistant_messages') is not None else [[text] for text in texts]
    for group in refusal_groups:
        error = _platform_refusal(group)
        if isinstance(error, PlatformNoChargeError):
            result.update(state='failed', error=error)
            return result
        refusal = refusal or error
    if refusal:
        result.update(state='failed', error=refusal)
        return result
    message_texts = ['\n'.join(group) for group in groups]
    is_one = model and (model == 'ic_mini' or '1.0' in model or model.endswith(('10', 'v10')))
    if any(is_seedance10_receipt(text) if is_one else
           is_seedance25_receipt(text) if model and '2.5' in model else
           (is_fast_receipt(text) or is_seedance25_receipt(text)) for text in message_texts):
        result['state'] = 'accepted'
        return result
    try:
        is_fast_request = normalize_model(model) == 'seedance-2.0'
    except ValueError:
        is_fast_request = False
    # This model-and-ETA format was observed for Fast; keep all other model receipt rules unchanged.
    if is_fast_request and any(_is_fast_model_eta_receipt(group) for group in groups):
        result['state'] = 'accepted'
        return result
    if is_one:
        for text in message_texts:
            if is_fast_receipt(text) or is_seedance25_receipt(text) or (
                    _is_credit_eta_receipt(text) and any(version.replace(',', '.') != '1.0'
                        for version in EXPLICIT_RECEIPT_MODEL_PATTERN.findall(text))):
                result.update(state='failed', error=PlatformModelMismatchError(text))
                return result
        # The observed 1.0 reply omits the model; only this request context can identify it.
        if any(_is_credit_eta_receipt(text) and not EXPLICIT_RECEIPT_MODEL_PATTERN.search(text)
               for text in message_texts):
            result['state'] = 'accepted'
            return result
    # A different model or credit count does not prove that no engine has started.
    if any(is_seedance10_receipt(text) or is_fast_receipt(text) or is_seedance25_receipt(text) or
           (SEEDANCE25_RECEIPT_MODEL.search(text) and not RECEIPT_PENDING_PATTERN.search(text)
            and GENERATION_STARTED_PATTERN.search(text)) for text in message_texts):
        return result
    for text in texts:
        if EXPLICIT_CONFIRMATION_PATTERN.search(text):
            result.update(state='not_accepted', error=ExplicitNotAcceptedError(text))
            return result
    return result
