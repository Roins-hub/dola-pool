"""dola-pool 配置：全部走环境变量，带默认值。"""
import os
from pathlib import Path


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

# 同时跑的视频任务上限（0 = 不限制）。这是全局唯一一道并发闸门。
# 设正数可防风控 / 防同号并发，但会限制吞吐。
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "3"))

# 全局待处理任务上限（queued + processing），0 = 不限制。
MAX_PENDING_TASKS = int(os.getenv("DOLA_MAX_PENDING_TASKS", "100"))

# 视频生成超时（秒）
VIDEO_TIMEOUT = int(os.getenv("DOLA_VIDEO_TIMEOUT", "300"))
# 单个任务的总时限（秒）：在此时间内持续换号调度，超时即判定生成失败。
TASK_DEADLINE = int(os.getenv("DOLA_TASK_DEADLINE", "600"))
# 单个任务最多尝试的账号次数；0 表示不限次数，只受 TASK_DEADLINE 约束。
VIDEO_MAX_ATTEMPTS = int(os.getenv("DOLA_VIDEO_MAX_ATTEMPTS", "0"))

# SQLite 任务库
DB_PATH = os.getenv("DOLA_DB_PATH", "tasks.db")

# 号池配额/账号元数据库（代理记录也放这里）
POOL_DB_PATH = os.getenv("DOLA_POOL_DB_PATH", "pool_usage.db")

# 视频下载目录（出片后下载转存，FastAPI 以静态文件方式对外提供）
DOWNLOAD_DIR = os.getenv("DOLA_DOWNLOAD_DIR", "downloads")

# 浏览器显式代理（P0 结论：不能依赖系统代理，Clash 规则变化会把 dola 分流到直连被墙）。
# 必须是指向 JP/KR 出口的代理；留空 = 跟随系统代理（仅调试用）。
PROXY = os.getenv("DOLA_PROXY", "http://127.0.0.1:7890")

# 浏览器是否无头运行（login.py 永远有头）
HEADLESS = os.getenv("DOLA_HEADLESS", "1") == "1"

# 对外返回视频 URL 的基址（FastAPI 静态文件服务）
PUBLIC_BASE = os.getenv("DOLA_PUBLIC_BASE", f"http://127.0.0.1:{PORT}")

# 管理面板密码（留空 = 面板不鉴权，开发模式）
ADMIN_KEY = os.getenv("DOLA_ADMIN_KEY", "")


# Dola 30 秒/无水印 Chromium 扩展（unpacked extension）
EXTENSION_DIR = os.getenv("DOLA_EXTENSION_DIR", "extensions/dola30")
EXTENSION_ENABLED = os.getenv("DOLA_EXTENSION_ENABLED", "1") == "1"

# Cookie 导入账号启动时使用 Windows 11 Edge 126 的 UA/启动参数伪装
EDGE_FINGERPRINT_ENABLED = os.getenv("DOLA_EDGE_FINGERPRINT", "1") == "1"

# 「验证」按钮对 Google 登录账号追加的聊天探活（账号声誉/风控预检）：
# 发送一句问候，看是否正常回复，还是发送后立刻被踢成未登录。
VERIFY_CHAT_PROBE = os.getenv("DOLA_VERIFY_CHAT_PROBE", "1") == "1"
VERIFY_CHAT_PROMPT = os.getenv("DOLA_VERIFY_CHAT_PROMPT", "你好")
VERIFY_CHAT_WINDOW = int(os.getenv("DOLA_VERIFY_CHAT_WINDOW", "30"))

# Dola 每日限流恢复时区；默认按日本时间恢复。
LIMIT_RESET_TZ = os.getenv("DOLA_LIMIT_RESET_TZ", "Asia/Tokyo")

# Dola 每日免费额度刷新的钟点（LIMIT_RESET_TZ 时区的小时，默认日本时间 00:00）。
# 号池的「额度日」计数与每日自动重置都按这个时点滚动。
# 2026-09-15 修正：原先按 11:00 记界，实测上游不是 11:00 刷新（ck05 于 10:37 JST 用满 4 点，
# 13:31 JST 仍回「今天的生成次数已经达到上限」），改按当地零点对齐；
# 更权威的口径是上游回执里的「今日剩余 N 个视频生成额度」，会实时回写到账号额度上。
LIMIT_RESET_HOUR = int(os.getenv("DOLA_LIMIT_RESET_HOUR", "0"))

# 生成前余额预检的保守最低积分；Dola 当前 2.5/30s 实测成本为 2。
VIDEO_REQUIRED_POINTS = int(os.getenv("DOLA_VIDEO_REQUIRED_POINTS", "2"))

# 30 秒要「一气呵成」：默认**不**把两段 15 秒拼成 30 秒。
# 上游只给短视频（例如 30 秒请求只回 15 秒）时按失败处理、换号重试，
# 直到拿到原生 30 秒成片（实测 ck1001/ck1002/ck1008 都是单次请求直接出 30.09s）。
# 需要恢复旧的「两段拼接」兜底时，把 DOLA_ALLOW_30S_PAIR 设为 1。
ALLOW_30S_PAIR = os.getenv("DOLA_ALLOW_30S_PAIR", "0") == "1"

# 每账号每日视频生成额度（点数；官方每日免费额度随账号/区域浮动，超出后 Dola 会返回每日上限错误自动停号）
DAILY_LIMIT = int(os.getenv("DOLA_DAILY_LIMIT", "4"))

