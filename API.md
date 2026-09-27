# stable-dola-pool 接口文档（下游对接版）

> dola.com（字节 Seedance 视频模型）号池 → OpenAI 兼容视频 API 分发服务。
> 本文档面向**下游接入方**：怎么鉴权、怎么提交任务、怎么拿视频、有哪些限制和错误码。
>
> 全部内容按当前线上代码核对：仓库 `origin/main` = `6fa247a`，部署实例 = 本文"基本信息"里的地址。

## 基本信息

| 项 | 值 |
|---|---|
| 服务名 | `stable-dola-pool` |
| 代码版本 | `6fa247a`（`feat(panel): 添加代理改为「静态/动态」二选一…`） |
| **Base URL** | `https://your-domain`（nginx 反代到本服务端口） |
| 交互式文档 | `https://your-domain/docs`（FastAPI Swagger，实时以代码为准） |
| OpenAPI Schema | `https://your-domain/openapi.json` |
| 管理面板（自带 Web UI） | `https://your-domain/` |
| 数据库 | `tasks.db`（任务）、`pool_usage.db`（账号/代理/额度/管理员） |

---

## 0. 三分钟接入

```bash
BASE=https://your-domain
KEY=<你的 API Key>            # 在管理面板「API 密钥」里创建；环境变量里的 Key 见附录 B

# 1) 提交一条 15 秒视频
curl -s -X POST "$BASE/v1/videos/generations" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.0","prompt":"一只猫在窗台上看雨","size":"720x1280","duration":15}'
# -> {"id":"video_xxx","status":"queued","model":"seedance-2.0","prompt":"..."}

# 2) 轮询（建议 3~5 秒一次）
curl -s "$BASE/v1/videos/video_xxx" -H "Authorization: Bearer $KEY"
# -> queued -> processing -> completed（带 video_url）

# 3) 拿视频：video_url 直接下载，或用内容端点
curl -sO "$BASE/videos/<文件名>.mp4"
curl -s "$BASE/v1/videos/video_xxx/content" -H "Authorization: Bearer $KEY" -o out.mp4
```

要点：**提交是异步的**（立刻返回 `queued`），出片要轮询；`video_url` 是本服务转存后的稳定地址，不是 dola 的临时 CDN 链接。

---

## 1. 鉴权

服务里有两套**完全独立**的鉴权，不要混用。

### 1.1 客户调用（下游业务用）

```http
Authorization: Bearer <API_KEY>
```

Key 有两个来源，**先查环境变量、再查数据库**（`server.py::_auth`）：

| 来源 | 说明 | 额度限制 |
|---|---|---|
| 环境变量 `DOLA_API_KEYS`（逗号分隔，可配多个） | 部署时写死的 Key | `daily_limit=0`、`concurrency_limit=0`、允许全部时长 —— **不受限** |
| 管理面板「API 密钥」创建的客户 Key | 每个 Key 可单独设每日任务数、并发上限、允许时长、过期时间 | 按下表生效 |

面板 Key 的字段（`POST /api/admin/keys`）：

| 字段 | 语义 | `0` / 空 |
|---|---|---|
| `name` | 客户名（只用于统计与显示） | — |
| `daily_limit` | 每自然日最多受理多少个任务 | `0` = 不限 |
| `concurrency_limit` | 该 Key 同时进行中的任务数上限 | `0` = 不限（受服务端全局并发约束） |
| `allowed_durations` | 该 Key 允许的时长列表，如 `[5,10]` | 默认全允许 |
| `expires_at` | 过期时间（Unix 秒） | 不填 = 永不过期 |

> **开发模式**：当 `DOLA_API_KEYS` 为空**且**数据库里没有任何启用的 Key 时，服务**完全不鉴权**（任何调用都放行）。当前线上已经配了 Key，不会进这个模式。

鉴权失败（`401`）：

| 响应体 | 触发条件 |
|---|---|
| `{"detail":"missing bearer token"}` | 没带 `Authorization`，或 `Bearer ` 后面为空 |
| `{"detail":"invalid api key"}` | Key 不存在 / 已禁用 / 已过期 |

### 1.2 管理接口（`/api/admin/*`，你自己运维用）

管理接口用**会话令牌**（不是 API Key）：

```http
POST /api/admin/login
Content-Type: application/json

{"username":"admin","password":"<面板密码>"}
```

