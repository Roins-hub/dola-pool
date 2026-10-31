"""patchright persistent context 统一启动：显式代理 + 反检测参数。

代理必须显式传（不依赖系统代理——系统代理规则变化曾把 dola 分流到直连被墙，
见 DEVELOPMENT.md 第 5 节）。所有需要打开 dola 的脚本都走这里。
"""
import json
import re
import shutil
from pathlib import Path

import aiohttp

import config
import proxy_usage
from public_assets import PublicAssets
from video_capabilities import to_wire_model, validate_video_parameters
from proxy_chain import ProxyChainBridge
from navigation_errors import BrowserCleanupError


_shared_scheduler = None


def set_shared_scheduler(scheduler):
    """Install the service's scheduler; standalone tooling remains independent."""
    global _shared_scheduler
    _shared_scheduler = scheduler


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
]


def account_browser_options(profile_dir: Path):
    options = {'locale': config.BROWSER_LOCALE, 'accept_language': config.BROWSER_ACCEPT_LANGUAGE,
               'timezone_id': config.BROWSER_TIMEZONE, 'cdn_via_local': config.CDN_VIA_LOCAL,
               'proxy_chain': '', 'required_proxy_server': '', 'isp_transport':False, 'site_language':''}
    path = profile_dir / 'browser.json'
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(saved, dict) or set(saved) - (set(options) | {'viewport'}):
                raise ValueError()
            options.update(saved)
            for key in ('locale', 'accept_language', 'timezone_id', 'proxy_chain', 'required_proxy_server', 'site_language'):
                if not isinstance(options[key], str) or any(c in options[key] for c in '\r\n'):
                    raise ValueError()
            if not all(options[key] for key in ('locale', 'accept_language', 'timezone_id')):
                raise ValueError()
            if not isinstance(options['cdn_via_local'], bool) or not isinstance(options['isp_transport'], bool):
                raise ValueError()
            if 'viewport' in options:
                size = options['viewport']
                if (not isinstance(size, dict) or set(size) != {'width', 'height'}
                        or any(type(v) is not int or not 600 <= v <= 3840 for v in size.values())):
                    raise ValueError()
        except (OSError, ValueError, TypeError):
            raise ValueError('账号浏览器配置格式错误，已停止启动，未回退默认线路') from None
    return options


async def sync_site_language(context, language):
    """Update only Dola's interface preference; authentication/region stay intact."""
    if not language:
        return
    cookies = await context.cookies('https://www.dola.com')
    language_cookies = [{**c, 'value':language} for c in cookies if c['name'] == 'i18next']
    await context.add_cookies(language_cookies or [
        {'name':'i18next','value':language,'domain':'.dola.com','path':'/','secure':True,'sameSite':'Lax'}])


def prepare_profile_language(profile_dir: Path, locale=None, accept_language=None):
    """Keep persisted Chromium preferences consistent with the automation locale."""
    path = profile_dir / 'Default' / 'Preferences'
    try:
        original = path.read_text(encoding='utf-8') if path.exists() else '{}'
        data = json.loads(original)
        if not isinstance(data, dict):
            return
        languages = list(dict.fromkeys([locale or config.BROWSER_LOCALE] + [
            value.split(';')[0].strip() for value in (accept_language or config.BROWSER_ACCEPT_LANGUAGE).split(',')
            if value.strip()]))
        intl = data.setdefault('intl', {})
        translate = data.setdefault('translate', {})
        if not isinstance(intl, dict) or not isinstance(translate, dict):
            return
        intl.update(accept_languages=','.join(languages), selected_languages=','.join(languages))
        translate['enabled'] = False
        if data == json.loads(original):
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name('Preferences.before-language-fix')
        if path.exists() and not backup.exists():
            shutil.copy2(path, backup)
        temporary = path.with_name('Preferences.language.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)
    except (OSError, ValueError):
        # Never replace an unreadable profile or lose its unrelated preferences.
        return


async def launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False,
                               duration: int = None, native30: bool = False,
                               native_duration: int = None, proxy_override: str | None = None,
                               native_model: str = 'seedance-2.5', extension_dir: str | None = None):
    scheduler = _shared_scheduler
    options = dict(headless=headless, use_extension=use_extension, duration=duration,
                   native30=native30, native_duration=native_duration,
                   proxy_override=proxy_override, native_model=native_model)
    if extension_dir is not None:
        options['extension_dir'] = extension_dir
    if scheduler is None or scheduler.account_is_managed('dola', account):
        return await _launch_account_context(p, account, **options)
    bound = config.proxy_for(account)
    # Manual inspection uses the original bound line, including in-flight locks.
    # An override is still validated by the actual launcher before any network IO.
    lease = scheduler.diagnostic('dola', account, bound or 'direct')
    await lease.__aenter__()
    try:
        context = await _launch_account_context(p, account, **options)
    except BaseException as error:
        if getattr(error, 'browser_cleanup_failed', False):
            lease.quarantine()
        await lease.__aexit__(type(error), error, error.__traceback__)
        raise
    original_close = context.close

    def closed(_context):
        lease.release()

    async def close(*args, **kwargs):
        try:
            result = await original_close(*args, **kwargs)
        except BaseException:
            lease.quarantine()
            raise
        lease.release()
        return result

    context.on('close', closed)
    context.close = close
    return context


