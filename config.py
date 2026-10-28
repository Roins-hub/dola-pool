"""dola-pool 配置：全部走环境变量，带默认值。"""
import os
from pathlib import Path


def _load_local_env():
    """Load ignored .env.local for local/tunnel runs; real environment wins.

    DOLA_ENV_FILE（start-local.ps1 传 dola.env 的路径）同样按此读入：Windows 上进程环境变量不能是空串，
    SetEnvironmentVariable 传空值等于删除，dola.env 里 `DOLA_PROXY=` 这种显式留空会退回默认值，只能在这里补回空串。
    """
    paths = []
    if os.getenv("DOLA_ENV_FILE"):
        paths.append(Path(os.environ["DOLA_ENV_FILE"]))
    # First value wins: process environment > explicit file > local defaults.
    paths.append(Path(__file__).with_name(".env.local"))
    for path in paths:
        if not path.exists():
            continue
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

# Cookie 账号运行时后端：pure = 现有纯 HTTP；cloak = Dola Cookie 账号固定使用
# CloakBrowser 持久 profile。严格模式下浏览器不可用时不会静默回退纯 API。
COOKIE_RUNTIME = os.getenv("DOLA_COOKIE_RUNTIME", "pure").strip().lower()
if COOKIE_RUNTIME not in {"pure", "cloak"}:
    COOKIE_RUNTIME = "pure"
COOKIE_PROFILE_SUBDIR = os.getenv("DOLA_COOKIE_PROFILE_SUBDIR", "cloak-profile").strip() or "cloak-profile"
CLOAK_HEADLESS = os.getenv("DOLA_CLOAK_HEADLESS", "0") == "1"
CLOAK_HUMANIZE = os.getenv("DOLA_CLOAK_HUMANIZE", "0") == "1"
CLOAK_GEOIP = os.getenv("DOLA_CLOAK_GEOIP", "0") == "1"
CLOAK_LICENSE_KEY = os.getenv("CLOAKBROWSER_LICENSE_KEY", "")
CLOAK_CACHE_DIR = os.getenv("CLOAKBROWSER_CACHE_DIR", "")
if CLOAK_CACHE_DIR:
    os.environ.setdefault("CLOAKBROWSER_CACHE_DIR", CLOAK_CACHE_DIR)

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

# 30 秒（及以上）请求怎么处理（2.1.8）。dola 自 2026-10-12 起单条视频最长 15 秒：30 秒请求会被
# 回「支持 4 到 15 秒 / 按默认 5 秒为你生成 / 要不要拆两段」，官网上选「拆两段」也直接报错。
#   reject：受理时直接拒绝（422，说明原因），不派号、不占额度（默认）
#   pair  ：明确请求两条 15 秒（前半段 / 后半段，各一条独立会话），本地 ffmpeg 拼成 30 秒；
#           消耗两条的额度（2.5 = 4 点），只派给满额的纯 API 号
#   native：旧行为，直接按 30 秒提交（留着等 dola 哪天恢复）
# 兼容旧变量：没设 DOLA_30S_MODE 但设了 DOLA_ALLOW_30S_PAIR=1 时按 pair。
LONG_VIDEO_MODE = os.getenv("DOLA_30S_MODE", "").strip().lower()
if LONG_VIDEO_MODE not in ("reject", "pair", "native"):
    LONG_VIDEO_MODE = "pair" if ALLOW_30S_PAIR else "reject"
# pair 模式每段**向上游请求**的时长。请求 30 秒时 dola 回「按最接近的支持时长 15 秒为你生成，将消耗 2 个」
# —— 拿到 15 秒只扣 2 点；而直接请求 15 秒要 12 点（2026-10-14 实测 + 网页截图）。所以默认请求 30。
PAIR_SEGMENT_SECONDS = int(os.getenv("DOLA_PAIR_SEGMENT_SECONDS", "30"))
# 每段至少要这么长才算数：上游有时回「按默认 5 秒」，5 秒段拼不出 30 秒，整单直接失败（不换号白烧额度）。
PAIR_MIN_SEGMENT_SECONDS = float(os.getenv("DOLA_PAIR_MIN_SEGMENT_SECONDS", "13"))

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

