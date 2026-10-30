# 视频超分接入说明

dola-pool 出的视频是 720p（Dola 原片 720×1280 / 1280×720，24fps）。视频超分把**已完成**的视频交给外部服务放大到 1080p / 2K / 4K，结果作为第二个地址提供，原片不变。

号池服务器不跑模型（没有 GPU），只负责：取原片地址 → 提交 → 轮询 → 下载或直链交付 → 记录状态和费用。支持两种服务：

| 服务 | 适合 | 费用 |
|---|---|---|
| **fal**（`fal-ai/bytedance-upscaler/upscale/video`，字节官方超分） | 开箱即用，效果稳定，有 AI 生成视频预设 | 1080p 每秒 $0.0072（10 秒约 $0.07），2K ×2、4K ×4，失败不计费 |
| **自建开源模型**（Real-ESRGAN、FlashVSR 等，部署在云端 GPU） | 想要更便宜或自控 | 只付 GPU 时间；按本文的接口约定实现即可接入 |

## 使用

1. 管理面板侧栏 →「视频超分」：选默认服务，填 fal API Key 或自建服务地址，选目标分辨率和交付方式，保存。
2. 任务列表「视频」列或视频管理「操作」列 → 「超分」。fal 会先弹出预计费用确认。
3. 完成后出现 `1080P` 链接、预览和复制链接；失败显示原因，可「重试超分」。

结果地址：`<DOLA_PUBLIC_BASE>/v/<任务ID>-1080p.mp4`（2K / 4K 为 `-2k` / `-4k`）。客户 API `GET /v1/videos/{id}` 的 `video_url_hd` 字段返回同一地址，没有超分时为 `null`。

交付方式：

- **下载到服务器（默认，推荐）**：结果下载到 `downloads/`，地址长期有效；受 `DOLA_DOWNLOAD_MAX_BYTES` 清理规则约束，删除任务时一并删除。
- **直链到服务商**：`/v/...` 302 到服务商的结果地址，不占服务器带宽；服务商的保留期限由对方决定，可能过期。下载失败时也会自动改为直链。

### 配置项

面板里的设置保存在数据库（`app_settings` 表的 `upscale`），优先于下面的环境变量；环境变量只作为首次默认值。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `DOLA_UPSCALE_PROVIDER` | `fal` | 默认服务：`fal` / `open` |
| `DOLA_FAL_KEY` | 空 | fal API Key |
| `DOLA_FAL_UPSCALE_MODEL` | `fal-ai/bytedance-upscaler/upscale/video` | fal 模型 ID |
| `DOLA_UPSCALE_OPEN_URL` | 空 | 自建超分服务地址，如 `https://xxx.modal.run` |
| `DOLA_UPSCALE_OPEN_TOKEN` | 空 | 自建服务的 Bearer 令牌 |
| `DOLA_UPSCALE_DELIVERY` | `download` | `download` / `link` |
| `DOLA_UPSCALE_CONCURRENCY` | `2` | 同时进行的超分任务数 |
| `DOLA_UPSCALE_TIMEOUT` | `1800` | 单条最长等待（秒），超时判失败 |
| `DOLA_UPSCALE_POLL_SECONDS` | `8` | 轮询间隔（秒） |

**`DOLA_PUBLIC_BASE` 必须是公网可访问的地址**：下载交付的原片以 `<PUBLIC_BASE>/videos/<文件>` 交给超分服务，本机地址（127.0.0.1 / localhost）会被拒绝。直链交付的原片用现签的 Dola CDN 地址，不经过本服务。

### 管理接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/settings/upscale` | 读取设置（密钥只返回是否已设置和末 4 位） |
| PATCH | `/api/admin/settings/upscale` | 保存设置；密钥字段不传 = 不改，传空字符串 = 清除 |
| POST | `/api/admin/tasks/{id}/upscale` | 发起超分，body `{"provider": "fal" \| "open", "force": false}`；已超分过需 `force: true` 才重做（会再次计费） |

