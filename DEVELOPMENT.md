# dola-pool 开发记录（交接文档）

> 面向接手开发者（Codex）。读完本文档即可无缝继续开发，无需重新逆向。
> 最后更新：2026-08-22 01:45

---

## 1. 项目目标

把 **dola.com**（字节跳动 Seedance 视频模型的海外品牌，即梦/豆包海外版）的免费视频生成能力，通过**账号池（号池）**包装成一个 **OpenAI 兼容的异步 API 服务**，分发给客户调用。

完整链路：`客户请求（OpenAI 格式）→ 号池挑账号 → 浏览器 UI 提交（过滑块）→ 轮询出片 → 下载转存 → 返回视频 URL`。

关键背景：
- dola.com **无官方公开 API**，视频生成走网页端接口，需逆向。
- 免费额度：**每账号每天 2 个视频**（UI 文案：1 片 = 2 积分），所以必须做号池堆产能。
- 登录方式：**Google OAuth**。
- 地区限制：需 **JP/KR 出口 IP**（大陆直连不可用）。

---

## 2. 技术栈

| 组件 | 用途 | 备注 |
|---|---|---|
| Python 3.13 | 运行时 | venv：`C:\Users\LuJia\.workbuddy\binaries\python\envs\default\Scripts\python.exe` |
| FastAPI + uvicorn | 异步 API 服务 | `server.py`，端口 8000 |
| SQLite | 任务/配额持久化 | `store.py`（tasks.db）、`browser_pool.py`（pool_usage.db） |
| **patchright** | Playwright 反检测分支（**当前主线**） | 抹掉 `navigator.webdriver` |
| Chromium | 浏览器内核 | `C:\Users\LuJia\AppData\Local\ms-playwright\chromium-1234` |
| OpenCV (cv2) | 滑块缺口识别 | `gap.py`（Canny + matchTemplate） |
| aiohttp | 下载视频 / 抓验证码图 | — |
| ~~ddddocr~~ | **不可用** | onnxruntime DLL 初始化失败，已用 gap.py 替代 |

安装依赖：`pip install -r requirements.txt`，另需 `patchright opencv-python`，浏览器 `python -m patchright install chromium`。

---

## 3. 核心技术路线演进（为什么这么做）

1. **HTTP 逆向（aiohttp 裸请求）**——协议字段逆向成功（`dola_client.py`），但提交视频必触发鲨鱼风控滑块（`710022004 slide`）。
2. **页面内裸 fetch**——仍有滑块。根因：前端真实请求带 **`a_bogus` 签名**（webmssdk 生成），裸 fetch 没有，被风控一眼识破。
3. **真实 UI 流（当前主线，已验证出片）**——点「创建视频」→ 输入 → 回车，由前端自己签名提交。风控仍可能弹滑块（headless + 机房 IP），但**滑块可解**（OpenCV 缺口 + 拟人轨迹），解完前端自动重试提交。
4. 轮询出片协议已更新为 2026-08 现行版（见 §4.1），旧扁平 body 返回 `712010202`。

**验收状态（2026-08-22）**：✅ 两条视频完整出片并下载（橘猫 10s / 雪山湖 10s），✅ FastAPI 异步两段式 API 端到端跑通。

---

## 4. 已完成的功能

### 4.1 协议知识（现行版，2026-08）

**提交（必须走 UI，前端签名 a_bogus）**：
- 入口：首页输入框下「创建视频」chip（ja-JP locale 文案为「動画を作成」）。
- 前端 body：`chat_ability={ability_type:17, ability_param:{model:"seedance_v2.0",duration}}`，文本前缀本地化（ja 为「生成された動画：{prompt}」；zh 系为「生成影片：{prompt}，{ratio}」）。
- query 含 `a_bogus`、`device_id`、`web_id`、`tea_uuid`、`pc_version=3.32.61` 等，由 webmssdk 生成，**不可裸调**。

**轮询出片（可页面内 fetch，无需签名）**：`POST /im/chain/single`
- body 必须包 `uplink_body.pull_singe_chain_uplink_body`：
  `{conversation_id, anchor_index: Number.MAX_SAFE_INTEGER, conversation_type: 3, direction: 1, limit: 20, ext:{}, filter:{index_list:[]}}`
  外加 `sequence_id: uuid, channel: 2, version: "1"`。
  （`dola_client.py` 里的扁平 body 是旧协议，返回 712010202，勿用。）