```json
{"ok":true,"auth_required":true,"token":"<SESSION_TOKEN>","username":"admin","role":"owner"}
```

之后所有管理请求带 `X-Admin-Key: <SESSION_TOKEN>`。

| 响应体 | 触发条件 |
|---|---|
| `{"detail":"用户名或密码错误"}` | 账号或密码不对 |
| `{"detail":"请先登录"}` | 没带 `X-Admin-Key`（**注意：浏览器标签页里的令牌是内存态，重启服务后旧标签会一直发空令牌**） |
| `{"detail":"登录已过期，请重新登录"}` | 令牌失效（默认有效期 7 天，服务重启即失效） |

> 会话存在进程内存里，**服务一重启就要重新登录**；前端令牌同时存 `localStorage`，但页面内变量只在加载时读一次 —— 遇到 401 请强刷页面重新登录。

---

## 2. 能力、时长与计费口径

### 2.1 模型 × 时长 × 扣点

| 模型 | 支持时长 | 每次出片扣点 | 一个号一天（默认 4 点）能出 |
|---|---|---|---|
| `seedance-2.5` | 5 / 10 / 30 秒 | 2 点 | 2 条 |
| `seedance-2.0` | 5 / 10 / 15 秒 | 3 点 | 1 条（剩 1 点不够第二条） |

- 时长字段优先级：`duration` → `seconds` → `duration_seconds` → 默认 `10`。
  （new-api 的 sora/vinted 插件会把时长改写成 `seconds`，本服务已兼容。）
- 请求里**显式写了 2.0 / 2.5 版本**，就只允许该版本的时长，不会"悄悄换型号"。
- 写不出可识别的版本（如 `model: "seedance"`）时，按请求时长自动挑一个支持的版本。

### 2.2 尺寸 → 画幅

| 传入 `size` | 归一化 `ratio` |
|---|---|
| `1280x720`、`1920x1080` | `16:9` |
| `720x1280`、`1080x1920` | `9:16` |
| `1024x1024` | `1:1` |
| `1440x1080` | `4:3` |
| `1080x1440` | `3:4` |

也可以用 `ratio` 直接传 `16:9 / 9:16 / 1:1 / 4:3 / 3:4`；都不带就用上游默认。

### 2.3 服务级限制（当前线上值）

| 项 | 值 | 说明 |
|---|---|---|
| 号池账号数 | 见 `GET /health` | 空池直接 `503` |
| 服务端全局并发 | `3`（`DOLA_MAX_CONCURRENCY`） | 超过则在 `queued` 排队，不报错 |
| 待处理任务上限 | `100`（`DOLA_MAX_PENDING_TASKS`） | 满了返回 `429` |
| 单任务出片截止 | 纯 API 路径 `900` 秒（`DOLA_PURE_TIMEOUT`）／浏览器路径 `300` 秒（`DOLA_VIDEO_TIMEOUT`）；服务重启后续跑的任务，30 秒档放宽到 `1800` 秒 | 超时判 `failed` |
| 失败重试 | 不限次数，总时限 `600` 秒（`DOLA_TASK_DEADLINE` / `DOLA_VIDEO_MAX_ATTEMPTS`） | 失败自动换号，一轮试完等冷却后再来一轮；超时判 `failed`；已拿到会话的轮询超时不换号 |
| 账号每日额度重置 | `Asia/Tokyo` 每天 `0:00` | `DOLA_LIMIT_RESET_TZ` / `DOLA_LIMIT_RESET_HOUR` |

> 排队是**正常现象**：号池越大吞吐越高。提交后拿到 `queued` 就一直轮询即可，不要因为排队就重试提交（会重复扣额度）。

---

## 3. 客户接口

### 3.1 创建视频任务（OpenAI 风格）

```http
POST /v1/videos/generations
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `prompt` | string | ✅ | 提示词，长度 ≥ 1 |
| `model` | string | | 默认 `seedance-2.0` |
| `duration` | int | | 5~30；实际合法性由模型决定 |
| `seconds` | int | | `duration` 的别名（new-api 插件用） |
| `duration_seconds` | int | | 同上 |
| `size` | string | | 见 2.2 映射表 |
| `ratio` | string | | 见 2.2 |
| `reference_images` | string[] | | 参考图 URL / data URI，见第 7 节 |
| `image_url` / `image` / `input_image` / `input_images` | 任意 | | 参考图的兼容写法 |

多余字段会被忽略（不会 422），方便各种网关直接转发。

响应 `200`：

```json
{"id":"video_5f3c…","status":"queued","model":"seedance-2.0","prompt":"一只猫在窗台上看雨"}
```

示例：

```bash
curl -s -X POST https://your-domain/v1/videos/generations \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{
        "model":"seedance-2.5",
        "prompt":"无人机穿过东京雨夜街道，电影感",
        "size":"1920x1080",
        "duration":30,
        "reference_images":["https://example.com/ref.jpg"]
      }'
