"""Read bounded completion JSON/SSE without interpreting undocumented event types.

Payloads remain internal evidence in wire order. The caller must prefer an earlier
trusted acceptance receipt over a later error. A response code alone proves no
billing fact, so this parser never declares a rejection free of charge.
"""
import json
import re


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_CREDENTIAL = re.compile(
    r"\b(?:access[_-]?token|refresh[_-]?token|msToken|token|sessionid(?:_ss)?|"
    r"session_id|sid_guard|authorization|password|passwd|ck)\s*[:=]\s*"
    r'''(?:"[^"\r\n]*"|'[^'\r\n]*'|[^;\s,}]+)''', re.I)


def _repair_utf8_message(value):
    """Undo only reversible UTF-8 Chinese misread as web Latin-1/Windows-1252.

    Chromium's web Windows-1252 decoder preserves undefined C1 bytes. Correct
    Unicode characters outside that byte mapping delimit runs and stay intact.
    Wire payloads remain unchanged; this is only the public error explanation.
    """
    output, chars, octets = [], [], bytearray()

    def flush():
        if not chars:
            return
        original = ''.join(chars)
        try:
            repaired = bytes(octets).decode('utf-8', errors='strict')
        except UnicodeDecodeError:
            repaired = original
        if repaired != original and re.search(r'[\u3400-\u9fff]', repaired):
            output.append(repaired)
        else:
            output.append(original)
        chars.clear()
        octets.clear()

    for char in value:
        try:
            encoded = bytes([ord(char)]) if ord(char) <= 255 else char.encode('cp1252')
        except UnicodeEncodeError:
            flush()
            output.append(char)
        else:
            chars.append(char)
            octets.extend(encoded)
    flush()
    return ''.join(output)


def _redact(value):
    value = re.sub(r'\bcookie\s*:\s*[^\r\n]+', 'Cookie: [redacted]', value,
                   flags=re.I)
    value = re.sub(r'\bbearer\s+[^\s;,"\'}]+', 'Bearer [redacted]', value,
                   flags=re.I)
    value = _CREDENTIAL.sub(lambda match: match.group().split('=', 1)[0].split(':', 1)[0]
                            + '=[redacted]', value)
    value = re.sub(r'(https?://)[^/\s:@]+:[^/@\s]+@', r'\1[redacted]@', value,
                   flags=re.I)
    value = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b',
                   '[redacted-email]', value, flags=re.I)
    return re.sub(r'\s+', ' ', ''.join(char for char in value
                  if char >= ' ' or char in '\n\r\t')).strip()


def _object(text):
    try:
        value = json.loads(text)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _error(payload):
    for key in ('code', 'errorcode', 'error_code'):
        code = payload.get(key)
        if type(code) not in (int, str):
            continue
        text = str(code).strip()
        if not text or (re.fullmatch(r'[+-]?\d+', text) and not text.lstrip('+-').strip('0')):
            continue
        message = next((payload.get(key) for key in ('message', 'msg', 'error_message', 'error_msg')
                        if isinstance(payload.get(key), str) and payload[key].strip()),
                       '上游返回错误。')
        return {'code': _redact(text)[:100], 'message': _redact(_repair_utf8_message(message))[:500],
                'no_charge': False}
    return None


def parse_completion_response(raw: str) -> dict:
    """Return JSON objects and the first top-level nonzero-code error candidate."""
    empty = {'payloads': [], 'error': None}
    if not isinstance(raw, str) or len(raw) > MAX_RESPONSE_BYTES:
        return empty
    if len(raw.encode('utf-8', errors='replace')) > MAX_RESPONSE_BYTES:
        return empty
    raw = raw.lstrip('\ufeff').strip()
    whole = _object(raw)
    if whole is not None:
        payloads = [whole]
    else:
        payloads, data = [], []
        # SSE uses CR/LF delimiters; str.splitlines() also splits C1/NEL and
        # Unicode separators which may legitimately occur inside JSON strings.
        for line in re.split(r'\r\n|\r|\n', raw) + ['']:
            if not line:
                if data:
                    payload = _object('\n'.join(data))
                    if payload is not None:
                        payloads.append(payload)
                data = []
            elif line.startswith('data:'):
                data.append(line[5:].removeprefix(' '))
    error = next((candidate for payload in payloads
                  if (candidate := _error(payload)) is not None), None)
    return {'payloads': payloads, 'error': error}