async def _launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False,
                                duration: int = None, native30: bool = False,
                                native_duration: int = None, proxy_override: str | None = None,
                                native_model: str = 'seedance-2.5', extension_dir: str | None = None):
    """启动 accounts/<account> profile，返回 BrowserContext。调用方负责 close。

    p: async_playwright() 实例
    headless: None = 用 config.HEADLESS
    """
    # native30 remains a compatibility alias for existing diagnostic callers.
    if native30:
        if native_duration not in (None, 30):
            raise ValueError('native30 与 native_duration 不一致')
        native_duration = 30
    if native_duration is not None and (type(native_duration) is not int or native_duration not in (5, 10, 15, 30)):
        raise ValueError('页面初始化时长仅支持 5、10、15 或 30 秒')
    if native_duration is not None:
        canonical_model, _ = validate_video_parameters(native_model, native_duration)
        native_model_key = to_wire_model(canonical_model)
    profile_dir = Path("accounts") / account
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"账号 profile 不存在: {profile_dir}（先跑 python login.py {account}）"
        )
    settings = account_browser_options(profile_dir)
    proxy = config.proxy_for(account) if proxy_override is None else config._parse_proxy(proxy_override)
    if config.ISP_ONLY:
        bound = config.proxy_for(account)
        if proxy != bound:
            raise ValueError('全 ISP 模式不允许覆盖账号绑定线路')
    required = settings['required_proxy_server']
    if required and (not proxy or proxy.get('server') != required):
        raise ValueError('该账号必须使用指定绑定的 ISP，已拒绝替换或默认线路')
    launch_headless = config.HEADLESS if headless is None else headless
    prepare_profile_language(profile_dir, settings['locale'], settings['accept_language'])
    args = list(LAUNCH_ARGS)
    if config.ISP_ONLY:
        args.extend(['--disable-quic', '--disable-background-networking',
                     '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'])
    if native_duration is not None:
        # Previously loaded profile extensions must not rewrite signed requests.
        args.append('--disable-extensions')
    if use_extension and native_duration is None:
        if not config.EXTENSION_ENABLED and extension_dir is None:
            raise RuntimeError("Dola 扩展已禁用（DOLA_EXTENSION_ENABLED=0）")
        extension_dir = Path(extension_dir or config.EXTENSION_DIR).resolve()
        if not extension_dir.exists():
            raise FileNotFoundError(f"Dola 扩展目录不存在: {extension_dir}")
        # Chrome debugger API 扩展需要有头模式；扩展会拦截 skill/action-bar 响应。
        launch_headless = False
        args.extend([
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
        ])
    kwargs = {
        "headless": launch_headless,
        "args": args,
        "locale": settings['locale'],
        "extra_http_headers": {"Accept-Language": settings['accept_language']},
        "timezone_id": settings['timezone_id'],
    }
    if 'viewport' in settings:
        kwargs['viewport'] = dict(settings['viewport'])
    if config.ISP_ONLY:
        kwargs['service_workers'] = 'block'
    # 只有账号专属 ISP（accounts/<name>/proxy.txt 或 proxies.json 显式映射）才消耗额度；
    # 全局 DOLA_PROXY / _default 默认映射属于共享出口，不计入单条 ISP 额度。
    _raw, source = config.account_proxy_raw(account)
    own_isp = (proxy_override is None or config.ISP_ONLY) and source in ("file", "map")
    if proxy:
        if own_isp:
            # 请求统计模式仅检查手动暂停；次数套餐还检查已用完。
            state = proxy_usage.usage.ensure_available(proxy)
            quota_text = (f"已统计 {state['used']} 次，本地次数不限" if state.get('unlimited')
                          else f"剩余 {state['remaining']}/{state['limit']} 次请求")
            print(
                f"[{account}] 专属 ISP 出口 {proxy['server']}"
                f"（{quota_text}）",
                flush=True,
            )
        else:
            print(f"[{account}] 共享代理出口 {proxy['server']}（不占用账号专属 ISP 额度）", flush=True)
        kwargs["proxy"] = proxy
    bridge = None
    try:
        if settings['proxy_chain'] or settings['isp_transport']:
            first = config._parse_proxy(settings['proxy_chain']) if settings['proxy_chain'] and not config.ISP_ONLY else None
            if not proxy or (settings['proxy_chain'] and not first and not config.ISP_ONLY):
                raise ValueError('串联代理配置无效，已停止启动，未回退默认线路')
            bridge = ProxyChainBridge(proxy, first)
            await bridge.__aenter__()
            kwargs['proxy'] = bridge.browser_proxy
            print(f"[{account}] 独立 ISP 认证通道 -> {proxy['server']}；出口保持绑定 ISP", flush=True)
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    except BaseException:
        if bridge:
            await bridge.close()
        raise
    try:
        if bridge:
            async def close_bridge(_context):
                await bridge.close()
            context.on('close', close_bridge)
            await context.route('**/*', bridge.authorize_request)
        # route 注册顺序很关键：Playwright 是后注册先执行（LIFO）。
        # duration 改写器必须先注册，counter 后注册并用 fallback 把
        # /chat/completion 交给它，否则改写器会被 counter 吞掉。
        if native_duration is not None:
            script = Path(__file__).with_name('web').joinpath('dola-native30.js').read_text(encoding='utf-8')
            if native_model_key == 'seedance_v2.0' and native_duration in (15, 30):
                script += '\n' + Path(__file__).with_name('web').joinpath('dola-fast15-native.js').read_text(encoding='utf-8')
            await context.add_init_script(
                f'globalThis.__DOLA_NATIVE_DURATION_TARGET__ = {native_duration};\n'
                f'globalThis.__DOLA_NATIVE_MODEL_TARGET__ = {json.dumps(native_model_key)};\n' + script)
        elif duration is not None:
            await install_duration_rewriter(context, duration)
        if proxy and own_isp:
            await proxy_usage.attach_request_counter(context, account, proxy)
            # CDN 分流必须在计数器之后注册（Playwright route 是 LIFO，后注册先执行）：
            # CDN 静态资源直接走本地快线 fulfill，不进计数器、不扣 ISP 额度；
            # 其余请求照常落进 计数器 → duration 改写器 → 网络。
            if config.ISP_ONLY:
                await install_cdn_accelerator(context, proxy=proxy, account=account)
            elif settings['cdn_via_local']:
                await install_cdn_accelerator(context)
        await _inject_saved_cookies(context, account)
        await sync_site_language(context, settings['site_language'])
    except BaseException as setup_error:
        try:
            await context.close()
        except BaseException as close_error:
            if bridge:
                await bridge.close()
            if isinstance(setup_error, BaseException) and not isinstance(setup_error, Exception):
                setup_error.browser_cleanup_failed = True
                raise setup_error from close_error
            error = BrowserCleanupError('浏览器初始化失败且未确认关闭，已隔离线路')
            error.browser_cleanup_failed = True
            raise error from close_error
        if bridge:
            await bridge.close()
        raise
    return context