```

### 3.2 创建视频任务（等价别名，返回 202）

```http
POST /v1/videos      # 与 3.1 完全同逻辑，只是 HTTP 状态码固定 202 Accepted
```

给「Sora/Vinted 风格」的任务插件用：请求体、响应体、错误码都与 3.1 一致，只有成功状态码是 `202`。

### 3.3 查询任务

```http
GET /v1/videos/{task_id}
Authorization: Bearer <API_KEY>
```

```json
{
  "id": "video_5f3c…",
  "status": "completed",
  "model": "seedance-2.5",
  "prompt": "无人机穿过东京雨夜街道，电影感",
  "video_url": "https://your-domain/videos/video_5f3c….mp4",
  "error": null
}
```

| 字段 | 说明 |
|---|---|
| `status` | `queued` / `processing` / `completed` / `failed` |
| `video_url` | 仅 `completed` 时有值；指向**本服务转存**的静态文件（稳定地址，不是 dola 临时 CDN） |
| `error` | 仅 `failed` 时有值，是最末一次失败的原因（最多 500 字） |

**只能查到自己 Key 创建的任务**；别人的 task_id 一律 `404 task not found`。

### 3.4 取视频内容（new-api 任务插件兼容）

```http
GET /v1/videos/{task_id}/content
Authorization: Bearer <API_KEY>
```

- `200` → 二进制流（`Content-Type: video/mp4`），服务端 120 秒超时内从转存地址读取。
- `409 {"detail":"video not ready"}` → 还没 completed。
- `502 {"detail":"failed to fetch video from upstream"}` → 转存文件读取失败。
- `404 {"detail":"task not found"}` → 任务不存在或不属于该 Key。

### 3.5 模型列表

```http
GET /v1/models        # 无需鉴权
```

```json
{"object":"list","data":[
  {"id":"seedance-2.0","object":"model","owned_by":"dola-pool","created":0},
  {"id":"seedance-2.5","object":"model","owned_by":"dola-pool","created":0}
]}
```

### 3.6 健康检查 / 容量探测

```http
GET /health           # 无需鉴权
```

```json
{
  "ok": true,
  "accounts": [{"name":"acc1","status":"healthy","…":"…"}],
  "pool": {
    "account_count": 1, "healthy_count": 1, "standby_count": 0,
    "checking_count": 0, "unsigned_count": 0, "cooldown_count": 0,
    "expired_count": 0, "open_accounts": 0, "inflight": 0, "jobs_today": 0,
    "isolation": {"ok": true, "account_proxy_enabled": true, "…":"…"}
  },
  "available": true,
  "pending_tasks": 0,
  "max_pending_tasks": 100
}
```

下游可以用它做**容量探测**：`available=false` 或 `pool.healthy_count=0` 时先别提交（虽然提交也会得到 429/503，但一次探测比多次失败请求便宜）。

### 3.7 火山 Ark 任务式协议（画布工具兼容）

下列 4 个前缀**等价**，都注册了同样的逻辑（画布/Ark 类工具任选其一即可）：

```
/v1/video/contents/generations/tasks
/api/v3/contents/generations/tasks
/v1/contents/generations/tasks
/seedance/v3/contents/generations/tasks
```

创建：

```http
POST {前缀}
Authorization: Bearer <API_KEY>

{
  "model": "seedance-2.5",
  "content": [
    {"type":"text","text":"一只柴犬在雪地里奔跑"},
    {"type":"image_url","image_url":{"url":"https://example.com/dog.jpg"}}
  ],
  "duration": 10,
  "ratio": "16:9"
}
```

响应：`{"id":"video_xxx","status":"queued"}`

查询：

```http
GET {前缀}/{task_id}
```

```json
{
  "id": "video_5f3c…",
  "model": "seedance-2.5",
  "status": "succeeded",
  "created_at": 1789804800,
  "updated_at": 1789804923,
  "content": {"video_url": "https://your-domain/videos/video_5f3c….mp4"}
}
```

状态映射：`queued→queued`、`processing→running`、`completed→succeeded`、`failed→failed`；失败时额外带 `error`。

### 3.8 静态出片文件

```http
GET /videos/<文件名>.mp4      # 无需鉴权
```

即 `video_url` 指向的地址，由 FastAPI 静态目录 `downloads/` 提供。

---

## 4. 任务状态机与轮询建议

```
提交 ──► queued ──► processing ──► completed   (video_url 可用)
               └───────────────► failed       (error 给出原因)