- query 无需 msToken/a_bogus：`version_code=20800&language=ja&device_platform=web&doubao_device_platform=web&aid=495671&real_aid=495671&pkg_type=release_version&pc_version=3.32.61&doubao_pc_version=3.32.61&region=JP&sys_region=JP&samantha_web=1&web_platform=browser&use-olympus-account=1&web_tab_id=<uuid>`。
- 出片判定：`downlink_body.pull_singe_chain_downlink_body.messages[].content`（JSON 字符串需 parse）里 `block_type=2074` 且 `creations[].type==2` → `video.download_url`（CDN 临时链接，带 `download=true`）。
- `conversation_id` 获取：UI 提交后 URL 从 `/chat/local_<ts>` 变成 `/chat/<数字id>`。

**常量**：`DOLA_AID="495671"`、`DOLA_BOT_ID="7339470689562525703"`、`VERSION_CODE="20800"`。

### 4.2 滑块求解（已验证，通过率约 1-2 次尝试）

字节 verifycenter（海外版 rmc-captcha）：
- 触发后前端加载 iframe，URL 含 `bdcaptcha.html`；后端 `verify-s.byteintlapi.com/captcha/get?subtype=slide`。
- iframe 内：背景图 `~tplv-w5pjy1c2y6-2.jpeg`（552×344）、滑块块 `~tplv-w5pjy1c2y6-1.png`（110×110 透明）。
- 拖动按钮 class：**`.captcha-slider-btn`**（注意 `[class*="slider"]` 会误中轨道 `captcha-slider-box`）。
- 缺口识别：`gap.py` OpenCV（alpha 轮廓 Canny → 背景边缘 matchTemplate），实测 conf 0.23-0.39 但位置正确；**等图加载完（naturalWidth>0）再算 scale**，显示宽 340 / 自然宽 552。
- 拟人轨迹：smootherstep 主行程 + 3-9px 过冲回稳 + y 轴抖动，总时长 ~1s，可过。
- 通过后 iframe 消失，前端**自动重试提交**；失败则同图停留（说明拖动没生效，检查选择器）。
- 滑块**不是每次必弹**（同一 profile 过一次后后续可能直接放行）。

### 4.3 OpenAI 兼容 API（已端到端验证）

- `POST /v1/videos/generations` → `{id, status:"queued"}`；`GET /v1/videos/{id}` → `{status, video_url}`。
- 出片后视频下载到 `downloads/`，经 `GET /videos/<file>` 静态服务返回永久链接（替代 CDN 临时链接）。
- `GET /health` → 账号配额状态。
- 号池 `browser_pool.py`：扫 `accounts/*/`，每号每天 2 片（SQLite 计数，保守记：成功/额度报错才记），同号 asyncio.Lock 互斥，额度不足自动换号。

### 4.4 登录态采集（沿用）

- `login.py <账号名>`：headful 手动 Google 登录，检测 sessionid 存 profile 到 `accounts/<名>/`。
- `verify_login.py`：无头验证登录态。
- 已有 profile：`accounts/acc1/`。

---

### 4.5 自动添加账号（Google OAuth 全自动，已验证）

`add_account.py <账号名> "email----password----totp_secret"`：
dola 登录弹窗「Googleで続ける」→ Google v3 登录状态机 → OAuth 回跳 → dola 年龄确认 → sessionid 写入 profile。
状态机逐页处理：选择账号 / 邮箱 / 密码 / 授权同意 / 年龄确认，直到离开 accounts.google.com。

坑（都已解决，改代码时注意）：
- Google v3 邮箱框是 `#identifierId` 且 **type=text**（不是 type=email）；密码框 `input[name="Passwd"]`。
- 同意页也显示邮箱文本，**选择账号页必须用 URL 含 `accountchooser` 判定**，否则误点同意页账号条。
- 同意页「继续」按钮要点 `button:has-text('继续')` / `[role='button']:has-text('继续')`（含 ja 続行 / en Continue）。
- dola 年龄确认弹窗（「18歳以上」）的 OK 普通选择器点不动，用 **JS dispatch click** 叶子节点。
- profile 里 Google cookie 会残留：二次登录走 选择账号→同意（免密码）。
- TOTP 用标准库实现（`totp()`），无第三方依赖。

