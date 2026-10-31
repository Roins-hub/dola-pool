"""Classify browser setup failures without exposing proxy credentials."""
import re


class BrowserSetupError(RuntimeError):
    """End this browser attempt; the pool decides whether to try another account."""


class BrowserCleanupError(RuntimeError):
    """Browser/driver shutdown is unconfirmed; its ISP must not be reused."""


def terminal_setup_reason(error):
    if isinstance(error, BrowserSetupError):
        return str(error)
    text = str(error)
    if any(code in text for code in (
        'ERR_INVALID_AUTH_CREDENTIALS', 'ERR_PROXY_AUTH_UNSUPPORTED', 'ERR_PROXY_AUTH_REQUESTED',
    )):
        return '浏览器连接认证失败，请检查 ISP 代理的用户名、密码和线路状态；已结束本次浏览器提交。'
    if type(error).__name__ == 'TargetClosedError' or 'Target page, context or browser has been closed' in text:
        return '视频浏览器已关闭，已结束本次浏览器提交。'
    return None


def safe_navigation_reason(error):
    code = re.search(r'\bERR_[A-Z_]+\b', str(error))
    return code.group(0) if code else type(error).__name__
