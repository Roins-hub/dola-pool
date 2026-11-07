<p align="center"><img src="web/logo.svg" width="96" height="96" alt="dola-pool logo"></p>
<h1 align="center">dola-pool</h1>
<p align="center">把一组 dola.com / 豆包账号组成号池，对外提供 OpenAI 兼容的视频生成与生图 API</p>
<p align="center">
  🌐 <b>中文</b> | <a href="README.en.md">English</a>
  &nbsp;·&nbsp; <a href="https://github.com/Roins-hub/dola-pool/releases/latest">下载最新版</a>
  &nbsp;·&nbsp; <a href="USAGE.md">使用手册</a>
  &nbsp;·&nbsp; <a href="API.md">接口文档</a>
</p>

<img align="right" width="190" alt="交流群二维码" src="https://github.com/user-attachments/assets/39207d3c-955f-49ec-adbc-8db471f23b79" />

[dola.com](https://www.dola.com) 是字节 Seedance 视频模型的国际版，[豆包](https://www.doubao.com) 是国内版。dola-pool 把这两个站点的账号放进同一个号池，对外提供 **OpenAI 风格接口**，也兼容火山方舟（Ark）任务协议，可以直接接入 [new-api](https://github.com/QuantumNous/new-api) 等网关。

- **号池调度**：自动选号、额度记账、风控冷却、失败换号、每日额度重置
- **两个站点混用**：dola 号接 `seedance-2.0-fast` / `seedance-2.5`；豆包号接 `doubao-seedance-*` 视频和 `doubao-seedream-*` 生图。按模型名分派，互不串号
- **两种出片路径**：纯 HTTP 协议（默认），浏览器自动化兜底（仅 dola）
- **对外接口**：`/v1/videos`、`/v1/images/generations`、`/v1/images/edits`，以及 Ark 风格 `/api/v3/contents/generations/tasks`
- **管理面板**：账号、代理、API Key、任务、视频、压力测试，可一键更新；支持中文 / 越南语
- **自动处理 dola 追问**：比例、时长、「已经生成过了」等追问自动回复，可选接入 AI 代答
- **按请求分辨率自动超分**：1080p / 2K / 4K

**交流群**：扫右侧二维码加群。

**第一次用？** 先看 [`USAGE.md`](USAGE.md)：从零到出第一条视频，以及管理面板每个按钮的作用。加号前看 [`COOKIE.md`](COOKIE.md)，里面讲怎么导出账号 Cookie。

<br clear="right">

## 快速开始

### 一键部署（Linux，推荐）

Ubuntu 20.04+ / Debian 11+，一条命令装好并注册为系统服务：

```bash
# 用 IP:8002 访问
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo bash

# 带域名 + 自动 HTTPS（先把域名解析到这台服务器）
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo DOLA_DOMAIN=video.example.com bash
```

装完会打印面板地址、默认账号 `admin` / `admin123`（请立即修改）和随机生成的 API Key。之后在面板「版本」里一键更新即可。可选参数见 [`deploy/install.sh`](deploy/install.sh) 开头的注释。

### Docker

```bash
mkdir dola-pool && cd dola-pool
curl -fsSLO https://raw.githubusercontent.com/Roins-hub/dola-pool/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/Roins-hub/dola-pool/main/deploy/dola.env.example -o dola.env
# 编辑 dola.env：至少改 DOLA_ADMIN_KEY、DOLA_PUBLIC_BASE（对外地址，如 http://你的IP:8000）
docker compose up -d
```

- 面板在 `http://服务器IP:8000/`，数据都在 `./data`，升级不会丢。
- 镜像 `ghcr.io/roins-hub/dola-pool` 只跑**纯 API**，只支持 Cookie 导入的号，不带浏览器。

### 本地电脑

下载 [Release](https://github.com/Roins-hub/dola-pool/releases/latest) 源码包解压，然后：
- Windows 双击 `start-local.bat`；
- macOS / Linux 运行 `bash start-local.sh`。

需要 Python 3.10+ 和 Node.js 18+。本机不在日本 / 韩国时要配代理，见 [`USAGE.md`](USAGE.md)。

<details>
<summary><b>手动部署（源码 + systemd）</b></summary>

dola.com 要求日本 / 韩国出口，服务器在大陆时必须配代理。

```bash
git clone https://github.com/Roins-hub/dola-pool.git && cd dola-pool
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
patchright install chromium                           # 浏览器兜底路径需要

export DOLA_API_KEYS=sk-your-key                      # 对外 API Key，逗号分隔
export DOLA_ADMIN_KEY=change-me                       # 管理面板密钥
export DOLA_PROXY=http://user:pass@jp-proxy:port
export DOLA_PUBLIC_BASE=https://your-domain           # 生成 video_url 用的对外地址
uvicorn server:app --host 127.0.0.1 --port 8002       # 只监听本机，由 nginx 对外
```

用 systemd 常驻：把环境变量写进 `/opt/dola-pool/dola.env`，再建 `/etc/systemd/system/dola-pool.service`：

```ini
[Unit]
Description=dola-pool OpenAI compatible video API
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/opt/dola-pool
EnvironmentFile=/opt/dola-pool/dola.env
ExecStart=/opt/dola-pool/.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8002
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now dola-pool`，再用 nginx 反代到 8002 并配 HTTPS。手动部署要用面板一键更新，还需执行 `sudo bash deploy/install-updater.sh`。

</details>

## 调用示例

```bash
# 创建任务
curl -X POST https://your-domain/v1/videos \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.5","prompt":"一只橘色小猫在草地上追逐蝴蝶","duration":10,"ratio":"16:9"}'
# -> {"id":"video_xxx","status":"queued",...}

# 查询：queued -> processing -> completed / failed
curl https://your-domain/v1/videos/video_xxx -H "Authorization: Bearer sk-your-key"

# 下载成片
curl -o out.mp4 https://your-domain/v1/videos/video_xxx/content -H "Authorization: Bearer sk-your-key"
```

- **参考图**：用 `reference_images`、`image_url`、`image`、`input_image(s)` 任一字段传公网 URL 或 Base64，单张最大 20 MiB。
- **时长**：认 `duration`、`seconds`、`duration_seconds`。
- **比例**：以请求参数为准。提示词里写了别的比例时，会注明以参数为准。
- **分辨率**：可带 `resolution`（`720p` / `1080p` / `2k` / `4k`）。后台开启自动超分时，出片后超分到该档位，见 [UPSCALE.md](UPSCALE.md)。
- **参考图来源说明**：带参考图的任务，默认附加一句「参考图为 AI 生成素材」（`DOLA_DEFAULT_REFERENCE_SOURCE=ai_generated`）。不需要时传 `"reference_source": "unspecified"`。这只是如实说明素材来源，不保证通过上游审核。

豆包号的视频把模型换成 `doubao-seedance-*` 即可。生图只接豆包号，同步返回：

```bash
curl -X POST https://your-domain/v1/images/generations \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedream-5.0-flash","prompt":"雪地里的红色狐狸","size":"1536x1024"}'

# 图生图 / 改图：multipart 上传，也可以在 JSON 里用 reference_images 传 URL
curl -X POST https://your-domain/v1/images/edits -H "Authorization: Bearer sk-your-key" \
  -F model=doubao-seedream-5.0-flash -F prompt="把猫的胡子去掉" -F image=@cat.jpg
```

字段、错误码和管理接口见 [`API.md`](API.md)。

## 支持的模型

### dola 号

| 模型 | 时长 | 每次扣点（每号每天默认 4 点） |
|---|---|---|
| `seedance-2.5` | 5 / 10 / 30 秒 | 5 秒 = 2、10 秒 = 4、30 秒 = 2（只出半截时补后半段，共 4） |
| `seedance-2.0-fast` | 5 / 10 秒，开启工作模式后加 15 秒 | 5 秒 = 2、10 秒 = 4；15 秒实际扣点未知 |

- **输出与参考图**：成片 1280×720、24fps、H.265。参考图最多 10 张，不支持参考音频和视频。
- **2.0 的 15 秒（可选）**：设置 `DOLA_WORK_15S=1` 后走 Dola 工作模式，支持参考图。需要 CloakBrowser，Docker 镜像暂不支持；助手可能改写提示词，一单约 6 分钟，出过一条的号当天不再派发。
- **15 秒**：seedance-2.5 的 15 秒要 12 点，超过免费号每天的额度，会直接拒绝（422）；seedance-2.0-fast 的 15 秒在普通聊天里已下线，只能走上面的工作模式。
- **30 秒**：由 `DOLA_30S_MODE` 决定。默认 `reject` 直接拒绝。推荐设 `pair`：先原样请求 30 秒，dola 多数会出满，只扣 2 点；只出了半截时，再补一次后半段并本地拼接。只能派给 Cookie 导入的号。详见 [issue #4](https://github.com/Roins-hub/dola-pool/issues/4)。
- **2.0 的名字**：`seedance-2.0-fast` 就是网页上的「Seedance 2.0 Fast」，旧名 `seedance-2.0` 仍可用。

### 豆包号

| 模型 | 时长 | 谁能用 | 豆包标注的消耗 |
|---|---|---|---|
| `doubao-seedance-2.0-mini` | 4～15 秒任意整数 | 免费号、会员 | 日常生成 |
| `doubao-seedance-2.0-fast` | 4～15 秒 | 免费号、会员 | 快速出片 |
| `doubao-seedance-2.0` | 4～15 秒 | 只有会员 | 2 倍 |
| `doubao-seedance-2.5` | 4～30 秒（原生，不拼接） | 只有会员 | 5 倍 |

生图（Seedream）：

| 模型 | 豆包里的名字 |
|---|---|
| `doubao-seedream-5.0-flash` | Seedream 5.0 Flash（专业快速） |
| `doubao-seedream-5.0-pro` | Seedream 5.0 Pro（4 倍消耗） |
| `doubao-seedream-4.5` | Seedream 4.5 |
| `doubao-seedream-4.0` | Seedream 4.0 |

生图约 20 秒出图，2K 档。

<details>
<summary><b>豆包号的细节</b>（额度、水印、付费确认、会员识别）</summary>

- **额度**：豆包不公开每日免费额度，回复里也不报剩余，上表的「倍数」是豆包页面的原话。面板显示「今日已出 N 条」，`DOUBAO_DAILY_LIMIT` 只是兜底上限。
- **付费确认**：免费额度用完时，豆包会问「本次将消耗付费额度，是否继续？」。`DOUBAO_ALLOW_PAID=1`（默认）时自动确认，设 `0` 则换号。
- **会员识别**：免费号请求 2.0 / 2.5 时，豆包会提议改用 Mini。号池**绝不确认**，会把这个号记为「免费号」，换会员号重派。
- **参数确认**：豆包先回「请先确认参数」，号池核对时长、模型都对才确认，对不上不确认、直接报错。
- **水印**：「AI 生成」明水印是按号的设置。`DOUBAO_AUTO_NO_WATERMARK=1`（默认）时，号池用官方设置接口替号关掉，再走官方无水印下载。内容公开发布时，需由发布方自行标注「AI 生成」。
- **比例**：视频比例支持 `16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9`。生图比例从提示词文字里读，号池会按格式自动写进去。
- **出口与导入方式**：境外服务器可直接用，不需要日韩代理。只支持 Cookie 导入，见 [COOKIE.md](COOKIE.md)。
- **插件官方名**：new-api Doubao 插件的官方名（如 `doubao-seedance-2-0-260128`）不是豆包号模型，照旧走 dola。

</details>

## 接入 new-api

用 new-api 内置的 **Doubao 任务插件**（渠道类型「任务插件」，插件选 Doubao）：

1. API 地址填本服务地址，密钥填 `DOLA_API_KEYS` 中的一个；
2. 渠道模型填插件认识的官方名 `doubao-seedance-2-0-260128`、`doubao-seedance-2-5-260628`，再做模型重定向：
   ```json
   {"doubao-seedance-2-0-260128": "seedance-2.0-fast", "doubao-seedance-2-5-260628": "seedance-2.5"}
   ```
3. 豆包号的视频模型直接加 `doubao-seedance-2.0-mini` 等名字；生图按 OpenAI 渠道接 `doubao-seedream-*`。

渠道页的「测试」按钮不支持任务插件，显示 `Task Plugin channel test is not supported` 是正常的，请用真实请求验证。

## 调度与出片

- **选号与换号**：按权重 / 优先级挑空闲的号。额度不足、风控、上游确认失败时自动换号；全池额度用完立即返回 429，不空等。
- **任务时限**：取得账号后有 `DOLA_TASK_DEADLINE`（默认 600 秒）的尝试时间，排队时间不算在内。超时就判失败，并列出试过哪些号。
- **不会重复出片**：已经拿到上游会话、只是等结果超时的任务，**不换号重提**，避免重复出片、重复扣额度。
- **dola 追问**：dola 不直接生成而是反问时（用哪个比例、要不要开始、已经生成过了……），程序先按任务参数自动回复两次。仍在问时，交给可选的 AI 代答（面板「设置 → AI 代答」）。都不行才结束任务，这时尚未生成，不扣点。
- **参考图上传**：浏览器路径上传参考图时，字节图片服务器直连，其余流量走账号自己的代理。
- **全局并发**：面板「设置 → 全局并发」，`0` 为不限。每个号同时只处理一个任务。

## 常用配置

写在 `dola.env`（一键部署）或 `.env` / 环境变量里。完整列表见 [`config.py`](config.py)。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DOLA_API_KEYS` | 空 | 对外 API Key，逗号分隔；也可以在面板里创建带额度的 Key |
| `DOLA_ADMIN_KEY` | 空 | 管理接口密钥 |
| `DOLA_PROXY` | `http://127.0.0.1:7890` | 访问 dola.com 的默认出口代理；留空 = 直连。账号可以单独绑定代理 |
| `DOLA_PUBLIC_BASE` | `http://127.0.0.1:8000` | 生成 `video_url` 用的对外地址 |
| `DOLA_DAILY_LIMIT` | `4` | 每个 dola 号每天的额度点数 |
| `DOLA_LIMIT_RESET_TZ` / `_HOUR` | `UTC` / `0` | 每日额度重置时刻（UTC 0 点 = 北京 8 点） |
| `DOLA_TASK_DEADLINE` | `600` | 取得账号后的尝试时间（秒） |
| `DOLA_MAX_QUEUE_WAIT_SECONDS` | `1800` | 排队等名额 / 账号的上限（秒） |
| `DOLA_MAX_CONCURRENCY` | `0` | 全局并发初始值，`0` = 不限；面板里保存的值优先 |
| `DOLA_MAX_PENDING_TASKS` | `100` | 待处理任务上限，超过返回 429 |
| `DOLA_30S_MODE` | `reject` | 30 秒请求：`reject` 拒绝 / `pair` 先请求 30 秒，半截时补后半段 |
| `DOLA_VIDEO_DELIVERY` | `download` | `download` 下载到服务器；`link` 下发短链，302 到上游原片，视频流量不经过服务器 |
| `DOLA_DEFAULT_REFERENCE_SOURCE` | `ai_generated` | 带参考图时默认附加来源说明；`unspecified` = 只对显式标记的任务附加 |
| `DOLA_UPLOAD_PROXY_BYPASS` | `*.bytevcloudapi.com,*.vodupload.com` | 浏览器上传参考图时直连的域名；留空 = 全部走代理 |
| `DOLA_STALL_REPLY_SECONDS` | `180` | dola 说完话不开工超过这么久就结束任务（未扣点）；`0` = 关闭 |
| `DOLA_GENERATION_FAILURE_RETRIES` | `2` | 上游确认生成失败后最多换号重提几次 |
| `DOLA_PORTRAIT_RETRIES` | `3` | dola 以「肖像保护」拒绝、且还没开始生成时，换号重提几次（审核结果因号而异；拒绝不扣点）；`0` = 关闭 |
| `DOLA_PURE_API` / `DOLA_PURE_FALLBACK_BROWSER` | `1` / `1` | 优先纯 HTTP 出片；失败时回退浏览器 |
| `DOLA_COOKIE_RUNTIME` | `pure` | `cloak` = Dola Cookie 号使用 CloakBrowser 独立浏览器环境（见下） |
| `DOUBAO_ALLOW_PAID` | `1` | 豆包免费额度用完后自动走付费额度；`0` = 换号 |
| `DOUBAO_AUTO_NO_WATERMARK` | `1` | 自动替豆包号关「AI 生成」明水印 |
| `DOLA_IMAGE_TIMEOUT` | `240` | 生图一次请求最多等多久（秒） |

<details>
<summary><b>严格 Cookie 浏览器隔离（可选）</b></summary>

默认 Cookie 号走纯 HTTP。要让每个 Dola Cookie 号固定在独立浏览器环境里，可以这样做：
- 在 Linux 上用 Xvfb + systemd 部署；
- 安装 `deploy-cloak-requirements.txt`；
- 在配置里加上：

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_CLOAK_HEADLESS=0
CLOAKBROWSER_CACHE_DIR=/opt/dola-pool/.cloakbrowser
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
```

- **工作方式**：每个号使用 `accounts/<账号>/cloak-profile/`，并绑定自己的固定代理。验证、提交、轮询都走这个 profile。
- **导入顺序**：批量导入时先勾选「跳过在线验证」，绑好专属代理后再逐个验证。
- **隔离范围**：这是浏览器资料隔离，不是每号一台虚拟机；不同代理记录也不保证不同公网 IP。
- **不支持的场景**：不适用于纯 API 的 Docker 镜像，30 秒 `pair` 也只适用于纯 API 号。CloakBrowser 内核另有许可证和并发限制。

完整步骤见 [`USAGE.md`](USAGE.md)。

</details>

<details>
<summary><b>目录结构</b></summary>

| 路径 | 作用 |
|---|---|
| `server.py` | FastAPI 服务：客户接口、Ark 兼容接口、管理接口、任务执行 |
| `browser_pool.py` | 号池调度：选号、换号重试、时限、额度与冷却 |
| `pool.py` / `store.py` / `user_store.py` | 账号池、任务存储（SQLite）、管理员 |
| `proxy_store.py` | 静态 / 动态代理管理与轮换 |
| `pure_api_gen.py` / `protocol/` | 纯 HTTP 出片协议（dola；豆包站点配置在 `protocol/doubao_site.py`） |
| `doubao_gen.py` / `image_gen.py` | 豆包号出视频、生图 |
| `browser.py` / `video_worker_ui.py` | 浏览器自动化出片路径 |
| `upstream_reply.py` / `dola_responder.py` | dola 追问的自动回复与 AI 代答 |
| `upscale.py` | 视频超分 |
| `web/` | 管理面板前端；`web/i18n/` 为多语言，见 [`I18N.md`](I18N.md) |
| `deploy/` | 一键安装、一键更新脚本 |
| `Dockerfile` / `docker-compose.yml` / `docker/` | Docker 镜像与编排 |
| `tests/` | pytest 测试：`pip install pytest && pytest -q tests` |

</details>

## 注意事项

- **Cookie 有效期**：账号 Cookie 约 60 天过期，失效的号会被自动标记，需重新导入。导入号池的号**不要在浏览器里点退出登录**，退出后 Cookie 会立刻失效。
- **dola 水印**：号池下载的是 dola 返回数据里同一条视频的无水印版本（`lr=unwatermarked`），不对画面做处理。第三方解析接口默认关闭（`DOLA_PURE_REMOVE_WATERMARK=0`），原理与风险见 [`USAGE.md`](USAGE.md#7-去水印说明)。
- **凭据安全**：`accounts/`、`*.db`、`dola.env`、`cookies.txt`、`downloads/` 都已加入忽略列表，切勿提交真实凭据。

## 许可协议

本项目自己的代码以 [MIT 协议](LICENSE) 开源。以下内容不属于本项目，不在 MIT 授权范围内：

- `extensions/dola30/`：第三方扩展，授权与使用权归原作者。
- `bdcaptcha.js`、`protocol/js/bdms-sdk.js`：从上游网页获取的 SDK，权利归其所有者。
- `web/fonts/`：IBM Plex 字体，遵循 [SIL Open Font License 1.1](web/fonts/OFL.txt)。

## 免责声明

本仓库是个人学习与互操作性研究记录，包含对上游网页接口的逆向实现。代码不含任何账号、Cookie、密钥或可用凭据。

使用者须自行遵守 dola.com、doubao.com 的服务条款及所在地法律法规，包括人工智能生成内容的标识要求，并自行评估账号与合规风险。