任务记录里的字段：`upscale_status`（queued / processing / completed / failed）、`upscale_provider`、`upscale_target`、`upscale_url`（服务商结果地址）、`upscale_file`（本地文件名）、`upscale_error`、`upscale_size`、`upscale_seconds`（计费秒数）、`upscale_started_at`、`upscale_finished_at`。服务重启后会从存库的任务状态接着查询，不会重复提交。

## 自建开源超分服务

### 接口约定

号池按下面两个接口调用你的服务（`open_endpoint` 不带末尾斜杠）。有令牌时请求头带 `Authorization: Bearer <token>`。

**提交**

```http
POST {open_endpoint}/v1/upscale
Content-Type: application/json

{"video_url": "https://.../source.mp4", "target_resolution": "1080p", "fps": null}
```

- `video_url`：公网可下载的原片（Dola CDN 签名地址约 6 小时有效，请立即下载）。
- `target_resolution`：`1080p` / `2k` / `4k`，指输出短边 1080 / 1440 / 2160。
- `fps`：`null` 表示保持原帧率。

返回（2xx）：

```json
{"id": "job-123", "status": "queued"}
```

**查询**

```http
GET {open_endpoint}/v1/upscale/{id}
```

返回：

```json
{"status": "queued | processing | completed | failed",
 "video_url": "https://.../result.mp4",
 "error": "失败原因",
 "size": 12345678,
 "duration": 10.08}
```

- `completed` 时必须给 `video_url`（号池要能下载；`link` 交付时直接给客户端用）。
- `failed` 时给 `error`，会显示在面板上。
- 其它状态视为进行中。HTTP 4xx/5xx 视为查询失败，连续 5 次失败判超分失败。

**要求**

- 输出 H.264 MP4（浏览器可直接播放），保留原音轨，帧率不变。
- 结果地址至少保留 24 小时；建议用对象存储签名地址。
- 只接受带令牌的请求；不要把服务开放给任意来源。

### 选哪个模型

| 模型 | 显存 | 速度（参考） | 效果 |
|---|---|---|---|
| **Real-ESRGAN**（`realesr-general-x4v3` / `realesr-animevideov3`） | 2–4 GB | 快，T4 / L4 即可 | 逐帧 GAN，可能有轻微闪烁和过锐，适合作为低成本方案 |
| **FlashVSR**（2025，一步扩散，流式） | 约 11–24 GB | 论文：A100 上 768×1408 约 17 fps；社区在 RTX 5090 上实测约 0.8 fps | 时序稳定，适合 AI 生成视频；需要 A100 级 GPU 才划算 |
| **SeedVR2**（字节开源，3B / 7B） | 3B 约 53 GB | 慢 | 效果最好，成本最高 |

720p → 1080p 是 1.5 倍：用 ×2 模型放大到 1440p 后缩到 1080p（Real-ESRGAN 的 `--outscale 1.5` 也是这么做的）。

### 示例：Modal + Real-ESRGAN

