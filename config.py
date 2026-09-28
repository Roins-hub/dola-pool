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
# 监听地址由 uvicorn 命令行给（--host/--port）；这里的 PORT 只用于 PUBLIC_BASE 的默认值。
PORT = int(os.getenv("DOLA_PORT", "8000"))

# 服务对外 API Key（逗号分隔多个；留空 = 不鉴权，仅内网调试用）
API_KEYS = [k.strip() for k in os.getenv("DOLA_API_KEYS", "").split(",") if k.strip()]

# 同时跑的视频任务上限（0 = 不限制，默认）。这是全局唯一一道并发闸门。
# 默认不限：任务提交后立即开跑，等待只可能来自「没有空闲账号」。
# 设正数可防风控 / 防同号并发，但会限制吞吐。
MAX_CONCURRENCY = int(os.getenv("DOLA_MAX_CONCURRENCY", "0"))

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

# ---- 模型额度规则（2026-09-22 对齐 dola-pool-cookie：只看模型，不看时长）----
# 每次出片消耗的点数**只看模型**，与时长无关；账号每日共 DAILY_LIMIT 点（默认 4 点）。
#   seedance-2.5 一条视频 = 2 点 → 一个号一天能出 2 条；
#   seedance-2.0 一条视频 = 3 点 → 一个号一天能出 1 条（剩 1 点不够第二条）。
# 未知模型按最贵的 DEFAULT_CREDIT_COST 算：宁可少派，也不要拿号去白撞一次
# 「额度不足」（与 dola-pool-cookie 的 credit_cost_for_model 同规则）。
# 注意：上游回执里的「将消耗 N 个视频生成额度」是**报价**，与实际扣点并不一致
# （30 秒报价 6 点、实扣 2 点；2.5 的 10 秒档报价 4 点、实测也确实扣满一天 4 点），
# 所以本表是记账口径；上游真实余额由回执里的「今日剩余 N 个」实时回写校正
# （见 browser_pool._set_credit_balance），两者配合不会把号用超。
MODEL_COSTS = {
    "seedance-2.5": 2,
    "seedance-2.0": 3,
}
DEFAULT_CREDIT_COST = 3

# seedance-2.0 只认这三个原生档位；其余（含 30）都必须走 2.5。
V20_DURATIONS = (5, 10, 15)

# ---- 时长白名单 ----
# 原生档位：上游 UI 直出的四个选项，**任何情况下都放行**。30 秒是线上主力流量，
# 绝不能被下面的上限夹掉（夹了就是把 30 秒片悄悄变成 15 秒片）。
NATIVE_DURATIONS = (5, 10, 15, 30)

# 任意时长（非原生档位）上限：0 = 关闭（非原生时长一律拒绝，保持旧行为）。
# >0 时允许 [MIN_DURATION, NATIVE_DURATION_MAX] 区间的非原生时长，且只能走
# seedance-2.5 —— 4~30 是 2.5 的能力；扩展实测就是改 ability_param.duration
# 并把 model 钉在 seedance_v2.5。
# 上游对号池回的话术是「4到15秒 / 超出了单条生成范围」，所以真实上限必须在线上一
# 档一档试出来：15 → 20 → 30。做成环境变量是为了不必每次重建。
MIN_DURATION = 4
NATIVE_DURATION_MAX = int(os.getenv("DOLA_NATIVE_DURATION_MAX", "0"))


def all_durations() -> list[int]:
    """当前允许下发的时长全集（原生档位永远在内）。"""
    if NATIVE_DURATION_MAX <= 0:
        return sorted(NATIVE_DURATIONS)
    return sorted(set(NATIVE_DURATIONS) | set(range(MIN_DURATION, NATIVE_DURATION_MAX + 1)))


ALL_DURATIONS = all_durations()