```

- `queued`：已受理、等并发额度或等账号。
- `processing`：已经开始出片（内部可能因失败在换号重试）。
- `completed`：`video_url` 就绪。
- `failed`：`error` 字段给出原因（超时、上游失败、账号全被限流等）。

| 场景 | 建议 |
|---|---|
| 轮询间隔 | 3~5 秒；不要 < 1 秒（无收益） |
| 轮询上限 | 一般档建议按 15 分钟设上限（纯 API 路径出片截止 900 秒）；30 秒档按 30 分钟 |
| 客户端超时 | 单次查询 10 秒足够（接口是立即返回的） |
| 不要做的事 | 失败就立刻重新提交同一个 prompt（会重复占用账号额度）；应等 `failed` 后再决定重试 |

---

## 5. 错误码总表

### 客户接口

| HTTP | `detail` 文案（原文） | 含义与处理 |
|---|---|---|
| `400` | `invalid json body` | 请求体不是合法 JSON |
| `401` | `missing bearer token` | 没带 Key |
| `401` | `invalid api key` | Key 不对/被禁用/过期 |
| `404` | `task not found` | 任务不存在，或不属于该 Key |
| `409` | `video not ready` | 视频还没出完（仅内容端点） |
| `422` | `seedance-2.0 仅支持 5/10/15 秒视频（收到 30 秒）` | 模型与时长不匹配 |
| `422` | `不支持的模型/时长组合：模型='x'，时长=7秒（seedance-2.5 仅支持 5/10/30 秒，seedance-2.0 仅支持 5/10/15 秒）` | 同上，模型名无法识别时 |
| `422` | `当前 API Key 不允许生成 30 秒视频` | Key 的 `allowed_durations` 限制 |
| `422` | `参考图片最多 30 张` / `参考文件不是有效图片` / `参考图片只支持 http/https 公网 URL` / `参考图片 URL 指向内网或保留地址` / `参考图片超过单文件大小限制` | 参考图校验失败 |
| `422` | pydantic 校验错误（如缺 `prompt`） | 请求体字段不合法 |
| `429` | `账号限流：所有已开启调度的账号均已达到 Dola 每日视频上限，请明天再试` | 号池当天额度用尽，明天（东京时间 0 点）恢复 |
| `429` | `积分不足：所有已开启调度的账号都没有足够积分，请等待额度刷新` | 号池积分不足 |
| `429` | `API Key 今日额度已用完（N 个任务）` | 该 Key 的 `daily_limit` 用完 |
| `429` | `待处理任务已达到服务上限（100）` | 服务端队列满，稍后重试 |
| `502` | `failed to fetch video from upstream` | 内容端点取流转存文件失败 |
| `503` | `no account in pool` | 号池没有任何账号（运维告警项） |

### 管理接口

| HTTP | 文案 | 说明 |
|---|---|---|
| `401` | `用户名或密码错误` | 登录失败 |
| `401` | `请先登录` | 没带 `X-Admin-Key` |
| `401` | `登录已过期，请重新登录` | 令牌失效（重启后必现，需重新登录） |
| `409` | `账号正在出片，稍后再验证` / `账号正在出片，不能删除` | 账号忙 |
| `409` | `验证失败（服务器异常）: xxx` | 验证流程内部异常（**含 Node.js 缺失这类环境问题**） |
| `409` | `代理名称已存在: xxx` | 代理重名 |
| `400` | `动态IP 代理要么填提取链接，要么粘贴节点列表（host:port:user:pass 一行一个）` | 动态代理创建参数不足 |

---

## 6. 完整对接示例

### 6.1 curl 全流程

```bash
BASE=https://your-domain
KEY=sk-your-key

