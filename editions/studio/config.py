"""dola-pool 配置：全部走环境变量，带默认值。"""
import json
import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins."""
    path = Path(__file__).with_name(".env.local")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()
HOST = os.getenv("DOLA_HOST", "0.0.0.0")
PORT = int(os.getenv("DOLA_PORT", "8000"))

# 服务对外 API Key（逗号分隔多个；留空 = 不鉴权，仅内网调试用）
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# 号池 cookie 文件（一行一个 dola.com cookie）
COOKIES_FILE = os.getenv("DOLA_COOKIES_FILE", "cookies.txt")
DOLA_ACCOUNTS_DIR = os.getenv('DOLA_ACCOUNTS_DIR', 'accounts')
DOLA_POOL_DB_PATH = os.getenv('DOLA_POOL_DB_PATH', 'pool_usage.db')

# 同时打开的提交浏览器数量；受理并关闭浏览器后释放。
MAX_CONCURRENCY = max(1, int(os.getenv("DOLA_MAX_CONCURRENCY", "5")))
# 提交中 + 后台生成/下载中的任务总数；同一物理 ISP 完成后才接下一条。
MAX_INFLIGHT = max(1, int(os.getenv("DOLA_MAX_INFLIGHT", "5")))
POLL_INTERVAL = max(5, int(os.getenv("DOLA_POLL_INTERVAL", "30")))
MAX_DOWNLOADS = max(1, int(os.getenv("DOLA_MAX_DOWNLOADS", "1")))

# 全局待处理任务上限（queued + processing），0 = 不限制。
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))
DOUBAO_DATA_DIR = os.getenv('DOUBAO_DATA_DIR', 'data/doubao')
DOUBAO_ENABLED = os.getenv('DOUBAO_ENABLED', '0') == '1'
DOUBAO_MAX_INFLIGHT = max(1, int(os.getenv('DOUBAO_MAX_INFLIGHT', '1')))
DOUBAO_VIDEO_TIMEOUT = max(60, int(os.getenv('DOUBAO_VIDEO_TIMEOUT', '7200')))
# An explicit Doubao-only local tunnel, separate from the system/Dola proxy.
DOUBAO_LOCAL_PROXY_URL = os.getenv('DOUBAO_LOCAL_PROXY_URL', '').strip()
DOUBAO_LOCAL_PROXY_LABEL = os.getenv('DOUBAO_LOCAL_PROXY_LABEL', '香港代理（豆包专用）').strip() or '香港代理（豆包专用）'
DOUBAO_LOCAL_PROXY_MAP = os.getenv('DOUBAO_LOCAL_PROXY_MAP', '{}')


def _doubao_http_loopback(raw):
    """A local allowance admits exactly one HTTP endpoint, never browser options."""
    try:
        if not isinstance(raw, str):
            return None
        parsed = urlsplit(raw)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment
                or raw != f'http://127.0.0.1:{parsed.port}'):
            return None
    except (TypeError, ValueError):
        return None
    return {'server': raw}


def doubao_local_proxy():
    """Keep the original single-endpoint configuration compatible."""
    return _doubao_http_loopback(DOUBAO_LOCAL_PROXY_URL)


def doubao_local_proxy_options():
    """Named Doubao-only loopback routes; a malformed extra map admits none."""
    choices = {}
    legacy = doubao_local_proxy()
    if legacy is not None:
        choices['hk'] = {**legacy, 'label': DOUBAO_LOCAL_PROXY_LABEL, 'country': '香港'}

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        raw = DOUBAO_LOCAL_PROXY_MAP
        if not isinstance(raw, str) or len(raw) > 16384:
            raise ValueError()
        source = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(source, dict) or len(source) > 16:
            raise ValueError()
        extra = {}
        servers = {option['server'] for option in choices.values()}
        for key, option in source.items():
            if (not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', key) or key in ('hk', 'direct')
                    or not isinstance(option, dict) or set(option) != {'server', 'label', 'country'}
                    or _doubao_http_loopback(option['server']) is None or option['server'] in servers):
                raise ValueError()
            if any(not isinstance(option[field], str) or not option[field].strip()
                   or len(option[field]) > 80 or not option[field].isprintable()
                   for field in ('label', 'country')):
                raise ValueError()
            extra[key] = dict(option)
            servers.add(option['server'])
    except (ValueError, TypeError):
        return choices
    return {**choices, **extra}

# 默认视频模型：seedance-2.5（高质量，低峰用）或 seedance-2.0（fast，高峰用，约 10 分钟出片）。
# 请求未显式传 model 时使用此默认值；高峰期想切 fast 只需改 .env.local 后重启。
DEFAULT_MODEL = os.getenv("DOLA_DEFAULT_MODEL", "seedance-2.0")

# Experimental one-shot HTTP transport for explicitly selected 2.5 / 30s only.
PURE_HTTP_VIDEO = os.getenv("DOLA_PURE_HTTP_VIDEO", "0") == "1"

# 用户要求：已提交视频最多等待两小时，出片或明确拒绝时提前结束。
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "7200"))

# 排队停滞超时（秒）：任务进入 processing 后，若超过此时长仍未派发到任何账号
# （account 为空、conversation_id 为空），判为排队超时失败，避免产生永久悬挂的僵尸任务。
QUEUE_STALL_SECONDS = int(os.getenv("DOLA_QUEUE_STALL_SECONDS", "600"))

# 排队等待超时（秒）：任务保持 queued 超过此时长仍未派发（并发位长期不足），
# 判为排队超时失败。应大于"最慢任务时长 × 并发轮数"，避免误杀正常排队任务。
QUEUE_WAIT_SECONDS = int(os.getenv("DOLA_QUEUE_WAIT_SECONDS", "1800"))

# 30 秒视频沿用同样的两小时等待上限。
LONG_VIDEO_TIMEOUT = int(os.getenv("DOLA_LONG_VIDEO_TIMEOUT", "7200"))

# 底部技能条「创建视频」入口是懒加载，高峰期首屏很慢。等不到入口时不能急着刷新
# （刷新会把刚渲染出来的入口清掉，永远找不到）；这是单次等待入口出现的秒数。
ENTRY_WAIT_SECONDS = int(os.getenv("DOLA_ENTRY_WAIT_SECONDS", "240"))

# 打开聊天页的导航超时（毫秒）与重试次数。高峰期/出口代理抖动时 domcontentloaded 实测
# 可达 80s+，之前 60s×5 会把其实能加载的页面判死，导致整单失败。这里放宽并允许跨号重试。
GOTO_TIMEOUT_MS = int(os.getenv("DOLA_GOTO_TIMEOUT_MS", "150000"))
GOTO_TRIES = int(os.getenv("DOLA_GOTO_TRIES", "2"))

# 发送 prompt 后等待 URL 变成数字 conversation_id 的秒数。
# 慢速 ISP 出口实测：Dola 受理并跳转可能 >30s，等太短会把已发送成功的任务白白丢弃
# （ISP 额度已花、账号配额已扣，却没有 conversation_id 无法恢复轮询）。
CONV_ID_WAIT_SECONDS = int(os.getenv("DOLA_CONV_ID_WAIT_SECONDS", "120"))

# 号池在“打开页面”阶段连续失败多少个号就中止。防止出口代理整体挂掉时
# 300 个号轮流空转几小时；留几次重试是为了容忍个别出口抖动。
POOL_MAX_PRESESSION_FAILURES = int(os.getenv("DOLA_POOL_MAX_PRESESSION_FAILURES", "5"))

# SQLite 任务库
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# 视频下载目录（出片后下载转存，FastAPI 以静态文件方式对外提供）
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# 浏览器显式代理（P0 结论：不能依赖系统代理，Clash 规则变化会把 dola 分流到直连被墙）。
# 建议指向德国出口的代理；留空 = 跟随系统代理（仅调试用）。
PROXY = os.getenv("DOLA_PROXY", "http://127.0.0.1:7890")

# Strict route policy for this project's account traffic. No global/direct fallback.
ISP_ONLY = os.getenv('DOLA_ISP_ONLY', '0') == '1'
PUBLIC_ASSET_CACHE_DIR = os.getenv('DOLA_PUBLIC_ASSET_CACHE_DIR', 'cache/public-assets')
PUBLIC_ASSET_CACHE_MB = max(8, int(os.getenv('DOLA_PUBLIC_ASSET_CACHE_MB', '256')))

# ---- CDN 分流加速（解决德国 ISP 打开 dola「UI 不弹出来一直转圈」）----
# dola 页面的 ~180 个 JS bundle 全部来自字节自家 CDN（sf-flow-web-cdn.ciciai.com 等）；
# 实测德国静态 ISP 的 IP 段被该 CDN 挂起/限流（一次加载全部 hang 10s+），其中包含
# 底部技能条（创建视频入口）的 JS——表现就是「UI 永远出不来，一直转圈」。
# 旧版公开 CDN 分流；ISP_ONLY 开启时强制覆盖为账号绑定 ISP。
CDN_VIA_LOCAL = os.getenv("DOLA_CDN_VIA_LOCAL", "1") not in ("0", "false", "False")
CDN_LOCAL_PROXY = os.getenv("DOLA_CDN_LOCAL_PROXY", PROXY)
CDN_LOCAL_TIMEOUT = int(os.getenv("DOLA_CDN_LOCAL_TIMEOUT", "45"))

# 浏览器是否无头运行（login.py 永远有头）
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"

# 生成浏览器恢复已验证的德文流程；管理后台和用户提示词不受影响。
BROWSER_LOCALE = os.getenv("DOLA_BROWSER_LOCALE", "de-DE")
BROWSER_TIMEZONE = os.getenv("DOLA_BROWSER_TIMEZONE", "Europe/Berlin")
BROWSER_ACCEPT_LANGUAGE = os.getenv(
    "DOLA_BROWSER_ACCEPT_LANGUAGE", "de-DE,de;q=0.9,en;q=0.8")
# 接口语言与界面一致；region / sys_region 保持原出口地区。
BROWSER_LANGUAGE = os.getenv("DOLA_BROWSER_LANGUAGE", "de")
BROWSER_REGION = os.getenv("DOLA_BROWSER_REGION", "DE")

# ===== 按账号代理（一号一 IP / 几号一 IP）=====
# 优先级：accounts/<name>/proxy.txt  >  代理映射表  >  上面的 DOLA_PROXY 全局回退。
# 映射来源（后者覆盖前者）：proxies.json 文件、环境变量 DOLA_PROXY_MAP（JSON）。
# 支持写法：
#   http://user:pass@1.2.3.4:8080     socks5://user:pass@1.2.3.4:1080
#   1.2.3.4:8080                      1.2.3.4:8080:user:pass
#   user:pass@1.2.3.4:8080
# 「几号一 IP」= 让多个账号在映射里指向同一个代理串即可（成本更低）。
PROXY_MAP_FILE = os.getenv("DOLA_PROXY_MAP_FILE", "proxies.json")

# ===== ISP / 静态代理请求统计 =====
# 流量/时长套餐可关闭本地次数限制；仍统计请求并保留手动暂停能力。
PROXY_REQUEST_LIMIT_ENABLED = os.getenv("DOLA_PROXY_REQUEST_LIMIT_ENABLED", "true").lower() not in ("0", "false", "no", "off")
# 每条 ISP 的总请求次数上限；达到后自动暂停该出口，不再发请求。
PROXY_REQUEST_LIMIT = int(os.getenv("DOLA_PROXY_REQUEST_LIMIT", "1000"))
# 预警阈值：已用达到该值面板标黄提醒（默认 900，留 100 次缓冲）。
PROXY_WARN_AT = int(os.getenv("DOLA_PROXY_WARN_AT", "900"))
# 独立额度库，避免与 tasks.db / pool_usage.db 抢锁。
PROXY_USAGE_DB = os.getenv("DOLA_PROXY_USAGE_DB", "proxy_usage.db")


def _parse_proxy(raw: str) -> dict | None:
    """把一行代理配置解析成 patchright 需要的 proxy 字典。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    # 带 scheme：http://user:pass@host:port、socks5://host:port ...
    if "://" in raw:
        u = urlsplit(raw)
        if not u.hostname:
            return None
        server = f"{u.scheme}://{u.hostname}"
        if u.port:
            server += f":{u.port}"
        proxy = {"server": server}
        if u.username:
            proxy["username"] = unquote(u.username)
        if u.password:
            proxy["password"] = unquote(u.password)
        return proxy
    # user:pass@host:port
    if "@" in raw:
        cred, host = raw.rsplit("@", 1)
        if ":" not in host:
            return None
        proxy = {"server": host if "://" in host else f"http://{host}"}
        if ":" in cred:
            user, pwd = cred.split(":", 1)
            proxy["username"] = user
            proxy["password"] = pwd
        else:
            proxy["username"] = cred
        return proxy
    parts = raw.split(":")
    # host:port
    if len(parts) == 2 and parts[0] and parts[1].isdigit():
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    # host:port:user:pass（1024proxy / 站大爷等静态 ISP 常用格式）
    if len(parts) == 4 and parts[0] and parts[1].isdigit():
        host, port, user, pwd = parts
        return {"server": f"http://{host}:{port}", "username": user, "password": pwd}
    return None