# ---- 模型-时长额度规则（2026-09-15 按运营口径：只看模型，不看时长）----
# 每次出片消耗的点数 = MODEL_DURATION_COSTS[模型][时长]；账号每日共 DAILY_LIMIT 点（默认 4 点）。
# seedance-2.5 支持 5s/10s/30s；seedance-2.0 仅支持 5s/10s/15s；其它时长一律拒绝。
# 记账口径（用户 2026-09-15 确认）：
#   seedance-2.5 一条视频 = 2 点 → 一个号一天能出 2 条；
#   seedance-2.0 一条视频 = 3 点 → 一个号一天能出 1 条（剩 1 点不够第二条）。
# 注意：上游回执里的「将消耗 N 个视频生成额度」是**报价**，与实际扣点并不一致
# （30 秒报价 6 点、实扣 2 点；2.5 的 10 秒档报价 4 点、实测也确实扣满一天 4 点），
# 所以本表是记账口径；上游真实余额由回执里的「今日剩余 N 个」实时回写校正
# （见 browser_pool._set_credit_balance），两者配合不会把号用超。
MODEL_DURATION_COSTS = {
    "seedance-2.5": {5: 2, 10: 2, 30: 2},
    "seedance-2.0": {5: 3, 10: 3, 15: 3},
}
ALL_DURATIONS = sorted({d for costs in MODEL_DURATION_COSTS.values() for d in costs})


# 公网参考图片下载限制
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))

# 带参考图片的任务给 Dola 更长的异步生成窗口（秒）。
REFERENCE_VIDEO_TIMEOUT = int(os.getenv("DOLA_REFERENCE_VIDEO_TIMEOUT", "900"))

# 参考图上传要打金山 CDN（imagex-*.bytevcloudapi.com），实测部分代理出口对该域名
# CONNECT 直接 403。1 = 上传段遇代理拦截时只用直连重传一次（生成仍走账号代理）。
REFERENCE_UPLOAD_DIRECT_FALLBACK = os.getenv("DOLA_REFERENCE_UPLOAD_DIRECT_FALLBACK", "1") == "1"

# ===== 号池工程化增强（移植自 api-pool，默认关闭/不破坏现有行为）=====

# 同出口隔离：同一代理出口（host:port）同时只允许一个号提交。
# 多个号共用同一个旋转网关时建议开 true，避免风控把同一出口并发送勤打断。
ISOLATE_SHARED_EGRESS = os.getenv("DOLA_ISOLATE_SHARED_EGRESS", "0") == "1"

# 连续失败分达到该值则冷却该号；0 = 不按失败分冷却。
FAIL_SCORE_CAP = int(os.getenv("DOLA_FAIL_SCORE_CAP", "0"))

# 上游「当前服务访问频繁」这类瞬时限流的冷却时长（秒）。
# 这类限流是出口 IP/上游抖动，不该把账号按「失败」锁到次日额度刷新。
TRANSIENT_COOLDOWN_SEC = int(os.getenv("DOLA_TRANSIENT_COOLDOWN_SEC", "600"))

# 同一号两次向上游提交的最小间隔（秒）；0 = 不限制。
MIN_SUBMIT_INTERVAL_SECONDS = int(os.getenv("DOLA_MIN_SUBMIT_INTERVAL_SECONDS", "0"))

# 提交前随机抖动上限（秒），减轻多号同时打上游；0 = 不抖动。
SUBMIT_JITTER_SECONDS = int(os.getenv("DOLA_SUBMIT_JITTER_SECONDS", "0"))

# 动态代理账号（proxy_store 里的 proxies）轮换刷新周期（秒）；0 = 静态绑定不轮换。
PROXY_ROTATION_INTERVAL_SECONDS = int(os.getenv("DOLA_PROXY_ROTATION_INTERVAL_SECONDS", "0"))

# 探活间隔（有任务时） / 空闲探活间隔（无排队无占用时，秒）。
HEALTH_INTERVAL = int(os.getenv("DOLA_HEALTH_INTERVAL", "120"))
HEALTH_IDLE_INTERVAL = int(os.getenv("DOLA_HEALTH_IDLE_INTERVAL", "600"))

# 同/近一次探活的最小间隔（秒）。避免空转打代理。
PROBE_INTERVAL_SECONDS = int(os.getenv("DOLA_PROBE_INTERVAL_SECONDS", "120"))

# 同时最多打开多少个号去提交/占用。0 = 不限制。
MAX_OPEN_ACCOUNTS = int(os.getenv("DOLA_MAX_OPEN_ACCOUNTS", "0"))

# ===== 纯 API 出片（cookie 账号，对齐 dola-pool-cookie）=====
# cookie 来源账号（source=cookie 且有 cookie_state.json）出片走纯 API + bdms 签名，
# 不再依赖浏览器 profile。1 = 开启；0 = 全部走浏览器。
PURE_API_ENABLED = os.getenv("DOLA_PURE_API", "1") == "1"
# 纯 API 失败时是否回退浏览器出片（避免一下子把 cookie 账号全打断）。
PURE_API_FALLBACK_BROWSER = os.getenv("DOLA_PURE_FALLBACK_BROWSER", "1") == "1"
# 纯 API 协议参数（对齐 dola-pool-cookie 的 region /pc_version）。
PURE_API_REGION = os.getenv("DOLA_PURE_REGION", "JP")
PURE_API_PC_VERSION = os.getenv("DOLA_PURE_PC_VERSION", "3.33.11")
# 是否走无水印（第三方 nowatermark 解析）。
PURE_API_REMOVE_WATERMARK = os.getenv("DOLA_PURE_REMOVE_WATERMARK", "1") == "1"
# 纯 API 出片轮询/下载目录（复用 DOWNLOAD_DIR）。
PURE_API_TIMEOUT = int(os.getenv("DOLA_PURE_TIMEOUT", "900"))
PURE_API_POLL_INTERVAL = int(os.getenv("DOLA_PURE_POLL_INTERVAL", "30"))