登录后 `verify_login.py <账号名>` 验证；号池（`browser_pool.py`）按目录自动发现新账号，无需重启服务。

---

## 5. 当前卡点

**无阻塞卡点。** 主链路已通。剩余为规模化/健壮性问题（见 §8）。

---

## 6. 代码结构

```
F:\ai\视频逆向\dola-pool\
├── server.py           # FastAPI 服务（OpenAI 兼容 + /videos 静态服务）★
├── browser_pool.py     # 浏览器号池（配额/互斥/换号）★
├── video_worker_ui.py  # [主线] UI 流提交 + 滑块求解 + 轮询 + 下载 ★
├── video_worker.py     # 裸 fetch 版（提交已废，但 POLL_JS/_download/RiskControlError 被复用）
├── gap.py              # OpenCV 滑块缺口识别 ★
├── browser.py          # persistent context 统一启动（显式代理+反检测）★
├── store.py            # SQLite 任务表
├── config.py           # 环境变量配置（含 DOLA_PROXY/DOLA_PUBLIC_BASE）★改
├── dola_client.py      # [历史] HTTP 逆向 SDK；轮询 body 已过期，仅留协议字段参考
├── pool.py             # [历史] aiohttp 号池（被 browser_pool 取代）
├── add_account.py        # [新] Google OAuth 自动登录加号（邮箱----密码----totp）★
├── login.py / verify_login.py   # 登录态采集/验证 ★改（显式代理）
├── captcha_probe.py / ui_explore.py / check_conv.py / diag_*.py  # 调试探针
├── bdcaptcha.html/js   # 字节验证码前端（离线存档，查 class 名用）
├── accounts/acc1/      # 账号 profile
├── downloads/          # 出片转存目录
└── tasks.db / pool_usage.db
```

（★ = 本轮新增/修改）

---

## 7. 已知技术债务

1. **locale 耦合**：UI 入口文案硬编码 ja-JP「動画を作成」；换 locale 要改 `VIDEO_BTN`。可改图标/属性选择器更稳。
2. **headless 指纹**：sec-ch-ua 暴露 `HeadlessChrome`，风控大概率因此弹滑块（可解，但增加耗时）。生产可考虑 headful + xvfb 或进一步指纹修补。
3. **时长/比例选择器 best-effort**：UI 下拉选项文案未完全摸清，设置失败会落回默认 10s。
4. **转存仍是本地静态**：`/videos/` 只在服务所在机可达；对外分发需 OSS 或公网机。
5. **配额计数与 dola 实际积分可能漂移**（1 片=2 积分；UI 有剩余积分文案）。失败重试不记配额是有意保守。
6. **dola_client.py 轮询 body 过期**（扁平结构），如复活 HTTP 版需按 §4.1 更新。
7. **滑块兜底已实现但非 100%**：轨迹被拒时靠重试（最多 3 次）；连续失败需换 IP/冷却。

---

## 8. 下一步任务（按优先级）

| 优先级 | 任务 | 说明 |
|---|---|---|
| P1 | 号池规模化 | 多账号 profile（批量 `login.py`）+ 配额面板；产能 = 2 片/号/天 |
| P1 | 转存对外 | 接 OSS（或公网机 + 域名），替换本地 `/videos/` |
| P2 | 入口选择器去 locale 化 | 用图标/aria 属性定位「创建视频」，比例/时长下拉摸清 |
| P2 | 登录态续期监控 | session ~60 天过期；定期 `verify_login.py` 巡检 + 告警 |
| P2 | 滑块失败冷却 | 连续 3 次不过 → 账号冷却 N 分钟 / 提示换 IP |
| P3 | 运营化 | 养号节奏、积分余额抓取（UI 文案）、请求级日志/指标 |

---

## 9. 约束条件与潜在风险