# 公网参考图片下载限制
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(15 * 1024 * 1024)))
REFERENCE_DOWNLOAD_TIMEOUT = int(os.getenv("DOLA_REFERENCE_DOWNLOAD_TIMEOUT", "60"))
REFERENCE_IMAGE_MAX_COUNT = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_COUNT", "30"))
# 内联参考图（data: URL）落盘目录：几 MB 的 base64 绝不写进 tasks.db（会把库撑到几百 MB）
REFERENCE_FILE_DIR = os.getenv("DOLA_REFERENCE_FILE_DIR", "refs")

# 参考图缩略图目录与规格（面板回看用）。
# 任务走到终态时原参考图会被清掉（落盘文件删除 / 临时目录 rmtree），所以要另存一份小图，
# 否则面板里"参考图"永远是空的。默认只存第一张，避免大规模号池把磁盘吃满。
THUMB_DIR = os.getenv("DOLA_THUMB_DIR", "thumbs")
REFERENCE_THUMB_COUNT = int(os.getenv("DOLA_REFERENCE_THUMB_COUNT", "1"))
REFERENCE_THUMB_MAX_PX = int(os.getenv("DOLA_REFERENCE_THUMB_MAX_PX", "160"))
REFERENCE_THUMB_QUALITY = int(os.getenv("DOLA_REFERENCE_THUMB_QUALITY", "80"))
# 单次请求参考图总量上限（落盘后的字节数）
REFERENCE_TOTAL_MAX_BYTES = int(
    os.getenv("DOLA_REFERENCE_TOTAL_MAX_BYTES", str(60 * 1024 * 1024))
)
# 单条 prompt 上限（超出直接 422；store 里还有一层截断兜底）
MAX_PROMPT_CHARS = int(os.getenv("DOLA_MAX_PROMPT_CHARS", "20000"))
# 已结束任务的保留天数：超期自动删记录 + 删对应视频文件（0 = 不清理）
TASK_RETENTION_DAYS = float(os.getenv("DOLA_TASK_RETENTION_DAYS", "14"))
# 出片文件目录体积上限（超过就按时间从旧到新删）
DOWNLOAD_MAX_BYTES = int(os.getenv("DOLA_DOWNLOAD_MAX_BYTES", str(20 * 1024 * 1024 * 1024)))
# 排队进度预估：各档位出片平均耗时（秒）兜底值（样本够多时用近 3 天实测均值）
ETA_FALLBACK_SECONDS = {5: 130, 10: 260, 15: 390, 30: 560}
ETA_DEFAULT_SECONDS = int(os.getenv("DOLA_ETA_DEFAULT_SECONDS", "420"))
# 排队等待上限：还没提交到上游（没有 conversation）且等了这么久 → 明确失败，别让客户端悬着
MAX_QUEUE_WAIT_SECONDS = int(os.getenv("DOLA_MAX_QUEUE_WAIT_SECONDS", "1800"))
# 看门狗的「没进展」判定：processing 任务超过这么久没有任何轮询更新 → 重新派发/回收
STALE_TASK_SECONDS = int(os.getenv("DOLA_STALE_TASK_SECONDS", "600"))
# 看门狗巡检间隔（秒）
WATCHDOG_INTERVAL_SECONDS = int(os.getenv("DOLA_WATCHDOG_INTERVAL_SECONDS", "60"))

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

# 【已废弃 2.1.0 P3】上游瞬时限流（710022002）的冷却时长。
# 「限流」概念已按拍板删除：这类拒绝现在只换号重试，不冷却、不标状态。
# 变量保留只为兼容旧 run.sh，不再被任何代码读取。
TRANSIENT_COOLDOWN_SEC = int(os.getenv("DOLA_TRANSIENT_COOLDOWN_SEC", "600"))

# 【异常】组的自动恢复时长（秒）：5 分钟无回执 / 连续失败 3 次 / profile 缺失时进组，
# 到期自动恢复。
ABNORMAL_COOLDOWN_SEC = int(os.getenv("DOLA_ABNORMAL_COOLDOWN_SEC", "1800"))

