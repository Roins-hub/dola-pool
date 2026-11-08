<p align="center"><img src="web/logo.svg" width="96" height="96" alt="dola-pool logo"></p>
<h1 align="center">dola-pool</h1>
<p align="center">Pool a set of dola.com / Doubao accounts and serve an OpenAI-compatible video generation and image API</p>
<p align="center">
  🌐 <a href="README.md">中文</a> | <b>English</b>
  &nbsp;·&nbsp; <a href="https://github.com/Roins-hub/dola-pool/releases/latest">Download</a>
  &nbsp;·&nbsp; <a href="USAGE.md">User guide (Chinese)</a>
  &nbsp;·&nbsp; <a href="API.md">API reference (Chinese)</a>
</p>

<img align="right" width="190" alt="Community group QR code" src="https://github.com/user-attachments/assets/39207d3c-955f-49ec-adbc-8db471f23b79" />

[dola.com](https://www.dola.com) is the international edition of ByteDance's Seedance video model; [Doubao](https://www.doubao.com) is the Chinese edition. dola-pool puts accounts from both sites into one pool and exposes an **OpenAI-style API**. It also speaks the Volcengine Ark task protocol, so gateways such as [new-api](https://github.com/QuantumNous/new-api) can use it directly.

- **Pool scheduling**: picks accounts, tracks quota, cools down risk-controlled accounts, switches accounts on failure, and resets quota daily
- **Both sites in one pool**: dola accounts serve `seedance-2.0-fast` / `seedance-2.5`; Doubao accounts serve `doubao-seedance-*` video and `doubao-seedream-*` images. Requests are routed by model name and never cross over
- **Two generation paths**: pure HTTP protocol by default, with browser automation as a fallback (dola only)
- **Public endpoints**: `/v1/videos`, `/v1/images/generations` and `/v1/images/edits`, plus the Ark-style `/api/v3/contents/generations/tasks`
- **Admin panel**: accounts, proxies, API keys, tasks, videos, stress tests and one-click updates; the UI is in Chinese and Vietnamese
- **Handles dola's follow-up questions**: replies automatically when dola asks about aspect ratio, duration, or says the video was already generated, with an optional AI responder
- **Upscaling by requested resolution**: 1080p / 2K / 4K

**Community**: scan the QR code on the right to join the group.

**New here?** Start with [`USAGE.md`](USAGE.md) (Chinese): it walks from zero to your first video and explains every button in the panel. Read [`COOKIE.md`](COOKIE.md) before adding accounts; it shows how to export account cookies.

<br clear="right">

## Quick start

### One-command install (Linux, recommended)

Ubuntu 20.04+ / Debian 11+. One command installs it and registers a system service:

```bash
# Reach it at IP:8002
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo bash

# With a domain and automatic HTTPS (point the domain at this server first)
curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo DOLA_DOMAIN=video.example.com bash
```

When it finishes, the installer prints:
- the panel address;
- the default login `admin` / `admin123` (change it right away);
- a randomly generated API key.

After that, update from the panel's **Version** button. Optional settings are documented at the top of [`deploy/install.sh`](deploy/install.sh).

### Docker

```bash
mkdir dola-pool && cd dola-pool
curl -fsSLO https://raw.githubusercontent.com/Roins-hub/dola-pool/main/docker-compose.yml
curl -fsSL https://raw.githubusercontent.com/Roins-hub/dola-pool/main/deploy/dola.env.example -o dola.env
# Edit dola.env: at least DOLA_ADMIN_KEY and DOLA_PUBLIC_BASE (public address, e.g. http://your-ip:8000)
docker compose up -d
```

- The panel is at `http://server-ip:8000/`. All data lives in `./data` and survives upgrades.
- The image `ghcr.io/roins-hub/dola-pool` runs the **pure API** path only. It supports cookie-imported accounts and ships without a browser.

### On your own computer

Download the source archive from the [Release](https://github.com/Roins-hub/dola-pool/releases/latest) page and unpack it, then:
- on Windows, double-click `start-local.bat`;
- on macOS / Linux, run `bash start-local.sh`.

You need Python 3.10+ and Node.js 18+. Outside Japan / Korea you need a proxy, see [`USAGE.md`](USAGE.md).

<details>
<summary><b>Manual install (source + systemd)</b></summary>

dola.com requires a Japanese or Korean exit IP, so a server in mainland China must use a proxy. For static and rotating proxies and how to avoid rate limits, see [`PROXY.md`](PROXY.md) (Chinese).

```bash
git clone https://github.com/Roins-hub/dola-pool.git && cd dola-pool
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
patchright install chromium                           # needed by the browser fallback

export DOLA_API_KEYS=sk-your-key                      # public API keys, comma-separated
export DOLA_ADMIN_KEY=change-me                       # admin panel key
export DOLA_PROXY=http://user:pass@jp-proxy:port
export DOLA_PUBLIC_BASE=https://your-domain           # public address used in video_url
uvicorn server:app --host 127.0.0.1 --port 8002       # listen locally, expose through nginx
```

To keep it running with systemd, put the variables in `/opt/dola-pool/dola.env` and create `/etc/systemd/system/dola-pool.service`:

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

Run `sudo systemctl enable --now dola-pool`, then put nginx with HTTPS in front of port 8002. For one-click updates on a manual install, also run `sudo bash deploy/install-updater.sh`.

</details>

## Usage

```bash
# Create a task
curl -X POST https://your-domain/v1/videos \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"seedance-2.5","prompt":"An orange kitten chasing butterflies on the grass","duration":10,"ratio":"16:9"}'
# -> {"id":"video_xxx","status":"queued",...}

# Poll: queued -> processing -> completed / failed
curl https://your-domain/v1/videos/video_xxx -H "Authorization: Bearer sk-your-key"

# Download the video
curl -o out.mp4 https://your-domain/v1/videos/video_xxx/content -H "Authorization: Bearer sk-your-key"
```

- **Reference images**: pass a public URL or Base64 in any of `reference_images`, `image_url`, `image` or `input_image(s)`, up to 20 MiB each.
- **Duration**: accepted as `duration`, `seconds` or `duration_seconds`.
- **Aspect ratio**: the request parameter wins. If the prompt mentions a different ratio, dola is told to follow the parameter.
- **Resolution**: optionally pass `resolution` (`720p` / `1080p` / `2k` / `4k`). With auto-upscaling enabled in the panel, finished videos are upscaled to that tier; see [UPSCALE.md](UPSCALE.md).
- **Reference source note**: tasks with reference images get a note saying the references are AI-generated material by default (`DOLA_DEFAULT_REFERENCE_SOURCE=ai_generated`). Pass `"reference_source": "unspecified"` to skip it. The note only states the source truthfully; it does not guarantee the upstream review will pass.

For Doubao video, switch the model to `doubao-seedance-*`. Image generation runs on Doubao accounts only and returns synchronously:

```bash
curl -X POST https://your-domain/v1/images/generations \
  -H "Authorization: Bearer sk-your-key" -H "Content-Type: application/json" \
  -d '{"model":"doubao-seedream-5.0-flash","prompt":"A red fox in the snow","size":"1536x1024"}'

# Image-to-image / editing: multipart upload, or pass URLs in reference_images as JSON
curl -X POST https://your-domain/v1/images/edits -H "Authorization: Bearer sk-your-key" \
  -F model=doubao-seedream-5.0-flash -F prompt="Remove the cat's whiskers" -F image=@cat.jpg
```

Fields, error codes and admin endpoints are in [`API.md`](API.md).

## Models

### dola accounts

| Model | Duration | Points per video (4 per account per day by default) |
|---|---|---|
| `seedance-2.5` | 5 / 10 / 30 s | 5 s = 2, 10 s = 4, 30 s = 2 (4 in total when the second half has to be generated separately) |
| `seedance-2.0-fast` | 5 / 10 s, plus 15 s and 30 s (two stitched segments) with Work mode on | 5 s = 2, 10 s = 4; 15 s cost unknown |

- **Output and references**: 1280×720, 24 fps, H.265. Up to 10 reference images; reference audio and video are not supported.
- **15 s on 2.0 (optional)**: turn on "2.0 的 15 秒" in the panel settings menu to route it through Dola Work mode, with reference images. It takes effect at once. It needs CloakBrowser and the Docker image does not include it. The assistant may rewrite the prompt, one video takes about 6 minutes, and an account that made one is not used again that day.
- **15 seconds**: 15 s on seedance-2.5 costs 12 points, more than a free account gets per day, so it is rejected with 422. On seedance-2.0-fast, 15 s is gone from normal chat and is only available through Work mode above.
- **30 seconds**: controlled by `DOLA_30S_MODE`.
  - `reject` (default): rejected.
  - `pair` (recommended): requests 30 s as is. dola usually delivers the full 30 s for 2 points; when it only delivers half, the second half is generated and stitched locally. Cookie-imported accounts only.

  See [issue #4](https://github.com/Roins-hub/dola-pool/issues/4).
- **Naming**: `seedance-2.0-fast` is "Seedance 2.0 Fast" on the website; the old name `seedance-2.0` still works.

### Doubao accounts

| Model | Duration | Who can use it | Cost shown by Doubao |
|---|---|---|---|
| `doubao-seedance-2.0-mini` | any whole number of seconds, 4–15 | free and paid | everyday |
| `doubao-seedance-2.0-fast` | 4–15 s | free and paid | fast |
| `doubao-seedance-2.0` | 4–15 s | paid only | 2× |
| `doubao-seedance-2.5` | 4–30 s (native, not stitched) | paid only | 5× |

Image generation (Seedream):

| Model | Name in Doubao |
|---|---|
| `doubao-seedream-5.0-flash` | Seedream 5.0 Flash |
| `doubao-seedream-5.0-pro` | Seedream 5.0 Pro (4× cost) |
| `doubao-seedream-4.5` | Seedream 4.5 |
| `doubao-seedream-4.0` | Seedream 4.0 |

Images take about 20 seconds, at 2K.

<details>
<summary><b>Doubao details</b> (quota, watermark, paid confirmation, membership detection)</summary>

- **Quota**: Doubao does not publish its daily free quota and never reports what is left. The multipliers above are quoted from the Doubao page. The panel shows "N made today", and `DOUBAO_DAILY_LIMIT` is only a safety cap.
- **Paid confirmation**: when the free quota runs out, Doubao asks whether to use paid quota. With `DOUBAO_ALLOW_PAID=1` (default) the pool confirms; with `0` it switches accounts.
- **Membership detection**: when a free account requests 2.0 / 2.5, Doubao offers Mini instead. The pool **never accepts**: it marks the account as free and re-dispatches to a paid account.
- **Parameter check**: Doubao first asks to confirm the parameters. The pool confirms only when duration and model match; otherwise it reports an error.
- **Watermark**: the visible "AI generated" watermark is a per-account setting. With `DOUBAO_AUTO_NO_WATERMARK=1` (default) the pool turns it off through the official settings API and downloads through the official watermark-free endpoint. Whoever publishes the content must label it as AI-generated.
- **Aspect ratio**: video supports `16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9`. For images, Doubao reads the ratio from the prompt text, and the pool writes it in automatically.
- **Network and import**: works from servers outside China without a Japanese / Korean proxy. Accounts can only be imported by cookie, see [COOKIE.md](COOKIE.md).
- **Plugin names**: the official names used by the new-api Doubao plugin (such as `doubao-seedance-2-0-260128`) are not Doubao-account models; they still go to dola.

</details>

## Connecting new-api

Use new-api's built-in **Doubao task plugin** (channel type "Task plugin", plugin Doubao):

1. Set the API address to this service and the key to one of `DOLA_API_KEYS`.
2. Add the official names the plugin knows, `doubao-seedance-2-0-260128` and `doubao-seedance-2-5-260628`, then add a model mapping:
   ```json
   {"doubao-seedance-2-0-260128": "seedance-2.0-fast", "doubao-seedance-2-5-260628": "seedance-2.5"}
   ```
3. For Doubao video, add names like `doubao-seedance-2.0-mini` directly. For images, connect `doubao-seedream-*` through an OpenAI channel.

The channel's **Test** button does not support task plugins. Seeing `Task Plugin channel test is not supported` is normal; verify with a real request.

## Scheduling

- **Picking and switching accounts**: an idle account is chosen by weight and priority. On insufficient quota, risk control or a confirmed upstream failure, the task moves to another account. When the whole pool is out of quota, the request gets 429 immediately.
- **Time limit**: once an account is assigned, a task has `DOLA_TASK_DEADLINE` (600 s by default) to succeed; queueing time does not count. On timeout it fails and lists the accounts it tried.
- **No duplicate videos**: a task that already has an upstream conversation and only timed out waiting for the result is **not** resubmitted elsewhere, to avoid duplicate videos and double charges.
- **dola's questions**: sometimes dola asks instead of generating (which ratio, whether to start, "already generated"…). The service then:
  1. replies twice from the task parameters;
  2. if dola is still asking, hands over to the optional AI responder (panel: Settings → AI responder);
  3. ends the task only if neither works. Nothing has been generated at that point, so nothing is charged.
- **Reference uploads**: on the browser path, uploads go straight to ByteDance's image servers; all other traffic uses the account's own proxy.
- **Global concurrency**: panel Settings → Global concurrency, `0` = unlimited. Each account handles one task at a time.

## Configuration

Set these in `dola.env` (one-command install), `.env`, or the environment. The full list is in [`config.py`](config.py).

| Variable | Default | Meaning |
|---|---|---|
| `DOLA_API_KEYS` | empty | Public API keys, comma-separated; keys with quotas can also be created in the panel |
| `DOLA_ADMIN_KEY` | empty | Admin API key |
| `DOLA_PROXY` | `http://127.0.0.1:7890` | Default proxy for dola.com; empty = direct. Accounts can have their own proxy |
| `DOLA_PUBLIC_BASE` | `http://127.0.0.1:8000` | Public address used to build `video_url` |
| `DOLA_DAILY_LIMIT` | `4` | Daily quota points per dola account |
| `DOLA_LIMIT_RESET_TZ` / `_HOUR` | `UTC` / `0` | When the daily quota resets |
| `DOLA_TASK_DEADLINE` | `600` | Seconds a task may try after getting an account |
| `DOLA_MAX_QUEUE_WAIT_SECONDS` | `1800` | Maximum seconds to wait in the queue |
| `DOLA_MAX_CONCURRENCY` | `0` | Initial global concurrency, `0` = unlimited; the value saved in the panel wins |
| `DOLA_MAX_PENDING_TASKS` | `100` | Pending task limit; beyond it requests get 429 |
| `DOLA_30S_MODE` | `reject` | 30-second requests: `reject`, or `pair` to request 30 s and fill in the second half if needed |
| `DOLA_VIDEO_DELIVERY` | `download` | `download` stores videos on the server; `link` hands out a short link that redirects to the upstream original, so video traffic skips the server |
| `DOLA_DEFAULT_REFERENCE_SOURCE` | `ai_generated` | Adds the source note to tasks with reference images; `unspecified` = only when the request asks for it |
| `DOLA_UPLOAD_PROXY_BYPASS` | `*.bytevcloudapi.com,*.vodupload.com` | Hosts the browser reaches directly when uploading references; empty = everything through the proxy |
| `DOLA_STALL_REPLY_SECONDS` | `180` | End the task when dola stops without starting for this long (nothing charged); `0` = off |
| `DOLA_GENERATION_FAILURE_RETRIES` | `2` | How many times to resubmit on another account after upstream confirms a failure |
| `DOLA_PORTRAIT_RETRIES` | `3` | When dola refuses for portrait protection before generating anything, retry on this many other accounts (the review varies by account; refusals are not charged); `0` = off |
| `DOLA_PURE_API` / `DOLA_PURE_FALLBACK_BROWSER` | `1` / `1` | Prefer the pure HTTP path; fall back to the browser on failure |
| `DOLA_COOKIE_RUNTIME` | `pure` | `cloak` = run each Dola cookie account in its own CloakBrowser profile (see below) |
| `DOLA_CLOAK_RETRY` | `1` | When a 5/10-second browser submission is rejected with 710022002, retry it once in CloakBrowser (needs CloakBrowser); `0` turns it off |
| `DOUBAO_ALLOW_PAID` | `1` | Use paid quota after the Doubao free quota runs out; `0` = switch accounts |
| `DOUBAO_AUTO_NO_WATERMARK` | `1` | Turn off the visible "AI generated" watermark on Doubao accounts |
| `DOLA_IMAGE_TIMEOUT` | `240` | Maximum seconds for one image request |

<details>
<summary><b>Strict cookie browser isolation (optional)</b></summary>

By default cookie accounts use pure HTTP. To keep each Dola cookie account in its own browser environment:
- server install: deploy with Xvfb + systemd and install `deploy-cloak-requirements.txt`;
- Docker: run `docker compose -f docker-compose.yml -f docker-compose.cloak.yml up -d --build` to build a CloakBrowser image on top of the official one;
- add this configuration:

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_CLOAK_HEADLESS=0
CLOAKBROWSER_CACHE_DIR=/opt/dola-pool/.cloakbrowser
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
```

- **How it works**: each account uses `accounts/<account>/cloak-profile/` and is bound to its own fixed proxy. Verification, submission and polling all go through that profile.
- **Import order**: for batch imports, tick "skip online verification" first, bind each account's proxy, then verify accounts one by one.
- **What it isolates**: browser data only; this is not a virtual machine per account, and separate proxy records do not guarantee separate public IPs.
- **Limits**: the official Docker image does not include CloakBrowser, so build it as above; a self-built image cannot use the panel's one-click update. `DOLA_30S_MODE=pair` only uses pure-API accounts, so strictly isolated accounts are skipped (the 30-second stitching that comes with the 2.0 15-second option is not affected). CloakBrowser is pinned to 0.6.3, because 0.7 and later need a license key.

See [`ISOLATION.md`](ISOLATION.md) (Chinese) for the full steps.

</details>

<details>
<summary><b>Project layout</b></summary>

| Path | Purpose |
|---|---|
| `server.py` | FastAPI service: public API, Ark-compatible API, admin API, task execution |
| `browser_pool.py` | Pool scheduling: account choice, retries, time limits, quota and cooldowns |
| `pool.py` / `store.py` / `user_store.py` | Account pool, task storage (SQLite), admins |
| `proxy_store.py` | Static / dynamic proxies and rotation |
| `pure_api_gen.py` / `protocol/` | Pure HTTP generation (dola; the Doubao site config is in `protocol/doubao_site.py`) |
| `doubao_gen.py` / `image_gen.py` | Doubao video and image generation |
| `browser.py` / `video_worker_ui.py` | Browser automation path |
| `upstream_reply.py` / `dola_responder.py` | Automatic replies to dola's questions and the AI responder |
| `upscale.py` | Video upscaling |
| `web/` | Admin panel; `web/i18n/` holds translations, see [`I18N.md`](I18N.md) |
| `deploy/` | Install and one-click update scripts |
| `Dockerfile` / `docker-compose.yml` / `docker/` | Docker image and compose files |
| `tests/` | pytest suite: `pip install pytest && pytest -q tests` |

</details>

## Notes

- **Cookie lifetime**: account cookies expire after about 60 days. Expired accounts are flagged automatically and need to be re-imported. **Do not log out in the browser** of an account you imported: logging out invalidates its cookie immediately.
- **dola watermark**: the pool downloads the watermark-free version of the same video from dola's own response (`lr=unwatermarked`) and does not alter the frames. The third-party parsing service is off by default (`DOLA_PURE_REMOVE_WATERMARK=0`); see [`USAGE.md`](USAGE.md#7-去水印说明) for how it works and the risks.
- **Credentials**: `accounts/`, `*.db`, `dola.env`, `cookies.txt` and `downloads/` are git-ignored. Never commit real credentials.

## License

This project's own code is released under the [MIT License](LICENSE). The following are not part of this project and are not covered by it:

- `extensions/dola30/`: a third-party extension; its license and rights belong to its author.
- `bdcaptcha.js` and `protocol/js/bdms-sdk.js`: SDK files obtained from upstream web pages; all rights belong to their owners.
- `web/fonts/`: IBM Plex fonts, under the [SIL Open Font License 1.1](web/fonts/OFL.txt).

## Disclaimer

This repository is a personal record of learning and interoperability research, including reverse-engineered implementations of upstream web APIs. It contains no accounts, cookies, keys or working credentials.

Users must follow the terms of service of dola.com and doubao.com and the laws where they live, including rules on labelling AI-generated content, and assess their own account and compliance risks.