def _proxy_to_url(proxy: dict) -> str:
    """把 proxy 字典还原成 aiohttp 用的 URL（含账号密码）。"""
    server = proxy["server"]
    if "://" not in server:
        server = "http://" + server
    if not proxy.get("username"):
        return server
    scheme, rest = server.split("://", 1)
    cred = quote(proxy["username"], safe="")
    if proxy.get("password"):
        cred += ":" + quote(proxy["password"], safe="")
    return f"{scheme}://{cred}@{rest}"


def _load_proxy_map() -> dict:
    """读取 账号→代理 映射；proxies.json 与环境变量 DOLA_PROXY_MAP（JSON）合并。"""
    mapping: dict[str, str] = {}
    path = Path(PROXY_MAP_FILE)
    if not path.is_absolute():
        path = Path(__file__).with_name(PROXY_MAP_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                mapping.update({str(k): str(v).strip()
                                for k, v in data.items() if isinstance(v, str) and v.strip()})
        except Exception:
            pass
    env = os.getenv("DOLA_PROXY_MAP", "").strip()
    if env:
        try:
            data = json.loads(env)
            if isinstance(data, dict):
                mapping.update({str(k): str(v).strip()
                                for k, v in data.items() if isinstance(v, str) and v.strip()})
        except Exception:
            pass
    return mapping


def proxy_for(account: str | None) -> dict | None:
    """返回账号专属代理字典；没有专属配置时回退全局 DOLA_PROXY。

    优先级：accounts/<name>/proxy.txt > 映射表(proxies.json / DOLA_PROXY_MAP) > DOLA_PROXY。
    """
    name = (account or "").strip()
    if ISP_ONLY:
        raw, source = account_proxy_raw(name)
        per_file = Path('accounts') / name / 'proxy.txt'
        if per_file.is_file():
            raw, source = per_file.read_text(encoding='utf-8-sig').strip(), 'file'
        if source not in ('file', 'map'):
            raise ValueError('全 ISP 模式要求账号绑定独立 ISP，未使用默认代理或直连')
        try:
            return require_isp_proxy(_parse_proxy(raw))
        except (ValueError, TypeError):
            raise ValueError('账号绑定 ISP 配置无效，已停止请求') from None
    if name:
        per_file = Path("accounts") / name / "proxy.txt"
        if per_file.exists():
            proxy = _parse_proxy(per_file.read_text(encoding="utf-8-sig").strip())
            if proxy:
                return proxy
        mapping = _load_proxy_map()
        raw = mapping.get(name) or mapping.get("_default")
        if raw:
            proxy = _parse_proxy(raw)
            if proxy:
                return proxy
    return _parse_proxy(PROXY) if PROXY else None


def require_isp_proxy(proxy):
    """Validate a bound/snapshotted exit without exposing credentials in errors."""
    try:
        parsed = urlsplit(proxy['server'])
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or not parsed.port:
            raise ValueError()
        if parsed.hostname.lower() == 'localhost':
            raise ValueError()
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ValueError('全 ISP 模式需要有效的公网 HTTP ISP 绑定，已阻止默认线路') from None
    return proxy


def check_saved_isp(proxy_url, own_isp):
    """Resume the original verified ISP, or stop; never replace a historical route."""
    if ISP_ONLY:
        if not own_isp:
            raise ValueError('原会话缺少专属 ISP 记录，已停止请求，未切换线路')
        require_isp_proxy(_parse_proxy(proxy_url))


def proxy_url_for(account: str | None) -> str | None:
    """返回账号专属代理的 URL 形式（供 aiohttp 使用）；无则回退全局。"""
    proxy = proxy_for(account)
    return _proxy_to_url(proxy) if proxy else None

# 对外返回视频 URL 的基址（FastAPI 静态文件服务）
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# 管理面板密码（留空 = 面板不鉴权，开发模式）
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")

# ===== 邮箱登录 / 成员系统 =====
# 是否允许自助注册成员（第一个注册者永远是管理员，不受此开关限制）
ALLOW_SELF_REGISTER = os.getenv("DOLA_ALLOW_SELF_REGISTER", "0") == "1"
# 自助注册邀请码（留空 = 不校验；仅 ALLOW_SELF_REGISTER=1 时生效）
INVITE_CODE = os.getenv("DOLA_INVITE_CODE", "")
# 登录会话有效期（天）
SESSION_TTL_DAYS = int(os.getenv("DOLA_SESSION_TTL_DAYS", "30"))


# Dola 30 秒/无水印 Chromium 扩展（unpacked extension）
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Dola 每日限流恢复时区；默认按德国日期零点恢复。
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Europe/Berlin")

# 生成前余额预检的保守最低积分；Dola 当前 2.5/30s 实测成本为 2。
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))