# ---- 按「模型 + 时长」的扣点（2.1.9）----
# 2026-10-14 两台线上日志里上游回执的报价（「将消耗 N 个视频生成额度」）按任务时长对齐：
#   seedance-2.5：5 秒 = 2（多次）、10 秒 = 4（5 次，配合上面注释「实测扣满一天 4 点」）、
#                 15 秒 = 12（实测：普通 15 秒请求被回「需要消耗 12 个…今日剩余 4 个，无法生成」）
#   seedance-2.0：2026-10-27 按用户确认的记账规则，5/10/15 秒统一 3 点。
# 注意 2.5 不是按秒线性涨价（15 秒一档跳到 12）：免费号每日 4 点，2.5 最长只能出 10 秒。
# 上游余额不够报价时直接拒绝（「需要消耗 N 个，今日剩余 M 个，无法生成」），所以派号必须按报价算。
# 表里没有的时长：取表里比它长的最短一档；比表里都长就按最长一档的每秒单价外推（向上取整）；
# 整个模型都没有表 → 退回 MODEL_COSTS。可用 DOLA_DURATION_COSTS 覆盖，格式：
#   seedance-2.5:5=2,10=4,15=12;seedance-2.0:10=2
DURATION_COSTS: dict[str, dict[int, int]] = {
    # 30 = 请求 30 秒：上游压成 15 秒出片、按 2 点扣（线上 4 次报价 + 网页截图），pair 模式每段就这么请求
    "seedance-2.5": {5: 2, 10: 4, 15: 12, 30: 2},
    "seedance-2.0": {5: 3, 10: 3, 15: 3},
}


def _parse_duration_costs(raw: str) -> dict[str, dict[int, int]]:
    table: dict[str, dict[int, int]] = {}
    for part in (raw or "").split(";"):
        if ":" not in part:
            continue
        model, _, pairs = part.partition(":")
        for pair in pairs.split(","):
            seconds, _, cost = pair.partition("=")
            try:
                table.setdefault(model.strip(), {})[int(seconds)] = int(cost)
            except ValueError:
                continue
    return table


for _model, _costs in _parse_duration_costs(os.getenv("DOLA_DURATION_COSTS", "")).items():
    DURATION_COSTS.setdefault(_model, {}).update(_costs)