async def install_cdn_accelerator(context, *, proxy=None, account=None):
    """Cache only public assets; strict misses use/count the bound ISP.

    Legacy mode retains its explicit CDN route for rollback. Connections and
    in-flight tasks belong to one browser; only fresh public JS/CSS persist.
    """
    if config.ISP_ONLY:
        config.require_isp_proxy(proxy)
        local = config._proxy_to_url(proxy)
    else:
        local = (config.CDN_LOCAL_PROXY or "").strip()
    if not local:
        return None
    accelerator = PublicAssets(context, local, timeout=config.CDN_LOCAL_TIMEOUT,
                               isp_proxy=proxy if config.ISP_ONLY else None)
    await accelerator.__aenter__()

    async def cleanup(_context):
        await accelerator.__aexit__()

    context.on('close', cleanup)
    return accelerator


async def install_duration_rewriter(context, duration):
    """Fast 5/10 秒旧流程的兼容改写器；Fast 15 秒和 2.5 不安装此路由。

    Dola 真实提交体把时长放在 chat_ability.ability_param（一个 JSON 字符串）里，
    形如 {"model":"seedance_v2.0","duration":30,...}。

    为什么不靠扩展：Patchright 自己通过 CDP 控制 Chromium，扩展再调
    chrome.debugger.attach 会被拒绝（Another debugger is already attached），
    导致 Fetch.enable 从未生效。context.route 在请求发出前确定性改写，
    这不证明平台接受该时长；2.5 的 15/30 秒由网页配置初始化、原请求核对和实际成片验证。
    """
    if duration is None:
        return

    target = int(duration)

    async def _handler(route):
        request = route.request
        try:
            if "/chat/completion" not in request.url:
                await route.continue_()
                return

            raw = request.post_data
            if not raw:
                await route.continue_()
                return

            try:
                body = json.loads(raw)
            except Exception:
                await route.continue_()
                return

            changed = False
            ability = body.get("chat_ability")
            if isinstance(ability, dict):
                param = ability.get("ability_param")
                parsed = None
                if isinstance(param, str):
                    try:
                        parsed = json.loads(param)
                    except Exception:
                        parsed = None
                elif isinstance(param, dict):
                    parsed = param
                if isinstance(parsed, dict) and parsed.get("duration") != target:
                    parsed["duration"] = target
                    ability["ability_param"] = json.dumps(
                        parsed, ensure_ascii=False, separators=(",", ":")
                    )
                    changed = True

            if changed:
                await route.continue_(
                    post_data=json.dumps(body, ensure_ascii=False, separators=(",", ":"))
                )
                print(
                    f"[duration-rewriter] /chat/completion duration -> {target}s",
                    flush=True,
                )
            else:
                await route.continue_()
        except Exception as exc:
            print(f"[duration-rewriter] 改写失败，放行原请求: {str(exc)[:120]}", flush=True)
            try:
                await route.continue_()
            except Exception:
                pass

    await context.route("**/chat/completion**", _handler)