TASK=$(curl -s -X POST "$BASE/v1/videos/generations" \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.0","prompt":"赛博朋克城市夜景","size":"1280x720","duration":10}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

while :; do
  R=$(curl -s "$BASE/v1/videos/$TASK" -H "Authorization: Bearer $KEY")
  S=$(echo "$R" | python -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  case "$S" in
    completed) echo "$R"; curl -s "$BASE/v1/videos/$TASK/content" -H "Authorization: Bearer $KEY" -o out.mp4; break;;
    failed)    echo "$R"; break;;
    *)         sleep 4;;
  esac
done
```

### 6.2 Python（同步轮询，带 429 退避）

```python
import time, requests

BASE = "https://your-domain"
KEY  = "sk-your-key"
H    = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}

def create(prompt, duration=10, model="seedance-2.0", size="720x1280", refs=None, retries=3):
    body = {"model": model, "prompt": prompt, "duration": duration, "size": size}
    if refs:
        body["reference_images"] = refs
    for i in range(retries):
        r = requests.post(f"{BASE}/v1/videos/generations", headers=H, json=body, timeout=30)
        if r.status_code in (200, 202):
            return r.json()["id"]
        if r.status_code == 429:                    # 队列满/额度用尽：退避后再试
            time.sleep(5 * (i + 1)); continue
        r.raise_for_status()                        # 4xx 参数问题直接抛出
    raise RuntimeError("create failed after retries")

def wait(task_id, interval=4, timeout=1800):
    end = time.time() + timeout
    while time.time() < end:
        r = requests.get(f"{BASE}/v1/videos/{task_id}", headers=H, timeout=15)
        r.raise_for_status()
        d = r.json()
        if d["status"] == "completed":
            return d["video_url"]
        if d["status"] == "failed":
            raise RuntimeError(f"生成失败: {d.get('error')}")
        time.sleep(interval)
    raise TimeoutError("轮询超时")

url = wait(create("一只柴犬在雪地里奔跑，30fps 电影感", duration=10))
print("视频地址:", url)
open("out.mp4", "wb").write(requests.get(url, timeout=120).content)
```

### 6.3 接进 new-api / OpenAI 兼容网关

| 项 | 填法 |
|---|---|
| 渠道类型 | OpenAI 兼容 / Sora 任务型插件 |
| Base URL | `https://your-domain` |
| 模型 | `seedance-2.0`、`seedance-2.5`（`GET /v1/models` 会自动返回） |
| 提交路径 | `POST /v1/videos/generations`（或插件要求的 `POST /v1/videos`） |
| 查询路径 | `GET /v1/videos/{id}`，取文件 `GET /v1/videos/{id}/content` |
| 时长字段 | 插件若把时长写成 `seconds`，本服务同样识别（不会掉回默认 10 秒） |
| Ark 任务式插件 | 用 3.7 的四个前缀之一 |

---

## 7. 参考图输入规范

支持字段（任选，可同时给，服务端会去重合并）：`reference_images[]`、`image_url`、`image`、`input_image`、`input_images`；另外会对请求体做深度扫描，自动识别 JSON 里出现的图片 URL。

| 约束 | 值 |
|---|---|
| 最多张数 | 30（`DOLA_REFERENCE_IMAGE_MAX_COUNT`） |
| 单张大小 | ≤ 15 MB（`DOLA_REFERENCE_IMAGE_MAX_BYTES`） |
| 形式 | `http(s)://` 公网 URL，或 `data:image/...;base64,...` |
| 禁止 | 内网/环回/保留地址、URL 里带用户名密码、非图片内容 |
| 超时 | 拉取 60 秒（`DOLA_REFERENCE_DOWNLOAD_TIMEOUT`） |

失败一律 `422`，`detail` 里会写清是哪一条不合法。

---

## 8. FAQ

**Q：`video_url` 会过期吗？**
不会 —— 出片后服务会把文件转存到本地 `downloads/`，返回的是 `https://your-domain/videos/xxx.mp4`（`DOLA_PUBLIC_BASE` 拼接）。注意：管理端删除该任务时会连同文件一起删（`DELETE /api/admin/videos/{id}`）。

**Q：为什么提交后一直是 `queued`？**
服务端全局并发只有 3，账号每日额度也有限，任务会排队；只要 `status` 在变或队列在减少就是正常的。用 `GET /health` 的 `pending_tasks` 观察积压。

**Q：为什么同样的请求有时 429？**
三种情况：号池当天额度全用尽、号池积分不足、队列满。前两种等东京时间 0 点恢复或加号；第三种过一会儿重试。文案会明确告诉你是哪一种。