def credit_cost(version: str, duration: int | None) -> int:
    """某个 seedance 版本出一条 duration 秒视频，上游要扣（报价）多少点。"""
    if version == "seedance-2.0-fast":
        version = "seedance-2.0"
    base = MODEL_COSTS.get(version, DEFAULT_CREDIT_COST)
    table = DURATION_COSTS.get(version) or {}
    try:
        seconds = int(duration or 0)
    except (TypeError, ValueError):
        seconds = 0
    if not table or seconds <= 0:
        return base
    if seconds in table:
        return table[seconds]
    longer = [d for d in sorted(table) if d >= seconds]
    if longer:
        return table[longer[0]]
    top = max(table)
    return -(-table[top] * seconds // top)        # 按最长一档的每秒单价外推，向上取整

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
# Dola web chat uploadMultiEntityConf.maxImageSize = 0x1400000 (2026-10-28).
# This is the incoming original-image limit; the website may compress it later.
REFERENCE_IMAGE_MAX_BYTES = int(os.getenv("DOLA_REFERENCE_IMAGE_MAX_BYTES", str(20 * 1024 * 1024)))
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
ISOLATE_SHARED_EGRESS = os.getenv(
    "DOLA_ISOLATE_SHARED_EGRESS", "1" if COOKIE_RUNTIME == "cloak" else "0"
) == "1"

# 连续失败分达到该值则冷却该号；0 = 不按失败分冷却。
FAIL_SCORE_CAP = int(os.getenv("DOLA_FAIL_SCORE_CAP", "0"))

# 【已废弃 2.1.0 P3】上游瞬时限流（710022002）的冷却时长。
# 「限流」概念已按拍板删除：这类拒绝现在只换号重试，不冷却、不标状态。
# 变量保留只为兼容旧 run.sh，不再被任何代码读取。
TRANSIENT_COOLDOWN_SEC = int(os.getenv("DOLA_TRANSIENT_COOLDOWN_SEC", "600"))

# ---- 出口限流退避（2.1.5）----
# 上游 710022002「访问频繁」本质是按**出口 IP** 限流：同一出口上的号换来换去只会继续撞墙，
# 越撞限得越久。所以改成按出口退避：某出口一被拒，就在这段时间内不再从它派任务/做探测，
# 任务原地排队等出口恢复。连续被拒按 2 倍递增（120 → 240 → 480 → 900 封顶），
# 成功一次即清零。0 = 关闭退避（恢复旧行为：立刻换号重试）。
EGRESS_THROTTLE_SECONDS = int(os.getenv("DOLA_EGRESS_THROTTLE_SECONDS", "120"))
# 同一出口在 EGRESS_THROTTLE_WINDOW_SECONDS 内有至少这么多个**不同的号**被限流，才判定是 IP 被限、
# 整个出口一起退避。只有 1 个号被拒时只暂停那个号（号级暂停，见 THROTTLE_ACCOUNT_*）——
# 2026-10-14 线上：acc4 被拒后整个 socks5 出口停了 2 分钟，同出口的 acc5 一放行就探测通过。
EGRESS_THROTTLE_MIN_ACCOUNTS = int(os.getenv(
    "DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS", "1" if COOKIE_RUNTIME == "cloak" else "2"
))
EGRESS_THROTTLE_WINDOW_SECONDS = int(os.getenv("DOLA_EGRESS_THROTTLE_WINDOW_SECONDS", "600"))
EGRESS_THROTTLE_MAX_SECONDS = int(os.getenv("DOLA_EGRESS_THROTTLE_MAX_SECONDS", "900"))
# 距上次被拒超过该秒数，递增计数清零，重新从 EGRESS_THROTTLE_SECONDS 起算。
EGRESS_THROTTLE_RESET_SECONDS = int(os.getenv("DOLA_EGRESS_THROTTLE_RESET_SECONDS", "1800"))
# 出口被限流时，动态代理/提取节点的号顺手换一个出口 IP（静态代理换不了，只能等）。
EGRESS_THROTTLE_ROTATE = os.getenv(
    "DOLA_EGRESS_THROTTLE_ROTATE", "0" if COOKIE_RUNTIME == "cloak" else "1"
) == "1"
# 没有任务总时限（deadline）的调用方，等出口恢复最多等多久（秒）。
EGRESS_THROTTLE_MAX_WAIT_SECONDS = int(os.getenv("DOLA_EGRESS_THROTTLE_MAX_WAIT_SECONDS", "600"))
# 面板提示：同一出口 IP 上的号超过这个数就标黄提醒（0 = 不提示）。
EGRESS_SHARE_WARN = int(os.getenv("DOLA_EGRESS_SHARE_WARN", "3"))

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
MIN_SUBMIT_INTERVAL_SECONDS = int(os.getenv(
    "DOLA_MIN_SUBMIT_INTERVAL_SECONDS", "15" if COOKIE_RUNTIME == "cloak" else "0"
))

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
# 是否调用第三方 nowatermark 解析接口（会把视频播放信息经 HTTP 发给第三方）。
# 默认 0（关闭）：2026-10 实测该接口长期连不上，每条视频白等约 2 分钟超时；
# 而 dola 自己返回的 lr=unwatermarked 直链一直可用，关闭后成片照样无水印。
PURE_API_REMOVE_WATERMARK = os.getenv("DOLA_PURE_REMOVE_WATERMARK", "0") == "1"
# 纯 API 出片轮询/下载目录（复用 DOWNLOAD_DIR）。
PURE_API_TIMEOUT = int(os.getenv("DOLA_PURE_TIMEOUT", "900"))
# 上游回执里写了「预计等待 N 分钟」时，出片等待跟着上游的预计放长（N×1.2 + 5 分钟），
# 但不超过这个上限（秒）。2026-10-13 线上：5 秒任务上游报「预计等待 45 分钟」，
# 按 15 分钟固定上限等就会白白判失败（上游其实还在排队生成）。
PURE_API_TIMEOUT_MAX = int(os.getenv("DOLA_PURE_TIMEOUT_MAX", "3600"))
# 30 秒（pair）**dola 没报预计等待时**每次出片最多等多久（秒）。报了就按下面的动态规则。
LONG_VIDEO_TIMEOUT_MAX = int(os.getenv("DOLA_30S_TIMEOUT_MAX", "7200"))
# 出片等待按 dola 的「预计等待 X」动态定：最多等 X × FACTOR + MARGIN，不短于默认超时、不超过 HARD_MAX；
# 等到点时再读一次 dola 最新的预计，变长了就顺延。2026-10-14 线上 dola 先后报过 45 分钟 / 60 分钟 / 2 小时，
# 固定上限总会卡在点上（等满 60 分钟判超时，片子随后出来）。
UPSTREAM_WAIT_FACTOR = float(os.getenv("DOLA_WAIT_FACTOR", "1.5"))
UPSTREAM_WAIT_MARGIN = int(os.getenv("DOLA_WAIT_MARGIN_SECONDS", "1800"))
WAIT_HARD_MAX = int(os.getenv("DOLA_WAIT_HARD_MAX", "21600"))
# 服务重启后「接着等原会话」时多久查一次 dola（秒）
RESUME_POLL_SECONDS = int(os.getenv("DOLA_RESUME_POLL_SECONDS", "60"))
# 超时任务自动取回：已拿到 dola 会话、只是等到上限没出片的任务，判失败后在后台继续盯着
# 那个会话（只查询，不重新提交、不额外扣额度），出片了就下载并把任务改成「完成」。
# 最多盯多久（秒）：0 = 关闭自动取回。
RECOVER_TIMEOUT_SECONDS = int(os.getenv("DOLA_RECOVER_TIMEOUT_SECONDS", "10800"))
# 每隔多久查一次（秒）。
RECOVER_INTERVAL_SECONDS = int(os.getenv("DOLA_RECOVER_INTERVAL_SECONDS", "120"))
# 账号吃到上游 710022002「访问频繁」后，该号进【异常】组暂停派发多久（秒；0 = 不暂停该号，
# 只做出口级退避）。2026-10-13 线上：直连和日本代理两个出口都被拒，按出口退避后换同出口的
# 下一个号继续撞，任务在「退避 → 换号 → 再被拒」里一直循环到超时。
THROTTLE_ACCOUNT_COOLDOWN_SECONDS = int(os.getenv("DOLA_THROTTLE_ACCOUNT_COOLDOWN_SECONDS", "1800"))
# 同一个号**连续**被限流时暂停时长翻倍（30 分 → 1 时 → 2 时 → 4 时 → 8 时），封顶这个值（秒）。
# 线上 acc16~18 导入后一次没成功、5 天里每个号被限流 48 次：固定 30 分钟等于每半小时白撞一次。
# 探测通过或出片成功即清零，下次重新从 THROTTLE_ACCOUNT_COOLDOWN_SECONDS 算起。
THROTTLE_ACCOUNT_COOLDOWN_MAX_SECONDS = int(os.getenv("DOLA_THROTTLE_ACCOUNT_COOLDOWN_MAX_SECONDS", "28800"))
# 距上次被限流超过这么久（秒），连续计数也清零（号很久没被用、也没被拒过，别背着旧账）。
THROTTLE_ACCOUNT_STREAK_RESET_SECONDS = int(os.getenv("DOLA_THROTTLE_ACCOUNT_STREAK_RESET_SECONDS", "86400"))
# 30 秒任务（不拼接时）上游回「只支持 4~15 秒」后，最多再等多久出片（秒）。上游有时说完还会出 15 秒
# 或自己出两段（可拼接），所以不立刻失败；但等这么久仍没片就判「时长不支持」，不再白等到 30 分钟。
# 上游明确说「按默认 5 秒为你生成」时不等，直接失败。0 = 不设上限（等到总超时）。
CAP_30S_GRACE_SECONDS = int(os.getenv("DOLA_30S_CAP_GRACE_SECONDS", "600"))
PURE_API_POLL_INTERVAL = int(os.getenv("DOLA_PURE_POLL_INTERVAL", "30"))

# ---- 生图（/v1/images/generations、/v1/images/edits）----
# 同步接口：一次请求最多等多久（秒），期间失败会换号重试
IMAGE_REQUEST_TIMEOUT = max(30, int(os.getenv("DOLA_IMAGE_TIMEOUT", "240")))

# ---- 豆包（doubao.com）号 ----
# 免费额度用完时，豆包问「本次将消耗付费额度，是否继续？」：1 = 自动确认走付费额度；
# 0 = 不走付费，判这个号今天额度用完、换号。
DOUBAO_ALLOW_PAID = os.getenv("DOUBAO_ALLOW_PAID", "1") == "1"
# 豆包号每天最多派几条（本地记账口径，每条记 1）。豆包不报剩余条数，真实额度以上游话术为准
# （额度用完 / 免费用完且不走付费 → 当天不再派），这个数只是兜底上限。
DOUBAO_DAILY_LIMIT = max(1, int(os.getenv("DOUBAO_DAILY_LIMIT", "999")))
# 豆包「AI 生成水印管理 → 图片、视频」是**按号**的设置，默认有「豆包AI生成」明水印。1 = 探测 / 出片前
# 发现没关就用豆包官方设置接口替这个号关掉（和在网页里点「无水印」一样）；0 = 不动，只在面板上提示。
# 注意：关掉明水印后，视频公开发布时要由发布方自行标注「AI 生成」（豆包设置页里的法规说明）。
DOUBAO_AUTO_NO_WATERMARK = os.getenv("DOUBAO_AUTO_NO_WATERMARK", "1") == "1"
# 豆包出片轮询间隔（秒）：确认 / 付费追问要及时回，所以比 dola 的 30 秒短
DOUBAO_POLL_INTERVAL = max(3, int(os.getenv("DOUBAO_POLL_INTERVAL", "10")))