1. **地区墙（硬约束）**：dola 必须 JP/KR 出口；代理显式传给浏览器（`config.PROXY`，默认 `http://127.0.0.1:7890`），不依赖系统代理。
2. **滑块风控（核心风险）**：UI 流 + 可解滑块是当前最优解；字节随时可能升级验证码（改轨迹检测/换形态），`bdcaptcha.js` 存档可用来跟 class 名。
3. **产能瓶颈**：2 片/号/天，号池规模 = 产能上限。
4. **登录态时效**：~60 天，需续期。
5. **前端变更风险**：入口文案/下拉/轮询协议都可能变；`diag_raw2.py` 可快速抓现行请求格式。

---

## 10. 给 Codex 的衔接提示

- **提交必须走 UI**（前端签 a_bogus）；轮询可页面内 fetch，body 用 `uplink_body` 包裹版（`video_worker.py` 的 `POLL_JS` 是现行版）。
- 滑块三件套：iframe 找 `bdcaptcha.html` → 按钮 `.captcha-slider-btn` → 缺口 `gap.py`。图要等 `naturalWidth>0`。
- 调试网络/协议变更：`diag_raw2.py`（抓 UI 真实请求）、`ui_explore.py`（UI 流程 dump）。
- 出片慢属正常：Seedance 10s 视频生成 3-5 分钟，`VIDEO_TIMEOUT` 默认 300s 够用。
- 跑脚本前 `set PYTHONIOENCODING=utf-8` 看中文日志。- 跑脚本前 `set PYTHONIOENCODING=utf-8` 看中文日志。
- 加新号：`add_account.py <名> "email----pass----totp"` 或面板「账号管理 → 添加账号」；加号失败会在 `/api/admin/jobs` 留 `failed` + 错误。
- 面板出问题先看浏览器控制台；前后端都由 `server.py` 启动，UI 是纯静态单文件 `web/index.html`，改完刷新即可、无需重启（改 Python 后端才需重启）。

---

## 11. 管理面板（v0.3.0）

- 访问 `http://127.0.0.1:8000/`，四大 tab：仪表盘 / 账号管理 / 任务列表 / API 密钥。
- 管理员鉴权：`config.ADMIN_KEY`（env `DOLA_ADMIN_KEY`）；留空 = 面板免登录（开发模式）。客户端每次请求带 `X-Admin-Key` 头，前端存 localStorage。
- 调度开关与风控冷却直接参与号池挑选：`generate_video` 跳过 `scheduling=0` 或 `cooldown_until>now`；捕到 `RiskControlError` 自动设 30 分钟冷却（`COOLDOWN_SEC`）。
- 账号元数据表 `accounts_meta`（pool_usage.db）：scheduling/note/created_at/last_used_at/login_ok/login_checked_at/cooldown_until；号池扫目录时 `INSERT OR IGNORE` 自动建档。

### 11.1 admin 接口族（均走 `X-Admin-Key`）
- `POST /api/admin/login`｜`GET /api/admin/accounts`｜`PATCH/DELETE /api/admin/accounts/{name}`｜`POST /api/admin/accounts/{name}/verify`（无头验证登录态，~7s）｜`POST /api/admin/accounts`（后台加号任务，202）｜`GET /api/admin/jobs`（加号任务状态）｜`GET /api/admin/tasks?limit=`｜`GET /api/admin/stats`｜`GET/POST/PATCH/DELETE /api/admin/keys`。

### 11.2 API 密钥语义（客户调用 /v1 用）
- 免鉴权仅当「`config.API_KEYS` 为空 且 DB 无启用 key」；否则必须 `Authorization: Bearer <key>`。
- env key（`DOLA_API_KEYS`）永远有效、面板只读；面板建的 key 存在 `tasks.db` 的 `api_keys` 表（`sk-` + 32 hex，启用开关、last_used_at 节流 60s 更新）。
- 停用/删除即时对调用方生效；删光所有 key 后回到免鉴权。
## 12. Seedance 2.5 / API key 测试记录（2026-08-22）