**Q：能不能指定用哪个账号出片？**
客户接口不能。账号选择由号池内部按健康度/额度/代理隔离自动调度（管理端有 `route` 预览接口）。

**Q：30 秒有什么额外注意？**
`seedance-2.5` 才支持 30 秒；默认「不拼接」，也就是必须一次拿到原生 30 秒成片，失败会换号重试，所以耗时会明显更长（超时放宽到 1800 秒）。

**Q：任务失败会退额度吗？**
账号侧的每日点数是按"实际出片成功"记账的（`MODEL_DURATION_COSTS`），失败重试会换号；但 **API Key 的 `daily_limit` 是按下发任务数计**的（`store.create` 时即计数，与结果无关），所以别用同一个 Key 反复试错。

**Q：我要自己接一个调用方，Key 从哪来？**
管理面板 → 「API 密钥」标签页创建，可设每日额度/并发/允许时长/过期时间；也可以让运维加到 `DOLA_API_KEYS` 环境变量里（这类 Key 不受限，慎用）。

---

## 附录 A：管理接口一览

所有路径都需要 `X-Admin-Key: <SESSION_TOKEN>`（除 `/api/admin/login`）。

### A.1 登录与管理员

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/admin/login` | 登录取令牌 |
| GET | `/api/admin/users` | 管理员列表 |
| POST | `/api/admin/users` | 新建管理员（`username`/`password`/`role`） |
| PATCH | `/api/admin/users/{username}` | 改密码/启停 |
| DELETE | `/api/admin/users/{username}` | 删除管理员（`owner` 不可删） |

### A.2 账号管理

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/admin/accounts` | 账号列表（状态、额度、绑定代理、出口 IP） |
| POST | `/api/admin/accounts` | 新增账号（`202` 异步；带 `cookies` 即 cookie 导入，`cookie_skip_verify` 控制是否在线校验） |
| PATCH | `/api/admin/accounts/{name}` | 改备注/调度开关等 |
| DELETE | `/api/admin/accounts/{name}` | 删除账号 |
| POST | `/api/admin/accounts/{name}/verify` | 验证登录态（cookie 号走纯 API 探活，需服务器装 Node.js） |
| POST | `/api/admin/accounts/batch-verify` | 批量验证（返回 `job_id`） |
| GET | `/api/admin/batch/{job_id}` | 批量任务进度 |
| POST | `/api/admin/accounts/{name}/test-generate` | 用指定账号跑一条真实出片 |
| GET | `/api/admin/test-gen/{task_id}` | 测试出片进度 |
| POST | `/api/admin/accounts/import-cookies` | 多行 Cookie 头文本批量导入（按 `sessionid` 去重，自动命名 `accN`） |
| POST | `/api/admin/accounts/import-accounts` | 多行「邮箱/密码/TOTP」批量登录导入 |
| POST | `/api/admin/accounts/reset-quotas` | 重置全部账号当日额度 |
| POST | `/api/admin/accounts/{name}/prefer` | 设优先账号 |
| POST | `/api/admin/accounts/{name}/weight` | 设调度权重 |
| GET | `/api/admin/jobs` | 进行中的加号任务 |
| GET | `/api/admin/route` | 预览下一个任务会落到哪个账号/出口 |

### A.3 代理管理

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/admin/proxies` | 代理列表（静态/动态、模式、绑定账号数） |
| POST | `/api/admin/proxies` | 新建（`mode=static` 要 `host/port`；`mode=extract` 可只给 `extract_url` 或 `nodes_text`） |
| PATCH | `/api/admin/proxies/{id}` | 改代理 |
| DELETE | `/api/admin/proxies/{id}` | 删代理 |
| POST | `/api/admin/proxies/{id}/probe` | 探测代理出口 |
| GET | `/api/admin/proxies/{id}/nodes` | 节点池（节点、存活、出口 IP、过期时间） |
| POST | `/api/admin/proxies/{id}/nodes` | 手工粘贴/追加节点（`text` 一行一个 `host:port:user:pass`） |
| POST | `/api/admin/proxies/{id}/refresh-nodes` | 调提取链接补节点 |
| POST | `/api/admin/proxies/{id}/prune-nodes` | 清理死节点 |
| POST | `/api/admin/proxies/{id}/rotate` | 给该代理下所有号换出口 |
| POST | `/api/admin/accounts/{name}/proxy` | 给账号绑定/解绑代理 |
| POST | `/api/admin/accounts/{name}/rotate-ip` | 给该号换出口 IP |
| POST | `/api/admin/accounts/{name}/probe-ip` | 探测该号当前出口 IP 与归属 |
| POST | `/api/admin/accounts/batch-proxy` | 批量改代理 |
| POST | `/api/admin/accounts/rebalance-proxies` | 自动重分配（一节点一号） |
| POST | `/api/admin/accounts/bind-proxies` | 批量绑定 |

### A.4 客户 API Key

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/admin/keys` | Key 列表 + 今日用量（`today_total/completed/failed/active/queued`） |
| POST | `/api/admin/keys` | 新建 Key（`name`/`daily_limit`/`concurrency_limit`/`allowed_durations`/`expires_at`） |
| PATCH | `/api/admin/keys/{key}` | 改 Key（含启停 `enabled`） |
| DELETE | `/api/admin/keys/{key}` | 删 Key |