# 派发后「上游必须给出任何回应」的时限（秒）：超时且一句回执都没有 → 该号进【异常】组，
# 任务报「生视频过程中出现异常情况，请重试」。按拍板取 5 分钟。
NO_ACK_SECONDS = int(os.getenv("DOLA_NO_ACK_SECONDS", "300"))

# ---- 「你好」风控探测（2026-09-23 P4）----
# 派发前对该号发一句「你好」：被登出（x-tt-agw-login != 1）或没有回复 → 判风控。
HELLO_PROBE_PROMPT = os.getenv("DOLA_HELLO_PROBE_PROMPT", "你好")
# 同号探测结果的复用时长（秒）：0 = 每个任务都真探（更严格，但每个任务多花 ~3 秒，
# 且要和出片抢同一条上游提交通道）。
HELLO_PROBE_CACHE_SECONDS = int(os.getenv("DOLA_HELLO_PROBE_CACHE_SECONDS", "300"))
# 单次「你好」等待回复的上限（秒）。
HELLO_PROBE_TIMEOUT = int(os.getenv("DOLA_HELLO_PROBE_TIMEOUT", "60"))
# 探测通过后、真正提交出片前的间隔（秒）：探测和出片走同一条上游提交通道，
# 挨着发容易被上游判「访问频繁」(710022002)，所以留出提交间隔（0 = 不等，最激进）。
HELLO_PROBE_SUBMIT_GAP_SECONDS = int(os.getenv("DOLA_HELLO_PROBE_SUBMIT_GAP_SECONDS", "15"))

# 同一号两次向上游提交的最小间隔（秒）；0 = 不限制。
MIN_SUBMIT_INTERVAL_SECONDS = int(os.getenv("DOLA_MIN_SUBMIT_INTERVAL_SECONDS", "0"))

# seedance-2.0（每条 3 点）只能用【满额】组账号；满额暂时用完时排队等待的上限（秒）。
# 0 = 不排队，立刻失败。
V20_WAIT_SECONDS = int(os.getenv("DOLA_V20_WAIT_SECONDS", "600"))

# 同/近一次探活的最小间隔（秒）。避免空转打代理。
PROBE_INTERVAL_SECONDS = int(os.getenv("DOLA_PROBE_INTERVAL_SECONDS", "120"))

# 同时最多打开多少个号去提交/占用。0 = 不限制。
MAX_OPEN_ACCOUNTS = int(os.getenv("DOLA_MAX_OPEN_ACCOUNTS", "0"))

# 出片/探测都跑在 asyncio.to_thread 里，而 Python 默认线程池只有 min(32, CPU+4) 个线程 ——
# 不限并发时它会变成真正的瓶颈（第 33 个任务只能排队等）。0 = 自动（max(64, CPU×8)）。
THREAD_POOL_MAX = int(os.getenv("DOLA_THREAD_POOL_MAX", "0"))

# ===== 纯 API 出片（cookie 账号，对齐 dola-pool-cookie）=====
# cookie 来源账号（source=cookie 且有 cookie_state.json）出片走纯 API + bdms 签名，
# 不再依赖浏览器 profile。1 = 开启；0 = 全部走浏览器。
PURE_API_ENABLED = os.getenv("DOLA_PURE_API", "1") == "1"
# 纯 API 协议参数（对齐 dola-pool-cookie 的 region /pc_version）。
PURE_API_REGION = os.getenv("DOLA_PURE_REGION", "JP")
PURE_API_PC_VERSION = os.getenv("DOLA_PURE_PC_VERSION", "3.33.11")
# 是否走无水印（第三方 nowatermark 解析）。
PURE_API_REMOVE_WATERMARK = os.getenv("DOLA_PURE_REMOVE_WATERMARK", "1") == "1"
# 纯 API 出片轮询/下载目录（复用 DOWNLOAD_DIR）。
PURE_API_TIMEOUT = int(os.getenv("DOLA_PURE_TIMEOUT", "900"))
PURE_API_POLL_INTERVAL = int(os.getenv("DOLA_PURE_POLL_INTERVAL", "30"))