COOKIE_DOMAIN = ".dola.com"


async def _inject_saved_cookies(context, account: str):
    """把 accounts/<account>/cookies.json 里的 cookie 注入 context。

    patchright 的 add_cookies 不会写入持久化 profile（实测关掉浏览器后丢失），
    因此每次启动都要重新注入；已存在同名 cookie 则跳过，避免覆盖站点轮换过的新值。
    """
    path = Path("accounts") / account / "cookies.json"
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    existing = set()
    try:
        for c in await context.cookies():
            existing.add(c["name"])
    except Exception:
        pass
    payload = [
        {"name": d["name"], "value": d["value"], "domain": COOKIE_DOMAIN, "path": "/",
         "secure": True, "httpOnly": False, "sameSite": "Lax"}
        for d in data
        if isinstance(d, dict) and d.get("name") and d.get("name") not in existing
    ]
    if not payload:
        return 0
    try:
        await context.add_cookies(payload)
        return len(payload)
    except Exception:
        n = 0
        for c in payload:
            try:
                await context.add_cookies([c])
                n += 1
            except Exception:
                pass
        return n


def cookie_value(cookies: list, name: str) -> str:
    """从 context.cookies() 结果里取指定 cookie 的值"""
    return next((c["value"] for c in cookies if c["name"] == name and c["value"]), "")

# 登录墙文案：未登录 / 掉登录时 Dola 会弹出「登录解锁更多功能」的模态框，
# 盖住底部技能条，导致后续出片流程全部失败。命中即判定为失效号。
LOGIN_WALL_TEXTS = (
    "Melde dich an", "Anmelden",
    "Sign in", "Log in", "登录", "登入",
)
# 登录墙「专属特征」：只出现在登录墙上的短语（如德语「解锁更多功能」）。
# 2026-10-07 实测教训：单看「Anmelden/Sign in」会误杀正常号——已登录页面的
# 隐藏节点/菜单模板里也有这些词。必须「登录动作词 + 专属特征」双条件同时命中。
LOGIN_WALL_FEATURE_TEXTS = (
    "um mehr Features", "更多功能", "unlock more features", "unlock more",
)