### A.5 任务、视频、统计

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/admin/tasks?limit=50` | 最近任务（上限 200） |
| GET | `/api/admin/videos` | 出片列表 |
| DELETE | `/api/admin/videos/{task_id}` | 删任务记录并删本地文件 |
| GET | `/api/admin/stats` | 总览统计（按账号/按 Key 用量） |
| GET | `/api/admin/stress` | 压测状态 |
| POST | `/api/admin/stress` | 启动压测 |

---

## 附录 B：环境变量（当前线上实际值）

由宝塔「Python项目 → 设置 → 环境变量」注入（唯一事实来源）。

### 服务与对外

| 变量 | 当前值 | 说明 |
|---|---|---|
| `DOLA_HOST` / `DOLA_PORT` | `0.0.0.0` / `8000` | 监听地址（只对 nginx 暴露） |
| `DOLA_PUBLIC_BASE` | `https://your-domain` | `video_url` 拼接基址，必须客户端可达 |
| `DOLA_API_KEYS` | `sk-dola-…`（1 个） | 环境变量 Key，逗号分隔；**不受额度限制** |
| `DOLA_ADMIN_KEY` | 已设置但**不生效** | 历史遗留字段：`config.ADMIN_KEY` 在当前代码里没有任何引用，管理接口只认登录会话令牌（见 1.2）——别指望用它当静态管理密钥 |

### 并发与队列

| 变量 | 当前值 |
|---|---|
| `DOLA_MAX_CONCURRENCY` | `3` |
| `DOLA_MAX_PENDING_TASKS` | `100` |
| `DOLA_VIDEO_TIMEOUT` | `300` |

### 号池与额度

| 变量 | 当前值 |
|---|---|
| `DOLA_DAILY_LIMIT` | `4`（每号每日点数） |
| `DOLA_ALLOW_30S_PAIR` | `0`（不拼接 30 秒） |
| `DOLA_LIMIT_RESET_TZ` / `DOLA_LIMIT_RESET_HOUR` | `Asia/Tokyo` / `0` |
| `DOLA_COOKIES_FILE` | `/www/wwwroot/stable-dola-pool/cookies.txt` |

### 代理与出口

| 变量 | 当前值 | 说明 |
|---|---|---|
| `DOLA_PROXY` | 空 | 全局兜底代理；空 = 直连（号级代理优先） |
| `DOLA_ISOLATE_SHARED_EGRESS` | `0` | 是否强制每号独立出口 |
| `DOLA_EXTRACT_TIMEOUT` / `DOLA_EXTRACT_NODE_TTL` | 默认 `30` / `300` | 节点提取与存活时长 |
| `DOLA_DYNAMIC_ROTATE_MIN_INTERVAL` / `DOLA_DYNAMIC_STICKY_TTL` | 默认 `60` / `0` | 换 IP 间隔 / session 有效期 |

### 上游协议与浏览器

| 变量 | 当前值 | 说明 |
|---|---|---|
| `DOLA_PURE_API` | `1` | cookie 号走纯 API（**需要 Node.js**，见附录 C） |
| `DOLA_PURE_FALLBACK_BROWSER` | `1` | 纯 API 失败是否回落浏览器 |
| `DOLA_PURE_REGION` / `DOLA_PURE_PC_VERSION` | `JP` / `3.33.11` | 上游区域与客户端版本 |
| `DOLA_PURE_REMOVE_WATERMARK` | `1` | 无水印解析 |
| `DOLA_HEADLESS` | `1` | 浏览器无头；有头路径由 `xvfb-run` 提供显示 |
| `DOLA_EXTENSION_DIR` / `DOLA_EXTENSION_ENABLED` | `…/extensions/dola30` / `1` | 30 秒/无水印扩展 |
| `PLAYWRIGHT_BROWSERS_PATH` | `/www/wwwroot/stable-dola-pool/.browsers` | Chromium（patchright）内核位置 |