[Modal](https://modal.com) 按秒计费，Starter 计划每月含 $30 免费额度（2026-10 官网），T4 每秒 $0.000164。按 10 秒视频放大加启动开销 30–60 秒估算，每条约 $0.005–0.01（未实测）。下面是实现上述接口的最小骨架，**未在本项目部署验证**，用于说明结构：

```python
# upscaler_modal.py —— modal deploy upscaler_modal.py
import modal, os, subprocess, tempfile, uuid

image = (modal.Image.debian_slim(python_version="3.11")
         .apt_install("ffmpeg")
         .pip_install("torch==2.4.1", "torchvision==0.19.1", "realesrgan==0.3.0",
                      "basicsr==1.4.2", "fastapi[standard]", "requests", "boto3")
         .run_commands("mkdir -p /weights && curl -L -o /weights/realesr-general-x4v3.pth "
                       "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth"))
app = modal.App("dola-upscaler", image=image)
jobs = modal.Dict.from_name("dola-upscale-jobs", create_if_missing=True)
SHORT_SIDE = {"1080p": 1080, "2k": 1440, "4k": 2160}


@app.function(gpu="T4", timeout=1800, secrets=[modal.Secret.from_name("dola-upscaler")])
def run(job_id: str, video_url: str, target: str):
    jobs[job_id] = {"status": "processing"}
    try:
        work = tempfile.mkdtemp()
        src, frames, out = f"{work}/src.mp4", f"{work}/f", f"{work}/out.mp4"
        subprocess.run(["curl", "-sfL", "-o", src, video_url], check=True)
        # 1) 拆帧 2) Real-ESRGAN 放大 3) 缩到目标短边并按原帧率、原音轨合成 H.264
        upscale_frames(src, frames)                       # 用 realesrgan.RealESRGANer 逐帧 ×2
        side = SHORT_SIDE[target]
        subprocess.run(["ffmpeg", "-y", "-framerate", fps_of(src), "-i", f"{frames}/%06d.png",
                        "-i", src, "-map", "0:v", "-map", "1:a?", "-c:a", "copy",
                        "-vf", f"scale='if(lt(iw,ih),{side},-2)':'if(lt(iw,ih),-2,{side})':flags=lanczos",
                        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", out], check=True)
        url = put_to_object_storage(out, f"upscaled/{job_id}.mp4")   # 返回签名地址（≥24 小时）
        jobs[job_id] = {"status": "completed", "video_url": url, "size": os.path.getsize(out)}
    except Exception as exc:
        jobs[job_id] = {"status": "failed", "error": str(exc)[:300]}


@app.function(secrets=[modal.Secret.from_name("dola-upscaler")])
@modal.asgi_app()
def api():
    from fastapi import FastAPI, Header, HTTPException
    web = FastAPI()

    def check(auth):
        if auth != f"Bearer {os.environ['UPSCALE_TOKEN']}":
            raise HTTPException(401, "bad token")

    @web.post("/v1/upscale")
    def submit(body: dict, authorization: str | None = Header(default=None)):
        check(authorization)
        job_id = uuid.uuid4().hex
        jobs[job_id] = {"status": "queued"}
        run.spawn(job_id, body["video_url"], body.get("target_resolution") or "1080p")
        return {"id": job_id, "status": "queued"}

    @web.get("/v1/upscale/{job_id}")
    def status(job_id: str, authorization: str | None = Header(default=None)):
        check(authorization)
        if job_id not in jobs:
            raise HTTPException(404, "unknown job")
        return jobs[job_id]

    return web
```

`upscale_frames` / `fps_of` / `put_to_object_storage` 按 Real-ESRGAN 的 `inference_realesrgan_video.py` 和你的对象存储（S3 / R2 / OSS）实现。部署后把 `https://<workspace>--dola-upscaler-api.modal.run` 填到面板的「服务地址」，令牌填 `UPSCALE_TOKEN`。

要点：

- 权重打进镜像或挂 Volume，避免每次冷启动下载。
- 用 `modal.Dict` / 数据库保存任务状态，提交接口立即返回，GPU 函数异步跑。
- 对 `video_url` 只允许 https，限制下载大小和时长，防止被当成任意抓取代理。
- 在 Modal 设置预算上限（Workspace budget），防止异常调用超支。

### 其它部署方式

- **RunPod / Replicate 等**：同样实现上面两个接口即可，号池不关心后端是什么。
- **自己的电脑（如 RTX 3060 6GB）**：Real-ESRGAN 能跑，但电脑需要常开且有公网地址（可用 Cloudflare Tunnel），只适合自用；FlashVSR / SeedVR2 显存不够。
- **号池服务器本身（4 核 CPU、无 GPU）**：CPU 跑 Real-ESRGAN 一条 10 秒视频需要几十分钟，不建议。

### 联调

```bash
# 1. 直接调你的服务
curl -s -X POST "$URL/v1/upscale" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"video_url":"https://example.com/sample.mp4","target_resolution":"1080p"}'
curl -s "$URL/v1/upscale/<id>" -H "Authorization: Bearer $TOKEN"
# 2. 面板「视频超分」选「自建开源模型」，填地址和令牌，对一条已完成的视频点「超分」
```

号池侧的提交、轮询、失败与续查逻辑由 `tests/test_upscale.py` 覆盖。