# 公网参考图片下载限制
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_UPLOAD_DIR = os.getenv("DOLA_REFERENCE_UPLOAD_DIR", "uploads/references")
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))
# Local project limits; actual model/account support is checked by Dola's uploader.
REFERENCE_MEDIA_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_MEDIA_MAX_BYTES", str(50 * 1024 * 1024)))
REFERENCE_MEDIA_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_MEDIA_MAX_COUNT", "10"))

# 带参考图片的任务给 Dola 更长的异步生成窗口（秒）。
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "7200"))
# ===== 账号代理读写（管理面板）=====

def proxy_map_path() -> Path:
    path = Path(PROXY_MAP_FILE)
    return path if path.is_absolute() else Path(__file__).with_name(PROXY_MAP_FILE)


def _read_proxy_map_file() -> dict:
    path = proxy_map_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_proxy_map_file(data: dict) -> None:
    proxy_map_path().write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def account_proxy_raw(account: str) -> tuple[str, str]:
    """返回账号当前生效的 (代理串, 来源)。来源: file/map/map_default/global/none。"""
    name = (account or "").strip()
    if name:
        per_file = Path("accounts") / name / "proxy.txt"
        if per_file.exists():
            raw = per_file.read_text(encoding="utf-8-sig").strip()
            if raw:
                return raw, "file"
        mapping = _load_proxy_map()
        raw = mapping.get(name)
        if raw:
            return raw, "map"
        raw = mapping.get("_default")
        if raw:
            return raw, "map_default"
    if PROXY:
        return PROXY, "global"
    return "", "none"


