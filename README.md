> 🌐 **中文** | [Tiếng Việt](README.vi.md)

有兴趣的可以加群交流
<img width="1305" height="2321" alt="301130c1842c3b30432394fc276a7a58" src="https://github.com/user-attachments/assets/39207d3c-955f-49ec-adbc-8db471f23b79" />

# dola-pool

**当前版本：v2.6.1** — 修复历史加号失败覆盖当前验证/生成状态，保留可展开的历史错误；
包含 v2.6.0 的全局并发、任务诊断与验证提速。完整改动见 [更新日志](CHANGELOG.md)。

把一组 [dola.com](https://www.dola.com)（字节 Seedance 视频模型国际版）和 [豆包 doubao.com](https://www.doubao.com)（国内版）账号组成号池，对外提供 **OpenAI 兼容的视频生成 / 生图 API**，并兼容火山方舟（Ark）任务式协议，可直接接入 [new-api](https://github.com/QuantumNous/new-api) 等网关。

- 多账号号池：自动选号、额度记账、风控冷却、失败换号、每日额度重置
- **两个站点的号混用**：dola 号接 `seedance-2.0-fast` / `seedance-2.5`，豆包号接 `doubao-seedance-*` 视频和 `doubao-seedream-*` 生图，按模型名自动分派、互不串号
- 两种出片路径：纯 HTTP 协议（默认）与 Chromium 浏览器自动化（兜底，仅 dola）
- 可选严格 Cookie 隔离：Dola Cookie 账号按账号绑定 CloakBrowser 持久 profile、固定 fingerprint seed 和独立出口
- 对外接口：OpenAI 风格 `/v1/videos`、`/v1/images/generations`、`/v1/images/edits`，Ark 风格 `/api/v3/contents/generations/tasks`
- 自带管理面板：账号、代理、客户 API Key、任务、视频、压力测试；界面支持中文 / 越南语 / 中越双语（见 [`I18N.md`](I18N.md)）
- 账号 Cookie 单个 JSON / 批量 ZIP 导出；添加账号时可绑定代理，批量添加支持多选代理轮流分配（登录/验证前生效）
- 参考图生视频 / 图生图、本地转存与静态分发（水印情况见「注意事项」）

> 📖 **第一次使用？先看 [`USAGE.md`](USAGE.md) 使用手册**：核心概念、管理面板每个按钮的作用、从零出第一条视频、日常运营与常见问题。
> 🍪 **加号前先看 [`COOKIE.md`](COOKIE.md)**：怎么从浏览器导出 dola / 豆包账号的 Cookie（Cookie-Editor 插件 / 开发者工具）。
>
> 完整的下游对接文档（鉴权、字段、错误码、管理接口）见 [`API.md`](API.md)。

## 支持的模型

| 模型 | 分辨率 | 时长 | 参考素材 | 每次扣点（账号每日默认 4 点） |
|---|---|---|---|---|
| `seedance-2.5` | 720p | 5 / 10 / 30 秒（15 秒出不了，30 秒需开 `pair`，见下） | 图片最多 10 张；不支持参考音频、视频 | 5 秒 = 2、10 秒 = 4、15 秒 = 12（超过每日额度）、**30 秒 = 2**（只出半截、补后半段时共 4） |
| `seedance-2.0-fast` | 720p | 5 / 10 / 15 秒 | 图片最多 10 张；不支持参考音频、视频 | 5 / 10 / 15 秒均为 3 点 |

- 扣点按 dola 回执里的**报价**，随「模型 + 时长」变化（2026-10-14 实测，见 `config.DURATION_COSTS`，可用 `DOLA_DURATION_COSTS` 覆盖）。
  报价超过一个号一天的额度（默认 4 点）的组合受理时直接 422：**seedance-2.5 的 15 秒要 12 点，免费号出不了**，请用 10 秒、30 秒或 seedance-2.0-fast。
- 一个号一天（4 点）能出：2.5 —— 5 秒 2 条 / 10 秒 1 条 / 30 秒 2 条；2.0 —— 5 / 10 / 15 秒均为 1 条（剩 1 点）。

- **30 秒**：dola 自 2026-10-12 起对 30 秒请求回「只支持 4～15 秒，按 15 秒生成」，但**实际仍按 30 秒出片**（[issue #4](https://github.com/Roins-hub/dola-pool/issues/4)）。由 `DOLA_30S_MODE` 决定：
  `reject`（默认，受理时直接 422 说明原因）/ `pair`（**推荐**）：**先原样请求一次 30 秒**——dola 嘴上回「按最接近的支持时长 15 秒生成，将消耗 2 个」，**实际多数出满 30 秒**（2026-10-14 实测 4 单都是 30.08 秒），只扣 **2 点**；只出了半截（～15 秒）才补一次「后半段」并本地拼接（再扣 2 点）。回执里的秒数不作数，只认落地时长；dola 反问「是否按 15 秒生成」时自动回「确认生成」；只能派给 Cookie 导入的号；补了后半段时两段之间画面可能不完全连贯 / `native`（按 30 秒原样提交的旧逻辑，dola 说「只支持 4～15 秒」后最多再等 10 分钟，不推荐）。
- 其它时长直接拒绝；显式写了 2.0 / 2.5 的请求只按该版本校验，不会悄悄换型号。
- 成片为 1280×720、24fps、H.265，码率约 1.5～4 Mbps（由上游决定，画面越简单文件越小）。
- `seedance-2.0-fast` 就是 dola 网页上的「Seedance 2.0 Fast」（dola 没有标准版 Seedance 2.0）。旧名 `seedance-2.0` 仍然可以用，自动按 2.0 Fast 处理，但不再出现在 `/v1/models` 里。

### 豆包（doubao.com）号的模型（2.3.0 起）

号池可以同时放 dola 号和豆包号：**豆包号只接下面这 4 个模型，dola 号只接 `seedance-2.0-fast` / `seedance-2.5`**，互不串。

| 模型 | 豆包里的名字 | 时长 | 谁能用 | 豆包页面的消耗标注 | 实测（2026-10-18） |
|---|---|---|---|---|---|
| `doubao-seedance-2.0-mini` | Seedance 2.0 Mini | 4～15 秒任意整数 | 免费号、会员 | 「日常生成使用」，不标倍数（按 1 倍算） | 免费号 5 秒连出 3 条都还在免费额度内；每条约 2 分钟出片 |
| `doubao-seedance-2.0-fast` | Seedance 2.0 Fast | 4～15 秒任意整数 | 免费号、会员 | 「快速出片选择」，不标倍数 | 未测 |
| `doubao-seedance-2.0` | Seedance 2.0 | 4～15 秒任意整数 | **只有会员** | 「进阶画面表现 · **2 倍消耗**」 | 未测；免费号请求会被豆包拒绝（号池自动换会员号） |
| `doubao-seedance-2.5` | Seedance 2.5 | 4～**30** 秒任意整数（原生，不拼接） | **只有会员** | 「旗舰视频标杆 · **5 倍消耗**」 | 体验套餐号出了 1 条 5 秒后，当天免费额度就用完了，再出 Mini 也提示要走付费额度 |

> 豆包**不公开每日免费额度是多少**，回复里也不报剩余，只会说「本次生成将消耗每日免费额度」；用完后改问
> 「本次将消耗付费额度，是否继续？」（号池按 `DOUBAO_ALLOW_PAID` 处理）。所以上表的「倍数」是豆包页面的原话，
> 能出几条以实际为准；额度规则豆包随时在调，2.5 的免费体验据报道 11 月后可能取消。

- 比例支持 `16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9`，不传则由豆包自动定。成片 720p。
- **水印**：豆包的「AI 生成」明水印是**按号**的设置（豆包设置 → AI 生成水印管理 → 生成的图片、视频）。
  `DOUBAO_AUTO_NO_WATERMARK=1`（默认）时号池在探测 / 出片前读这项设置，没关就用豆包官方设置接口替号关掉，
  之后走官方下载接口拿的就是干净成片（免费号、会员号都实测过）；面板「来源」列显示「无水印 ✓ / ⚠ 未关水印」。
  关掉明水印后，视频公开发布时要由发布方自行标注「AI 生成」（豆包设置页里的法规说明）。
- 豆包会先回「请先确认参数」，号池核对时长、模型都对才自动回「确认生成」；对不上就不确认（不扣额度）、直接报错。
- 免费额度用完时豆包问「本次将消耗付费额度，是否继续？」：`DOUBAO_ALLOW_PAID=1`（默认）自动确认走付费；
  设成 `0` 则判这个号今天额度用完、换号。号没有付费额度时同样换号。
- 免费号请求 2.0 / 2.5 时豆包回「当前无法使用 Seedance 2.5，是否改用 Mini」：号池**绝不确认**（会悄悄降级），
  把这个号记为「免费号」、换会员号重派；会员模型出片成功的号记为「会员」，面板「来源」列能看到。
- 豆包不报剩余条数，面板显示「今日已出 N 条」；`DOUBAO_DAILY_LIMIT`（默认 999）只是兜底上限。
- 境外服务器能直接用（2026-10-18 从日本出口实测通过），不需要日本 / 韩国代理。
- 只支持 Cookie 导入（见 [COOKIE.md](COOKIE.md) 的「豆包号」一节）。new-api Doubao 插件的官方名
  `doubao-seedance-2-0-260128` 等**不是**豆包号模型，照旧走 dola。

### 生图（Seedream，2.4.0 起，只接豆包号）

OpenAI 兼容、**同步返回**：`POST /v1/images/generations`（文生图）、`POST /v1/images/edits`（图生图 / 改图），只派给豆包号：

| 模型 | 豆包网页里的名字 | 比例 | 一次出几张 |
|---|---|---|---|
| `doubao-seedream-5.0-flash` | Seedream 5.0 Flash（专业快速） | 自动 / 9:16 / 2:3 / 3:4 / 1:1 / 4:3 / 3:2 / 16:9 | 1 |
| `doubao-seedream-5.0-pro` | Seedream 5.0 Pro（专业出图 · 4 倍消耗） | 同上 | 1 |
| `doubao-seedream-4.5` | Seedream 4.5（日常生成） | 同上 | 1 |
| `doubao-seedream-4.0` | Seedream 4.0（基础生图） | 同上 | 1 |

- 请求体：`{"model", "prompt", "size"（如 1536x1024，自动换算成最接近的比例）或 "ratio"（如 16:9）,
  "response_format": "url" | "b64_json", "reference_images": [图片 URL / data URL]}`；edits 也收 multipart 的 `image` / `image[]` 文件。
- 返回：`{"created", "data": [{"url"} 或 {"b64_json"}], "model", "account", "unwatermarked"}`。分辨率 2K 档（16:9 实测 2720×1520），约 20 秒出图。
- **水印**：走豆包官方无水印下载接口；号在豆包设置里关了「AI 生成水印」就是干净图，号池会自动替号关（同视频，`DOUBAO_AUTO_NO_WATERMARK`）。
- 豆包从**文字**里读比例：号池按「生成图片：<提示词>，16:9」的格式发，否则豆包按 1:1 出。
- 整个请求最多等 `DOLA_IMAGE_TIMEOUT`（默认 240 秒），期间失败自动换号；上游拒绝出图（审核）不换号、直接返回原因。
- 生图不记视频额度、不按额度挑号（只跳过风控 / 待激活 / 异常 / 出口限流中 / 正在出片的号）。

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

### Docker 部署

镜像发布在 `ghcr.io/roins-hub/dola-pool`（每个版本标签自动构建，`latest` 是最新版）。需要 Docker 20+ 和 Docker Compose：

```bash
mkdir dola-pool && cd dola-pool
curl -fsSLO https://raw.githubusercontent.com/Roins-hub/dola-pool/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/Roins-hub/dola-pool/main/deploy/dola.env.example -o dola.env
# 编辑 dola.env：至少改 DOLA_ADMIN_KEY、DOLA_PUBLIC_BASE（对外访问地址，如 http://你的IP:8000）
docker compose up -d
```

- 面板：`http://服务器IP:8000/`；健康检查 `http://服务器IP:8000/health`。端口在 `docker-compose.yml` 的 `ports` 里改。
- **数据都在 `./data`**（账号、数据库、成片、参考图），升级镜像不丢：`docker compose pull && docker compose up -d`。
- 想用本地代码构建：把 `docker-compose.yml` 里的 `image:` 换成 `build: .`，再 `docker compose up -d --build`。
- 镜像只跑**纯 API**（Cookie 导入的 dola / 豆包号），不带浏览器，Google 账号登录的号在 Docker 里用不了。
- 镜像约 1.6 GB（Python 3.12 + Node + ffmpeg + opencv）。

### 严格 Cookie 浏览器隔离（2.5.0）

默认 Cookie 账号仍走纯 HTTP。需要让 Dola Cookie 账号固定在独立浏览器环境时，使用 Linux
Xvfb/systemd 部署并安装可选的 `deploy-cloak-requirements.txt`：

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_CLOAK_HEADLESS=0
CLOAKBROWSER_CACHE_DIR=/opt/dola-pool/.cloakbrowser
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_EGRESS_THROTTLE_ROTATE=0
DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
```

严格模式下，每个 Dola Cookie 账号使用 `accounts/<账号>/cloak-profile/`，并绑定自己的固定代理或
sticky session；验证、提交和轮询都走该 profile。命中 `710022002` 时按出口和账号退避，默认不
自动换 IP。批量导入时先勾选“跳过在线验证”，导入后绑定账号专属代理，再逐个点击“验证”。

该模式不适用于当前纯 API Docker 镜像；完整部署和迁移步骤见 [`USAGE.md`](USAGE.md)。

主分支已修复 v2.5.0 的会话 Cookie 在第二次启动时丢失问题：关闭时保存账号独立的会话快照，
下次只恢复缺失的会话 Cookie；退出登录后不重放原始导入。此修复不在原 v2.5.0 发布包内。

这是浏览器资料隔离，不是每账号独立虚拟机。账号仍共享服务器、应用进程和调度数据库；视频下载
仍使用独立 HTTP 客户端。不同代理记录或 sticky session 不保证不同公网 IP，应通过实际出口探测确认。
`710022002` 表示上游拒绝/限流，不能单凭错误码认定是指纹或 IP 原因，也不能保证换浏览器后消失。
30 秒 `pair` 和纯 API 超时后台取回仍只适用于纯 API 账号。CloakBrowser 内核另有许可证与版本/并发限制。

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

### 纯 AI 参考图来源说明（按任务选择）

只有确认本次全部参考图中的人物均为纯 AI 生成的虚构角色时，才传
`"reference_source": "ai_generated"`。服务在提交视频生成时附加来源说明，
任务列表、提示词弹窗、复制和 API 返回的 `prompt` 仍保留用户原文。
来源标记和附加说明分别保存在任务的 `reference_source`、`prompt_supplement` 字段，
后台参考图旁显示“AI 素材”，重启恢复时沿用这条任务的说明。

```json
{
  "model": "seedance-2.0-fast",
  "prompt": "人物缓缓抬眸，花瓣飘落，镜头缓慢推进",
  "duration": 10,
  "ratio": "16:9",
  "reference_images": ["https://your-domain/fictional-character.png"],
  "reference_source": "ai_generated"
}
```

默认值为 `unspecified`，省略时不会附加说明，也不会根据图片外观或提示词猜测素材来源。
无参考图却选择 `ai_generated`、或传入其他来源值会返回 422。
OpenAI JSON、multipart（`-F reference_source=ai_generated`）和 Ark 任务接口均支持该字段；
经 new-api 转发时需要调用方和中间渠道保留这个顶层字段，未透传则不启用。
这只是如实说明素材来源，不是解除肖像保护的开关，不保证通过上游审核；内容拒绝仍正常结束任务。

豆包号的视频把模型换成 `doubao-seedance-*` 即可（时长 4～15 秒任意整数，2.5 到 30 秒）：

```bash
curl -X POST https://your-domain/v1/videos \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedance-2.0-mini","prompt":"一只橘色小猫在草地上追逐蝴蝶","duration":7,"ratio":"9:16"}'
```

生图（只接豆包号，同步返回）：

```bash
curl -X POST https://your-domain/v1/images/generations \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedream-5.0-flash","prompt":"雪地里的红色狐狸","size":"1536x1024"}'
# -> {"created":1792373000,"data":[{"url":"https://your-domain/videos/img_xxx.png"}],...}

# 图生图 / 改图：multipart 上传图片（也可以 JSON 里用 reference_images 传 URL）
curl -X POST https://your-domain/v1/images/edits -H "Authorization: Bearer sk-your-key" \
  -F model=doubao-seedream-5.0-flash -F prompt="把猫的胡子去掉" -F image=@cat.jpg
```

### 接入 new-api

用 new-api 内置的 **Doubao 任务插件**（渠道类型「任务插件」，插件选 Doubao）：

1. API 地址填本服务地址（例如 `https://your-domain`），密钥填 `DOLA_API_KEYS` 中的一个；
2. 渠道模型填插件认识的官方名 `doubao-seedance-2-0-260128`、`doubao-seedance-2-5-260628`；
3. 模型重定向：
   ```json
   {"doubao-seedance-2-0-260128": "seedance-2.0-fast", "doubao-seedance-2-5-260628": "seedance-2.5"}
   ```
4. 在 new-api 中为这两个模型配置价格（按次或按量均可）。
5. 豆包号的视频模型同理：渠道里加 `doubao-seedance-2.0-mini` 等名字（或对外起别名再用模型重定向映射过来）；
   new-api 插件的官方名（`doubao-seedance-2-0-260128` 等）**不会**派给豆包号，仍按 dola 处理。
6. 生图走 OpenAI 图片接口（`/v1/images/generations`），在 new-api 里按 OpenAI 渠道接 `doubao-seedream-*` 模型即可。

插件会以 Ark 协议调用本服务的 `/api/v3/contents/generations/tasks`。new-api 渠道页的「测试」按钮不支持任务插件渠道，显示 `Task Plugin channel test is not supported` 属于正常现象，请用真实请求验证。

## 主要配置

### 任务状态、失败诊断与隔离

- 账号的“验证中 / 探测中”与“准备生成 / 等待上游结果 / 下载处理中”分别显示。
  验证只检查登录与配置允许的探活，不提交视频；验证成功不保证能出片。
- 验证按钮点击后立即显示状态与等待秒数，列表刷新不会重置按钮或允许重复提交。
  同一账号浏览器收到第一方用户接口的明确登录标记，且 Cookie、输入框检查通过后可提前结束；
  没有可靠标记时保留原 24～34 秒稳定检查。显示启动、加载、稳定检查、关闭等实际阶段，
  页面未就绪或网络超时报告“未确认”，不把网络慢直接当作登录失效。单次常规验证上限 90 秒；
  失效后的自动重登另行执行，仍可能较久。提速不改变账号代理或使用缓存冒充当次验证。
- 浏览器只使用当前会话中有明确顺序、且晚于最近用户消息的最新助手回复做判断。
  明确内容拒绝、缺少信息或生成失败会保留原话并结束，不再用历史受理话术覆盖失败。
  “无法生成 / 本次未扣额度”不等于“额度不足”；只有明确的额度不足文案才走额度处理。
- 仅在最新助手消息明确询问时长时自动确认一次。普通“请选择方向 / 请上传图片”不会触发回复。
- 排队等待、取得可用账号后的尝试预算、浏览器提交准备和已受理生成等待分别计时。
  提交准备超时但结果不确定时不会自动换号重提；已生成视频的下载失败同样不会重提。
- 浏览器参考图上传遇到连接错误后，按共享出口暂停该线路的后续图生任务。对已知 ImageX
  域名做一次不含账号 Cookie 的连通性诊断：连接失败暂停 300 秒，可连通或未知暂停 30 秒。
  不换代理、不清账号状态、不把网络失败记成额度不足；HTTP 响应只证明连通，不证明能上传。
- 新任务提供“尝试记录”，记录账号、阶段、耗时和失败原因，保留最近 100 次且总计不超过
  64 KiB；历史任务不会凭空补出之前未保存的记录。
- 高码率地址查询与视频下载固定使用账号代理，SOCKS 代理经本地 HTTP 中转；失败不直连。
  严格隔离模式缺少有效绑定代理时明确报错，日志不再输出视频签名模型。
  这修复了下载路径的回退缺口，不代表所有请求都改为浏览器传输或能保证不触发上游风控。

参考图片单文件默认上限为 **20 MiB（20,971,520 字节）**，对齐 2026-10-28
[Dola 官网聊天页脚本](https://sf-flow-web-cdn.ciciai.com/obj/ocean-flow-web-sg/dola_web/static/js/chat.d5d9e59a68.js)
中的 `uploadMultiEntityConf.maxImageSize = 0x1400000`。这是原图接收配置，
不是所有模型的永久上限；Dola 网页后续仍可能压缩图片或执行模型专属校验。

### 后台调整全局并发

在管理后台左侧点击 **全局并发**，输入同时处理的任务数并点击 **保存并生效**。
`1` 为串行，`2` 允许两条任务同时处理，`0` 为不限；浏览器模式可先从 `2` 开始观察资源占用。
提高上限会立即放行等待全局名额的任务；降低上限不取消正在运行的任务，而是等待名额释放。
同账号互斥、API Key 自身并发限制、账号可用性和出口隔离仍然有效，因此设置上限不代表一定能跑满。
开启同出口隔离后，共用一个出口的账号仍按顺序处理；同时提交三条任务会让其余任务等待出口释放。
共享出口占用、上传线路暂停、账号短时失败冷却都会有界等待并显示排队原因；未派号即超时会明确标记
“尚未提交生成”，不会把另一条任务的上传错误记为本任务的生成失败。不同可用出口仍可同时处理任务。
设置保存在任务数据库 `app_settings` 中，重启后保留，优先于环境变量 `DOLA_MAX_CONCURRENCY`；
从未保存时才使用环境变量初始值。此功能适用于当前单进程服务，不支持多进程间共享并发计数。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DOLA_API_KEYS` | 空 | 对外 API Key（逗号分隔）；也可在面板中创建带额度的 Key |
| `DOLA_ADMIN_KEY` | 空 | 管理接口 / 面板密钥 |
| `DOLA_PROXY` | `http://127.0.0.1:7890` | 访问 dola.com 的出口代理；`dola.env` 里写 `DOLA_PROXY=`（留空）= 不走代理直连 |
| `DOLA_PUBLIC_BASE` | `http://127.0.0.1:8000` | 生成 `video_url` 的对外地址 |
| `DOLA_TASK_DEADLINE` | `600` | 首次取得可用账号后的尝试预算（秒）；排队不占用，已受理任务可继续等结果 |
| `DOLA_MAX_QUEUE_WAIT_SECONDS` | `1800` | 排队等待名额/账号的上限（秒），不包含生成等待 |
| `DOLA_PREPARE_TIMEOUT` | `180` | 浏览器单次提交准备上限（秒）；拿到上游会话后交给生成轮询计时 |
| `DOLA_VIDEO_MAX_ATTEMPTS` | `0` | 单任务最多尝试次数，`0` 表示不限、只受总时限约束 |
| `DOLA_MAX_CONCURRENCY` | `0` | 全局并发初始值，0 = 不限；后台保存的值优先 |
| `DOLA_MAX_PENDING_TASKS` | `100` | 待处理任务上限，超过返回 429 |
| `DOLA_REFERENCE_IMAGE_MAX_BYTES` | `20971520` | 每张原始参考图最多 20 MiB；URL 下载和 Base64 输入使用同一限制 |
| `DOLA_REFERENCE_TOTAL_MAX_BYTES` | `62914560` | Base64 参考图落盘时的单次累计限制，60 MiB |
| `DOLA_DAILY_LIMIT` | `4` | 每账号每日额度点数 |
| `DOLA_LIMIT_RESET_TZ` / `DOLA_LIMIT_RESET_HOUR` | `Asia/Tokyo` / `0` | 每日额度重置时区与时刻 |
| `DOUBAO_ALLOW_PAID` | `1` | 豆包号免费额度用完后是否自动确认走付费额度；`0` = 换号 |
| `DOUBAO_DAILY_LIMIT` | `999` | 豆包号每天最多派几条（兜底；真实额度看豆包回复） |
| `DOUBAO_POLL_INTERVAL` | `10` | 豆包出片轮询间隔（秒） |
| `DOLA_IMAGE_TIMEOUT` | `240` | 生图接口一次请求最多等多久（秒），期间失败换号 |
| `DOUBAO_AUTO_NO_WATERMARK` | `1` | 豆包号没关「AI 生成」明水印时自动替它关；`0` = 只在面板提示 |
| `DOLA_PURE_API` | `1` | 优先使用纯 HTTP 出片路径 |
| `DOLA_PURE_FALLBACK_BROWSER` | `1` | 纯 HTTP 失败时回退浏览器路径 |
| `DOLA_COOKIE_RUNTIME` | `pure` | Cookie 账号运行时：`pure`=纯 HTTP；`cloak`=Dola Cookie 账号固定使用 CloakBrowser 独立持久 profile |
| `DOLA_COOKIE_PROFILE_SUBDIR` | `cloak-profile` | 严格 Cookie 模式下每个账号的独立浏览器 profile 子目录 |
| `DOLA_CLOAK_HEADLESS` | `0` | CloakBrowser 是否无头；生产建议配 Xvfb 使用有头模式 |
| `DOLA_CLOAK_GEOIP` | `0` | 是否按代理出口自动解析时区/语言；固定代理时建议手动固定配置 |
| `DOLA_ISOLATE_SHARED_EGRESS` | `0` | 同一出口是否禁止多个账号并发；严格 Cookie 模式默认自动为 `1` |
| `DOLA_MIN_SUBMIT_INTERVAL_SECONDS` | `0` | 同一账号两次提交的最小间隔；严格 Cookie 模式默认自动为 `15` |
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
| `pure_api_gen.py` / `protocol/` | 纯 HTTP 出片协议实现（dola） |
| `protocol/doubao_site.py` / `sites.py` | 豆包站点配置（复用同一份协议代码）、账号站点与模型识别 |
| `doubao_gen.py` | 豆包号出视频：确认 / 付费 / 会员模型话术处理、自动关 AI 水印、无水印下载 |
| `image_gen.py` | 豆包号生图（Seedream） |
| `browser.py` / `video_worker_ui.py` | 浏览器自动化出片路径 |
| `media.py` | 参考图下载、成片元数据 |
| `web/` | 管理面板前端 |
| `web/i18n/` / `i18n_tool.py` | 管理面板多语言（中文 / Tiếng Việt / 双语），见 [`I18N.md`](I18N.md) |
| `extensions/dola30/` | 第三方 Chromium 扩展（30 秒时长、无水印解析） |
| `deploy-cloak-requirements.txt` | 严格 Cookie 模式的可选 CloakBrowser wrapper 依赖 |
| `Dockerfile` / `docker-compose.yml` / `docker/` | Docker 镜像与编排（纯 API，数据在 `/data`） |
| `.github/workflows/` | 推送时自动跑测试（`tests.yml`）；打版本标签时构建并发布镜像（`docker.yml`） |
| `tests/` | pytest 测试 |

运行测试：

```bash
pip install pytest
pytest -q tests
```

## 注意事项

- 账号 cookie 约 60 天过期；登录态失效的账号会被自动标记，需重新导入。导入号池的号**不要在浏览器里点退出登录**（豆包实测退出后 Cookie 立刻失效）。
- **水印**：
  - **dola 视频**：dola 网页默认给的下载地址带水印（`lr=cici_ai`），号池下载的是 dola 返回数据里同一条视频的另一个版本（`lr=unwatermarked`），不对画面做处理。第三方解析接口（`protocol/dola_pure_api.py` 中的 `NOWATERMARK_API_ENDPOINT`）**默认关闭**（`DOLA_PURE_REMOVE_WATERMARK=0`）；设为 `1` 才会启用，届时视频播放信息会经 HTTP 发给第三方。原理与风险详见 [`USAGE.md` 的「去水印说明」](USAGE.md#7-去水印说明)。
  - **豆包视频 / 生图**：「AI 生成」明水印是豆包官方的按号设置（豆包设置 → AI 生成水印管理）。`DOUBAO_AUTO_NO_WATERMARK=1`（默认）时号池用官方设置接口替号关掉，再走官方无水印下载接口；关掉明水印后，内容公开发布时需由发布方自行标注「AI 生成」。
- `accounts/`、`*.db`、`dola.env`、`cookies.txt`、`downloads/` 均已忽略，切勿提交真实凭据。

## 免责声明

本仓库是个人学习与互操作性研究记录，包含对上游网页接口的逆向实现；代码不含任何账号、cookie、密钥或可用凭据。`extensions/dola30/` 为第三方扩展，其授权与使用权归原作者。使用者须自行遵守 dola.com、doubao.com 服务条款及所在地法律法规（包括人工智能生成内容的标识要求），自行评估账号与合规风险。