# 网络 / 代理类故障标记。命中这些字样说明「页面根本没打开」，
# 绝不能当成掉登录，否则会误判并连带删除账号。
NETWORK_ERROR_MARKERS = (
    "ERR_PROXY", "ERR_TUNNEL", "ERR_CONNECTION", "ERR_TIMED_OUT",
    "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED", "ERR_NETWORK_CHANGED",
    "ERR_INVALID_AUTH_CREDENTIALS", "ERR_SOCKS", "ERR_EMPTY_RESPONSE",
    "ERR_CERT", "ERR_SSL", "ERR_ADDRESS_UNREACHABLE", "ERR_TOO_MANY_REDIRECTS",
    "net::", "Timeout", "timed out",
    "TargetClosedError", "Target page, context or browser has been closed",
    "Browser has been closed", "Connection closed",
    "ProxyQuotaExceeded", "ISP 请求被拦截", "额度已用完",
)


def _is_network_error(exc) -> bool:
    """异常是否属于网络 / 代理 / 浏览器崩溃类（无法据此判断登录态）。"""
    if exc is None:
        return False
    msg = f"{type(exc).__name__}: {exc}"
    return any(m in msg for m in NETWORK_ERROR_MARKERS)


def _note_unknown(account: str, reason: str, exc=None):
    if _is_network_error(exc):
        reason = f"{reason} [网络/代理故障]"
    detail = f"（{type(exc).__name__}: {str(exc)[:160]}）" if exc is not None else ""
    print(f"[{account}] 登录检测无法判定：{reason}{detail}", flush=True)


async def _page_shows_network_error(page) -> bool:
    """页面是否停在浏览器网络错误页（ERR_*），而非 Dola 正常内容。"""
    try:
        return bool(await page.evaluate(
            "() => {"
            "  try {"
            "    if (document.querySelector('#main-frame-error, .neterror, #net-error')) return true;"
            "    const t = (document.body && document.body.innerText) || '';"
            "    return /ERR_[A-Z_]{3,}|This site can.t be reached|无法访问此网站|Die Website ist nicht erreichbar/.test(t);"
            "  } catch (e) { return false; }"
            "}"
        ))
    except Exception:
        return False


async def _has_login_wall(page) -> bool:
    """检测页面上是否出现 Dola 的登录墙弹窗（未登录/掉登录时出现）。

    双条件判定：登录墙专属特征（「um mehr Features」等）+ 登录动作词
    （Anmelden/Sign in 等）必须同时命中，才判定为登录墙。单看动作词会
    误杀正常号（已登录页面隐藏节点里也有这些词）。
    """
    # 第一层：专属特征是否可见（这是登录墙的精确信号）
    feature = False
    for text in LOGIN_WALL_FEATURE_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    if await loc.nth(i).is_visible():
                        feature = True
                        break
                except Exception:
                    pass
            if feature:
                break
        except Exception:
            pass
    if not feature:
        return False
    # 第二层：登录动作词是否同时可见
    for text in LOGIN_WALL_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    if await loc.nth(i).is_visible():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


# 过载/限流横幅文本（与 video_worker_ui.OVERLOAD_TEXTS 同源，browser.py 独立使用）
_OVERLOAD_TEXTS = (
    "目前需求非常高", "需求非常高", "请稍后再试", "稍后再试",
    "too much demand", "high demand", "try again later",
    "Nachfrage", "später erneut", "später noch einmal",
)

# 生日/年龄验证弹窗：只有登录态正常才会要求补生日，出现即说明号活着。
_BIRTHDAY_RE = re.compile(r"Geburtstag|Geburtsdatum|date of birth|birthday", re.IGNORECASE)


async def _probe_overload(page) -> bool:
    """检测过载横幅（命中 = 服务端限流，不能作为掉号依据）。"""
    for text in _OVERLOAD_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(await loc.count()):
                try:
                    if await loc.nth(i).is_visible():
                        return True
                except Exception:
                    pass
        except Exception:
            pass
    return False


async def check_login_state(account: str):
    """Read-only account-info verification shared by the CLI and account panel.

    True means an authenticated identity; False requires explicit authentication
    denial; None preserves the cached state. Never send a test chat or interpret
    an unresponsive input, browser timeout or missing cookie as logout.
    """
    from account_login import check_account_login
    return await check_account_login(account)
