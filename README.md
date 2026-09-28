# dola-pool

把一组 [dola.com](https://www.dola.com)（字节 Seedance 视频模型国际版）账号组成号池，对外提供 **OpenAI 兼容的视频生成 API**，并兼容火山方舟（Ark）任务式协议，可直接接入 [new-api](https://github.com/QuantumNous/new-api) 等网关。

- 多账号号池：自动选号、额度记账、风控冷却、失败换号、每日额度重置
- 两种出片路径：纯 HTTP 协议（默认）与 Chromium 浏览器自动化（兜底）
- 对外接口：OpenAI 风格 `/v1/videos`、Ark 风格 `/api/v3/contents/generations/tasks`
- 自带管理面板：账号、代理、客户 API Key、任务、视频、压力测试
- 参考图生视频、无水印成片、本地转存与静态分发

> 完整的下游对接文档（鉴权、字段、错误码、管理接口）见 [`API.md`](API.md)。

## 支持的模型

| 模型 | 分辨率 | 时长 | 参考素材 | 每次扣点（账号每日默认 4 点） |
|---|---|---|---|---|
| `seedance-2.5` | 720p | 5 / 10 / 30 秒 | 图片最多 10 张；不支持参考音频、视频 | 2 |
| `seedance-2.0` | 720p | 5 / 10 / 15 秒 | 图片最多 10 张；不支持参考音频、视频 | 3 |

- 其它时长直接拒绝；显式写了 2.0 / 2.5 的请求只按该版本校验，不会悄悄换型号。
- 成片为 1280×720、24fps、H.265，码率约 1.5~4 Mbps（由上游决定，画面越简单文件越小）。
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

## 快速开始

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
- 无水印解析会把视频信息发送给第三方解析服务（见 `protocol/dola_pure_api.py` 中的 `NOWATERMARK_API_ENDPOINT`），介意可设 `DOLA_PURE_REMOVE_WATERMARK=0`。
- `accounts/`、`*.db`、`dola.env`、`cookies.txt`、`downloads/` 均已忽略，切勿提交真实凭据。

## 免责声明

本仓库是个人学习与互操作性研究记录，包含对上游网页接口的逆向实现；代码不含任何账号、cookie、密钥或可用凭据。`extensions/dola30/` 为第三方扩展，其授权与使用权归原作者。使用者须自行遵守 dola.com 服务条款及所在地法律法规，自行评估账号与合规风险。