def build_proxy_raw(scheme: str, host: str, port, username: str = "", password: str = "") -> str:
    """把分字段拼回一行代理串。"""
    scheme = (scheme or "http").strip().lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        scheme = "http"
    host = (host or "").strip()
    port = str(port or "").strip()
    if not host or not port:
        return ""
    cred = ""
    if username:
        cred = quote(username, safe="")
        if password:
            cred += ":" + quote(password, safe="")
        cred += "@"
    return f"{scheme}://{cred}{host}:{port}"


def save_account_proxy(account: str, raw: str, save_to: str = "map") -> dict:
    """保存账号专属代理。save_to: map(proxies.json) / file(accounts/<name>/proxy.txt)。"""
    raw = (raw or "").strip()
    proxy = _parse_proxy(raw)
    if not proxy:
        raise ValueError("无法解析代理串，请检查格式")
    if save_to == "file":
        d = Path("accounts") / account
        d.mkdir(parents=True, exist_ok=True)
        (d / "proxy.txt").write_text(raw + "\n", encoding="utf-8")
    else:
        data = _read_proxy_map_file()
        data[account] = raw
        _write_proxy_map_file(data)
    return proxy


def clear_account_proxy(account: str) -> None:
    """清除账号专属代理（专属文件 + 映射表），回退全局。"""
    per_file = Path("accounts") / account / "proxy.txt"
    if per_file.exists():
        per_file.unlink()
    data = _read_proxy_map_file()
    if account in data:
        data.pop(account, None)
        _write_proxy_map_file(data)


def import_proxy_map(entries: dict) -> tuple[int, int]:
    """批量写入 proxies.json；返回 (成功, 跳过)。"""
    data = _read_proxy_map_file()
    ok = skip = 0
    for k, v in entries.items():
        if not isinstance(v, str) or not v.strip() or not _parse_proxy(v):
            skip += 1
            continue
        data[str(k).strip()] = v.strip()
        ok += 1
    if ok:
        _write_proxy_map_file(data)
    return ok, skip