- 测试 key 名：`codex-seedance-2.5-30s-test`，已保留在面板「API 密钥」中。
- `model: "seedance-2.5"` 已接入 worker，映射到网页端真实字段 `seedance_v2.5`；不要再固定写死 `seedance_v2.0`。
- Dola 当前网页端 Seedance 2.5 只展示 `5s`、`10s` 两个时长；API 对 `duration:30` 返回 HTTP 422「当前网页端仅提供 5 秒和 10 秒，不支持 30 秒」，不会偷偷降级。
- 同 key 的 2.5/10s 请求已真实提交、滑块通过并拿到 conversation_id；Dola 返回本次消耗 4 积分，但 acc2 当时剩余 0 积分，300 秒内没有 `video.download_url`，任务失败。该次已计入 acc2 当日使用次数。
- 后续若 Dola UI 出现 30s，需要同时更新 worker 的时长选项与 server 的 duration 白名单；在此之前不要伪造 30s。
## 13. 账号每日上限自动切号（2026-08-22）

- `video_worker_ui.py` 识别 Dola 日文文案 `動画生成の1日あたりの上限に達しました。明日またお試しください。`，抛出 `AccountLimitedError`。
- `browser_pool.py` 捕获后立即把该号本地 usage 封顶到 `DAILY_LIMIT=2`，跳过当前号继续下一个，不等待 300s 超时。
- 所有开启调度且未处于冷却的账号都封顶时，创建接口直接返回 HTTP 429；已入队任务若在执行中才发现全部封顶，任务状态为 failed，error 以 `429:` 开头。
- 2026-08-22 实测：`acc1` 返回每日上限后被自动标记 `2/2`；`acc2` 已是 `2/2`；任务 `video_7c468876bf224f76971fef69067ed49e` 最终记录 429；后续新请求直接 HTTP 429。
## 14. 限流账号恢复调度（2026-08-22）

- `accounts_meta` 新增 `rate_limited_until` / `limit_reason`。
- Dola 返回每日上限后，账号立即标记为「限流」，`_schedulable()` 在恢复时间前始终跳过该账号；不是只依赖 `usage.used=2`。
- 默认恢复点为日本时间次日 00:00（`DOLA_LIMIT_RESET_TZ` 可配置；运行环境缺 tzdata 时对 Asia/Tokyo 使用 UTC+9 fallback）。本次 acc1/acc2 已标记恢复时间：2026-08-23 00:00（日本时间）。
- 到期后 `list_accounts()` 自动清除限流标记；当天 usage 查询按新日期自然归零，账号重新进入调度。
- 面板账号状态显示「限流至 YYYY-MM-DD HH:MM」；全部账号限流时新请求直接 HTTP 429。
## 15. 长视频超时策略（2026-08-22）

- Dola 2.5/30s 页面会提示预计约 15 分钟；worker 对 `duration=30` 自动使用至少 1200s 轮询超时。
- 普通轮询超时不再自动换号重提：拿到 conversation_id 后，Dola 端可能仍在生成，换号会造成重复扣额度/重复视频。
- 只有 Dola 明确返回每日上限（AccountLimitedError）或风控（RiskControlError）才自动切号。
- 2026-08-22 的第一次 2.5/30s 测试曾因旧 300s 策略从 acc3 重提到 acc4；最终文件为 30.04s，后续已修复并重启服务。
## 16. 积分预检与 conversation 恢复（2026-08-22）

- `accounts_meta` 新增 `credit_balance` / `credit_checked_at`；轮询响应里的“剩余 N points/ポイント/积分”会缓存到账号。
- 已知余额低于 `DOLA_VIDEO_REQUIRED_POINTS`（默认 2）时，生成前直接跳过账号并标记积分不足；所有账号都已知不足时返回 HTTP 429。新账号余额未知时不误拦截，仍允许第一次提交并等待 Dola 返回余额。
- `tasks` 新增 `conversation_id`、`deadline_at`、`last_poll_at`、`failure_code`。拿到 Dola conversation_id 后立即持久化；轮询每 30 秒更新 last_poll_at。
- FastAPI 启动时恢复 `queued/processing + conversation_id/account` 任务，调用原会话轮询，不重新发送 prompt；已过 deadline 的任务只会结束，不会重提。
- 30 秒任务轮询上限固定至少 1800 秒（30 分钟）；普通超时不自动换号，避免重复扣费。
- Dola 当前没有稳定的独立余额接口，预检优先使用缓存余额；余额未知时采用不误阻断策略。
## 17. 本地 Cloudflare Quick Tunnel（2026-08-22）