### 参考图与存储

| 变量 | 当前值 |
|---|---|
| `DOLA_REFERENCE_IMAGE_MAX_COUNT` | `30` |
| `DOLA_REFERENCE_IMAGE_MAX_BYTES` | `15728640`（15 MB） |
| `DOLA_REFERENCE_DOWNLOAD_TIMEOUT` | `60` |
| `DOLA_DB_PATH` / `DOLA_POOL_DB_PATH` | `tasks.db` / `pool_usage.db`（项目目录内） |
| `DOLA_DOWNLOAD_DIR` | `downloads` |

---

## 附录 C：运维速查

```bash
# —— 服务状态（宝塔面板里也可以点按钮）——
sudo -i                                    # 全部以 root 执行
cd /www/wwwroot/stable-dola-pool
ss -lntp | grep 8000                       # 端口在听 = 进程在跑
curl -s http://127.0.0.1:8000/health       # 号池状态

# —— 日志 ——
tail -f /www/wwwlogs/python/stable-dola-pool/error.log     # 应用日志
tail -f /www/wwwlogs/stable-dola-pool.log                  # nginx 访问日志

# —— 重启（推荐用面板：网站 → Python项目 → stable-dola-pool → 重启）——
/www/server/python_project/vhost/scripts/stable-dola-pool_cmd.sh   # 启动脚本
# 面板守护任务每 120 秒检查一次，异常退出会自动拉起

# —— 系统依赖（重装/迁移时别漏）——
dnf install -y nodejs              # 纯 API 签名器需要 node（缺了会导致"验证失败/出片失败"）
dnf install -y xorg-x11-server-Xvfb python3-devel nss atk cups-libs libdrm \
               libXcomposite libXdamage libXrandr mesa-libgbm alsa-lib pango gtk3

# —— 号池 cookie 变更后 ——
vi /www/wwwroot/stable-dola-pool/cookies.txt   # 一行一个
# 或面板「账号管理 → 批量添加 → Cookie 头文本」导入
```

**排障顺序**（照着这条链走，能覆盖绝大多数问题）：

1. `GET /health` → `accounts` 数量与状态（`healthy/expired/cooldown/unsigned`）。
2. `POST /api/admin/accounts/{name}/probe-ip` → 出口是不是你绑的代理（还是回落直连）。
3. `POST /api/admin/accounts/{name}/verify` → 登录态；失败时看服务器有没有 `node`（纯 API 签名依赖）。
4. `GET /api/admin/proxies/{id}/nodes` → 动态代理节点池是否为空/节点是否被判死。

**已知坑（都在代码里，改号池时注意）**：

- 删除账号**不会**清理它在 `account_proxy` / `proxy_sessions` 里的绑定与节点占用，残留会占着节点导致其它号"明明绑了代理却走直连"（需要手工清或修 `delete_account`）。
- `verify_account` 会把失败原因丢掉（面板只显示"验证失败"），排查要么看日志要么用上面的探针脚本。
- 管理会话在进程内存里，重启后所有已登录的浏览器标签都要重新登录。

---

## 附录 D：与旧版文档的差异

本文档取代了此前那份旧版接口文档 —— 它写的是**上一台服务器**（`110.42.42.218:8000`、systemd、`/opt/stable-dola-pool`、Python 3.12），已不适用。主要差异：

| 项 | 旧（API.md） | 现（本文档） |
|---|---|---|
| 地址 | `http://110.42.42.218:8000`（直连 8000） | `https://your-domain`（nginx 80 反代；8000 不对公网开放） |
| 运行环境 | Python 3.12 venv | Python 3.11.6 venv + **Node.js 20（新增依赖，纯 API 签名用）** |
| 日志 | `/var/log/dola-pool*.log` | `/www/wwwlogs/python/stable-dola-pool/error.log` |
| 版本 | `0.4.0` | `6fa247a`（动态代理支持「直接粘贴节点」） |
