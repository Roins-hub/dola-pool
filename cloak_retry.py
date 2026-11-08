"""5/10 秒浏览器提交被 dola 拒绝（710022002）时，用 CloakBrowser 再提交一次。

2026-11-07 工作模式实测：同一个号在 patchright 里被 710022002 拒绝，放进 CloakBrowser 能过。
这里照工作模式的做法，把账号登录态复制进一个临时 CloakBrowser profile，用完即删：
账号自己的 patchright profile 不动，平时也不多开浏览器，只在被拒时多开这一次。
"""
import shutil
import tempfile
from contextlib import asynccontextmanager
from importlib.util import find_spec
from pathlib import Path

import config


def unavailable_reason(account: str, duration) -> str:
    """能用 CloakBrowser 重试时返回空串，否则返回不重试的原因。"""
    if not config.CLOAK_RETRY:
        return "DOLA_CLOAK_RETRY=0"
    if duration == 30:
        return "30 秒走扩展注入，CloakBrowser 里没测过"
    if find_spec("cloakbrowser") is None:
        return "未安装 CloakBrowser"
    from account_transport import http_proxy
    try:
        if not http_proxy(account):
            return "没有绑定代理"
    except Exception as exc:
        return str(exc)[:80]
    return ""


@asynccontextmanager
async def cloak_context(account: str, use_extension: bool):
    """带着账号登录态的临时 CloakBrowser；退出时关浏览器、删 profile。"""
    from patchright.async_api import async_playwright
    from video_worker_agent import _copied_session, _launch_cloak

    extensions = None
    if use_extension and config.EXTENSION_ENABLED and Path(config.EXTENSION_DIR).exists():
        extensions = [str(Path(config.EXTENSION_DIR).resolve())]
    profile = tempfile.mkdtemp(prefix="dola-retry-")
    context = None
    try:
        async with async_playwright() as p:
            cookies = await _copied_session(p, account)
        context = await _launch_cloak(account, profile, extensions)
        await context.add_cookies(cookies)
        yield context
    finally:
        try:
            if context is not None:
                await context.close()
        finally:
            shutil.rmtree(profile, ignore_errors=True)
