# dola-pool

把一组 [dola.com](https://www.dola.com)（字节 Seedance 视频模型国际版）账号组成号池，对外提供 **OpenAI 兼容的视频生成 API**，并兼容火山方舟（Ark）任务式协议，可直接接入 [new-api](https://github.com/QuantumNous/new-api) 等网关。

- 多账号号池：自动选号、额度记账、风控冷却、失败换号、每日额度重置
- 两种出片路径：纯 HTTP 协议（默认）与 Chromium 浏览器自动化（兜底）
- 对外接口：OpenAI 风格 `/v1/videos`、Ark 风格 `/api/v3/contents/generations/tasks`
- 自带管理面板：账号、代理、客户 API Key、任务、视频、压力测试
- 参考图生视频、无水印成片、本地转存与静态分发

> 📖 **第一次使用？先看 [`USAGE.md`](USAGE.md) 使用手册**：核心概念、管理面板每个按钮的作用、从零出第一条视频、日常运营与常见问题。
> 🍪 **加号前先看 [`COOKIE.md`](COOKIE.md)**：怎么从浏览器导出 dola 账号的 Cookie（Cookie-Editor 插件 / 开发者工具）。
>
> 完整的下游对接文档（鉴权、字段、错误码、管理接口）见 [`API.md`](API.md)。

## 支持的模型

| 模型 | 分辨率 | 时长 | 参考素材 | 每次扣点（账号每日默认 4 点） |
|---|---|---|---|---|
| `seedance-2.5` | 720p | 5 / 10 / 30 秒（15 秒出不了，30 秒需开 `pair`，见下） | 图片最多 10 张；不支持参考音频、视频 | 5 秒 = 2、10 秒 = 4、15 秒 = 12（超过每日额度）、**30 秒 = 2**（只出半截、补后半段时共 4） |
| `seedance-2.0` | 720p | 5 / 10 / 15 秒 | 图片最多 10 张；不支持参考音频、视频 | 5 秒 = 1、10 秒 = 2、15 秒 ≈ 3（推算，未实测） |

- 扣点按 dola 回执里的**报价**，随「模型 + 时长」变化（2026-10-14 实测，见 `config.DURATION_COSTS`，可用 `DOLA_DURATION_COSTS` 覆盖）。
  报价超过一个号一天的额度（默认 4 点）的组合受理时直接 422：**seedance-2.5 的 15 秒要 12 点，免费号出不了**，请用 10 秒、30 秒或 seedance-2.0。
- 一个号一天（4 点）能出：2.5 —— 5 秒 2 条 / 10 秒 1 条 / 30 秒 2 条；2.0 —— 5 秒 4 条 / 10 秒 2 条 / 15 秒 1 条。

- **30 秒**：dola 自 2026-10-12 起对 30 秒请求回「只支持 4～15 秒，按 15 秒生成」，但**实际仍按 30 秒出片**（[issue #4](https://github.com/Roins-hub/dola-pool/issues/4)）。由 `DOLA_30S_MODE` 决定：
  `reject`（默认，受理时直接 422 说明原因）/ `pair`（**推荐**）：**先原样请求一次 30 秒**——dola 嘴上回「按最接近的支持时长 15 秒生成，将消耗 2 个」，**实际多数出满 30 秒**（2026-10-14 实测 4 单都是 30.08 秒），只扣 **2 点**；只出了半截（～15 秒）才补一次「后半段」并本地拼接（再扣 2 点）。回执里的秒数不作数，只认落地时长；dola 反问「是否按 15 秒生成」时自动回「确认生成」；只能派给 Cookie 导入的号；补了后半段时两段之间画面可能不完全连贯 / `native`（按 30 秒原样提交的旧逻辑，dola 说「只支持 4～15 秒」后最多再等 10 分钟，不推荐）。
- 其它时长直接拒绝；显式写了 2.0 / 2.5 的请求只按该版本校验，不会悄悄换型号。
- 成片为 1280×720、24fps、H.265，码率约 1.5～4 Mbps（由上游决定，画面越简单文件越小）。
- `seedance-2.0` 在 Dola 端实际使用 Seedance 2.0 Fast 生成。

## 调度与超时

每个任务在 **总时限**内持续换号出片，超时即判定失败，不会无限等待：

1. 按权重/优先级挑一个可调度且空闲的账号生成；
2. 失败（额度不足、每日上限、风控、`generation_voided` 等）自动换下一个号；
3. 一轮账号都试过仍未成功，等失败冷却结束后从头再来一轮；
4. 超过 `DOLA_TASK_DEADLINE`（默认 **600 秒**）仍未成功，任务失败并返回
   `超过 600 秒仍未生成成功，任务失败（已尝试账号: …）`；
5. 全池都已达每日上限或积分不足时立即返回 429，不空等；
6. 已拿到会话、只是轮询超时的任务**不换号重提**，避免 Dola 端重复出片、重复扣额度。

## 一键部署（推荐）

**Linux 服务器**（Ubuntu 20.04+ / Debian 11+），一条命令装好并注册为系统服务：

```bash
# 直接用 IP:8002 访问
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo bash

# 带域名 + 自动 HTTPS（先把域名 A 记录解析到这台服务器）
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo DOLA_DOMAIN=video.example.com bash
```

装完会打印面板地址、默认账号（`admin` / `admin123`，请立即修改）和随机生成的 API Key。
再次执行同一条命令即升级，配置与数据都会保留。可选参数（安装目录、端口、服务名等）见 [`deploy/install.sh`](deploy/install.sh) 开头的注释。

**本地电脑**：下载 [Release](https://github.com/Roins-hub/dola-pool/releases/latest) 的源码包并解压，然后

- Windows：双击 `start-local.bat`
- macOS / Linux：`bash start-local.sh`

首次运行会自动建虚拟环境、装依赖、下载 Chromium，然后打开 `http://127.0.0.1:8002/`。
需要提前装好 Python 3.10+ 和 Node.js 18+。本机不在日本 / 韩国时需要配代理（见 [`USAGE.md`](USAGE.md)）。

## 手动部署

需要 Python 3.10+。dola.com 要求日本/韩国出口，服务器在大陆时必须配置代理。

```bash
git clone https://github.com/Roins-hub/dola-pool.git
cd dola-pool
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
patchright install chromium          # 浏览器兜底路径需要

cp cookies.txt.example cookies.txt   # 填入账号 cookie，一行一个（或启动后在面板里导入）

export DOLA_API_KEYS=sk-your-key     # 对外 API Key，逗号分隔
export DOLA_ADMIN_KEY=change-me      # 管理面板密钥
export DOLA_PROXY=http://user:pass@jp-proxy:port
export DOLA_PUBLIC_BASE=https://your-domain   # 生成 video_url 用的对外地址

# 4. 配置服务
export DOLA_API_KEYS=sk-xxx        # 对外 API key，逗号分隔多个
export DOLA_MAX_CONCURRENCY=0      # 全局出片并发，0 = 不限（默认）
export DOLA_VIDEO_TIMEOUT=300      # 出片超时（秒）

# 5. 启动
uvicorn server:app --host 127.0.0.1 --port 8002   # 只监听本机，由 nginx 对外
```

打开 `http://127.0.0.1:8002/` 进入管理面板，`/docs` 是 Swagger 接口文档。

### 用 systemd 部署

```ini
# /etc/systemd/system/dola-pool.service
[Unit]
Description=dola-pool OpenAI compatible video API
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/dola-pool
EnvironmentFile=/opt/dola-pool/dola.env
ExecStart=/opt/dola-pool/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8002
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

把环境变量写进 `dola.env`（已在 `.gitignore` 中），然后 `sudo systemctl enable --now dola-pool`，再用 nginx 反代到 8002 并配置 HTTPS。

## 调用示例

```bash
# 创建任务
curl -X POST https://your-domain/v1/videos \
  -H "Authorization: Bearer sk-your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.5","prompt":"一只橘色小猫在草地上追逐蝴蝶","duration":10,"ratio":"16:9"}'
# -> {"id":"video_xxx","status":"queued",...}

# 查询任务：queued -> processing -> completed / failed
curl https://your-domain/v1/videos/video_xxx -H "Authorization: Bearer sk-your-key"

# 下载成片
curl -o out.mp4 https://your-domain/v1/videos/video_xxx/content -H "Authorization: Bearer sk-your-key"
```

参考图可用 `reference_images`、`image_url`、`image`、`input_image(s)` 任一字段传公网 URL；时长字段兼容 `duration` / `seconds` / `duration_seconds`。

### 接入 new-api

用 new-api 内置的 **Doubao 任务插件**（渠道类型「任务插件」，插件选 Doubao）：

1. API 地址填本服务地址（例如 `https://your-domain`），密钥填 `DOLA_API_KEYS` 中的一个；
2. 渠道模型填插件认识的官方名 `doubao-seedance-2-0-260128`、`doubao-seedance-2-5-260628`；
3. 模型重定向：
   ```json
   {"doubao-seedance-2-0-260128": "seedance-2.0", "doubao-seedance-2-5-260628": "seedance-2.5"}
   ```
4. 在 new-api 中为这两个模型配置价格（按次或按量均可）。

插件会以 Ark 协议调用本服务的 `/api/v3/contents/generations/tasks`。new-api 渠道页的「测试」按钮不支持任务插件渠道，显示 `Task Plugin channel test is not supported` 属于正常现象，请用真实请求验证。

## 主要配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DOLA_API_KEYS` | 空 | 对外 API Key（逗号分隔）；也可在面板中创建带额度的 Key |
| `DOLA_ADMIN_KEY` | 空 | 管理接口 / 面板密钥 |
| `DOLA_PROXY` | `http://127.0.0.1:7890` | 访问 dola.com 的出口代理 |
| `DOLA_PUBLIC_BASE` | `http://127.0.0.1:8000` | 生成 `video_url` 的对外地址 |
| `DOLA_TASK_DEADLINE` | `600` | 单个任务总时限（秒），期间持续换号，超时判失败 |
| `DOLA_VIDEO_MAX_ATTEMPTS` | `0` | 单任务最多尝试次数，`0` 表示不限、只受总时限约束 |
| `DOLA_MAX_CONCURRENCY` | `3` | 全局并发出片数 |
| `DOLA_MAX_PENDING_TASKS` | `100` | 待处理任务上限，超过返回 429 |
| `DOLA_DAILY_LIMIT` | `4` | 每账号每日额度点数 |
| `DOLA_LIMIT_RESET_TZ` / `DOLA_LIMIT_RESET_HOUR` | `Asia/Tokyo` / `0` | 每日额度重置时区与时刻 |
| `DOLA_PURE_API` | `1` | 优先使用纯 HTTP 出片路径 |
| `DOLA_PURE_FALLBACK_BROWSER` | `1` | 纯 HTTP 失败时回退浏览器路径 |
| `DOLA_PURE_TIMEOUT` | `900` | 纯 HTTP 路径单次出片轮询超时（秒） |
| `DOLA_EXTENSION_ENABLED` | `1` | 加载 `extensions/dola30`（30 秒时长与无水印解析依赖） |

完整列表见 [`config.py`](config.py)。

## 目录结构

| 路径 | 作用 |
|---|---|
| `server.py` | FastAPI 服务：客户接口、Ark 兼容接口、管理接口、任务执行 |
| `browser_pool.py` | 号池调度：选号、换号重试、总时限、额度与冷却 |
| `pool.py` / `store.py` / `user_store.py` | 账号池、任务存储（SQLite）、管理员 |
| `proxy_store.py` | 静态 / 动态代理管理与轮换 |
| `pure_api_gen.py` / `protocol/` | 纯 HTTP 出片协议实现 |
| `browser.py` / `video_worker_ui.py` | 浏览器自动化出片路径 |
| `media.py` | 参考图下载、成片元数据 |
| `web/` | 管理面板前端 |
| `extensions/dola30/` | 第三方 Chromium 扩展（30 秒时长、无水印解析） |
| `tests/` | pytest 测试 |

运行测试：

```bash
pip install pytest
pytest -q tests
```

## 注意事项

- 账号 cookie 约 60 天过期；登录态失效的账号会被自动标记，需重新导入。
- **去水印**：不处理画面，而是直接下载 dola 服务器上的无水印版本（dola 自带的 `lr=unwatermarked` 直链）。第三方解析接口（`protocol/dola_pure_api.py` 中的 `NOWATERMARK_API_ENDPOINT`）**默认关闭**（`DOLA_PURE_REMOVE_WATERMARK=0`）；设为 `1` 才会启用，届时视频播放信息会经 HTTP 发给第三方。原理与风险详见 [`USAGE.md` 的「去水印说明」](USAGE.md#7-去水印说明)。
- `accounts/`、`*.db`、`dola.env`、`cookies.txt`、`downloads/` 均已忽略，切勿提交真实凭据。

## 免责声明

本仓库是个人学习与互操作性研究记录，包含对上游网页接口的逆向实现；代码不含任何账号、cookie、密钥或可用凭据。`extensions/dola30/` 为第三方扩展，其授权与使用权归原作者。使用者须自行遵守 dola.com 服务条款及所在地法律法规，自行评估账号与合规风险。