- 已安装 `cloudflared` 2026.8.2，当前临时公网地址：`https://<quick-tunnel>.trycloudflare.com`。
- 本地 `.env.local`（已加入 `.gitignore`）保存 `DOLA_PUBLIC_BASE`、`DOLA_ADMIN_KEY`、扩展开关；`config.py` 启动时自动读取，真实环境变量优先。
- Tunnel 命令：`cloudflared tunnel --url http://127.0.0.1:8000`。Quick Tunnel 重启后域名会变化，需要更新 `.env.local` 并重启 FastAPI。
- 已验证：公网 `/` 返回 200；无 `X-Admin-Key` 访问 `/api/admin/stats` 返回 401；带管理员密码返回 200；公网 `/videos/*.mp4` 支持 206。
- NewAPI 的兼容地址使用 `https://<quick-tunnel>.trycloudflare.com/v1`；当前不需要 R2，新视频先由 `/videos/` 通过 Tunnel 提供。
- Quick Tunnel 仅用于本地测试；正式长期服务应改 Named Tunnel/固定域名，并接 OSS/R2。
## 18. 参考图片输入（2026-08-22）

- API 新增 `reference_images: string[]`，当前先支持图片，最多 30 张；服务端下载公网 URL 到临时目录，校验 JPEG/PNG/WEBP、大小和 SSRF，再通过 Dola 原生 `input[type=file]` 上传。
- Dola 图片协议已实测：`/alice/resource/prepare_upload` → TOS 上传；生成请求使用 `block_type=10052`，`attachment_block.attachments[].image.uri`。
- 临时素材任务目录在系统 temp 下，任务结束后自动删除；图片下载先尝试 DOLA_PROXY，失败自动直连回退。
- 内网/回环 URL 返回 422；公网测试 URL `https://httpbin.org/image/jpeg` 已成功进入 Dola、拿到 conversation_id 并完成视频。
- 图片参考测试任务：`video_e74f2b89f87745c2adc730b8dfd5fd8e`，5 秒 Seedance 2.5，结果：`https://<quick-tunnel>.trycloudflare.com/videos/acc5_20260822_220543.mp4`。
- 目前尚未接入 `reference_videos` / `reference_audios`；其数量限制和上传协议后续再做。

### API 示例
```json
{
  "model": "seedance-2.5",
  "prompt": "让参考图片中的主体轻轻转头，保持外观一致",
  "size": "1280x720",
  "duration": 5,
  "reference_images": ["https://example.com/image.jpg"]
}
```
## 19. 无水印视频下载（2026-08-22）

- Dola `video.download_url` 是带水印地址（通常 `lr=cici_ai`），不能再直接作为最终下载源。
- 出片响应的 `video.video_model` JSON 内含 `video_list.*.main_url`（base64 编码）。解码后是 `lr=unwatermarked` 的无水印视频链接。
- `video_worker.POLL_JS` 现在返回 `videoModels`；`extract_unwatermarked_url()` 按最高 bitrate 选择 main_url，解析失败才回退普通 `download_url`。
- `video_worker_ui.poll_conversation()` 已接入无水印优先下载，所有新任务会返回无水印版本。
- 实测图片参考任务 `video_e74f2b89f87745c2adc730b8dfd5fd8e`：带水印原文件与无水印 main_url 对比确认，当前返回无水印文件：`https://<quick-tunnel>.trycloudflare.com/videos/acc5_20260822_234820.mp4`。
## 20. 参考素材范围确认（2026-08-22）

- 当前对外只支持 `reference_images`，最多 30 张公网图片。
- `reference_videos` / `reference_audios` 暂不支持，也不伪造 Dola attachment；Dola 当前视频面板和通用附件入口实际只接受图片/文档，MP4/WAV 注入未触发上传请求。
- 客户侧如果传视频或音频素材，当前版本不应指望被处理；后续只有在 Dola 网页端暴露稳定上传入口并完成协议探针后再扩展。