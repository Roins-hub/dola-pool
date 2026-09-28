# 部署手册（CentOS 7 / glibc 2.17 / 宝塔面板）

这是线上实际在用的部署方式，模板都在本目录。老机器（glibc 2.17）
踩过的坑都写在下面，照着走能一次到位。

## 0. 目录约定

| 项 | 路径 |
|---|---|
| 代码 | `/www/wwwroot/stable-dola-pool`（属主 `www:www`） |
| Python 运行时 | `/www/server/pyenv-dola/python`（CPython 3.11.9 独立发行版） |
| 虚拟环境 | `<代码目录>/.venv` |
| Node（bdms 签名） | `/usr/local/bin/node`（v16 即可，官方二进制在 glibc 2.17 上能跑） |
| 日志 | `/www/wwwlogs/python/stable-dola-pool/{access,error}.log` |
| 运行期数据 | `tasks.db`、`pool_usage.db`、`downloads/`、`refs/`、`accounts/` |

## 1. Python 与依赖

```bash
# 1) 独立发行版 Python（自带 OpenSSL 3，不依赖系统 python2）
mkdir -p /www/server/pyenv-dola && cd /www/server/pyenv-dola
curl -sSL -o py311.tar.gz \
  https://github.com/astral-sh/python-build-standalone/releases/download/20240726/cpython-3.11.9%2B20240726-x86_64-unknown-linux-gnu-install_only.tar.gz
tar -xzf py311.tar.gz && rm -f py311.tar.gz

# 2) venv + 依赖（必须带 -c 约束文件，见下）
cd /www/wwwroot/stable-dola-pool
/www/server/pyenv-dola/python/bin/python3 -m venv .venv
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --upgrade pip setuptools wheel
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -c deploy-constraints.txt -r requirements.txt
.venv/bin/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -c deploy-constraints.txt -r deploy-extra-requirements.txt
```

**为什么必须带 `-c deploy-constraints.txt`**：`pillow / greenlet / numpy / opencv-python-headless`
最新版只发 manylinux_2_28 轮子，glibc 2.17 上会退化成源码编译，而这类机器通常没有 clang。
`deploy-extra-requirements.txt` 则补了 `requirements.txt` 漏掉的 `opencv-python-headless`
（import 链 `server.py → browser_pool.py → video_worker_ui.py → gap.py → cv2`）。

## 2. Node（纯 API 出片的 bdms 签名依赖）

```bash
mkdir -p /www/server/nodejs && cd /www/server/nodejs
curl -sSL -o node16.tar.xz https://registry.npmmirror.com/-/binary/node/v16.20.2/node-v16.20.2-linux-x64.tar.xz
tar -xJf node16.tar.xz && rm -f node16.tar.xz
ln -sfn /www/server/nodejs/node-v16.20.2-linux-x64/bin/node /usr/local/bin/node
ln -sfn /www/server/nodejs/node-v16.20.2-linux-x64/bin/npm  /usr/local/bin/npm

# 自测（能出 a_bogus 就对了）
echo '{"url":"https://www.dola.com/api/samantha/chat/completion?aid=1","method":"POST","headers":{},"body":"{}","cookies":{}}' \
  | node protocol/js/bdms_sign_url.js | head -c 160
```

> v18/v20 官方二进制要 glibc ≥ 2.28，CentOS 7 跑不了；v16 足够。

## 3. systemd 托管（本目录的模板）

```bash
cp deploy/run.sh.example /www/wwwroot/stable-dola-pool/run.sh     # 改域名/Key 等
cp deploy/service.sh          /www/wwwroot/stable-dola-pool/
cp deploy/healthcheck.sh      /www/wwwroot/stable-dola-pool/
chmod 750 /www/wwwroot/stable-dola-pool/{run.sh,service.sh,healthcheck.sh}
chown -R www:www /www/wwwroot/stable-dola-pool

cp deploy/stable-dola-pool.service        /etc/systemd/system/
cp deploy/stable-dola-pool-health.service /etc/systemd/system/
cp deploy/stable-dola-pool-health.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now stable-dola-pool stable-dola-pool-health.timer
```

- `stable-dola-pool.service`：单进程 uvicorn，`Restart=always`，`MemoryLimit=2800M`
  （跑飞被内核杀掉后自动拉起，而不是半死卡着）。
- `stable-dola-pool-health.timer` + `healthcheck.sh`：每 2 分钟探 `/health`，
  连续 3 次不通自动重启服务。

## 4. nginx 反代（模板见 `nginx-site.conf.example`）

- 站点 `server_name` 指向你的域名，`proxy_pass http://127.0.0.1:8000;`（8000 不对外）；
- **必须** `client_max_body_size 64m;`（画布会 multipart 传参考图，默认 1m 会 413）；
- 保留 `location ~ \.well-known { allow all; }`，否则宝塔申请/续签证书会失败；
- `proxy_read_timeout 1800s;`（30 秒档出片要 6~20 分钟，别用默认 60s 掐断）。

宝塔面板里就是：**网站 → 你的域名 → 反向代理 → 目标 `http://127.0.0.1:8000`**，
SSL 用面板一键申请（Let's Encrypt），续签计划任务面板自带。

## 5. 环境变量（`run.sh` 是唯一事实来源）

| 变量 | 建议值 | 说明 |
|---|---|---|
| `DOLA_PUBLIC_BASE` | `https://你的域名` | `video_url` 拼接基址，客户端必须可达 |
| `DOLA_API_KEYS` | `sk-dola-…` | 环境变量 Key（不受额度限制），逗号分隔多个 |
| `DOLA_MAX_CONCURRENCY` | `3`（0 = 不限） | 全局并发闸门；号池大/机器小建议设正数 |
| `DOLA_MAX_PENDING_TASKS` | `5000` | 待处理任务上限（超出 429）。放开并发后这才是真正的派发闸门 |
| `DOLA_THREAD_POOL_MAX` | `512` | 阻塞线程池；纯 API 出片跑在 `asyncio.to_thread` 里，`0` = 自动(CPU×8) |
| `DOLA_PURE_POLL_INTERVAL` | `60` | 上游轮询间隔（秒）。每次轮询都要过 BDMS 签名，调大直接降签名压力 |
| `DOLA_SIGNER_PROCESSES` | `12` | BDMS 签名进程池大小。签名是 CPU 密集，并发靠多进程横向扩，建议 ≈ CPU 核数 |
| `DOLA_REFERENCE_THUMB_COUNT` | `1` | 面板「参考图」留存的缩略图张数（`0` = 关闭）；最长边 `DOLA_REFERENCE_THUMB_MAX_PX`(160) |
| `DOLA_MIN_SUBMIT_INTERVAL_SECONDS` | `20` | 同号两次提交最小间隔，缓解上游 `710022002` 限流 |
| `DOLA_NATIVE_DURATION_MAX` | `15` | 任意时长上限（0 = 只放原生 5/10/15/30） |
| `DOLA_ALLOW_30S_PAIR` | `1` | 上游要拆段时本地生成 2×15 秒 + ffmpeg 拼接 |
| `DOLA_CORS_ORIGINS` | `*` 或前端域名 | 浏览器直连（画布）需要 |
| `DOLA_REFERENCE_FILE_DIR` / `DOLA_REFERENCE_TOTAL_MAX_BYTES` | `refs` / `67108864` | 参考图落盘目录与单请求总量上限 |
| `DOLA_MAX_PROMPT_CHARS` | `20000` | 单条 prompt 上限（超出 422） |
| `DOLA_TASK_RETENTION_DAYS` / `DOLA_DOWNLOAD_MAX_BYTES` | `14` / `21474836480` | 任务保留期、出片目录体积上限（超限自动清旧） |
| `DOLA_MAX_QUEUE_WAIT_SECONDS` / `DOLA_STALE_TASK_SECONDS` | `1800` / `600` | 排队等待上限、看门狗判「没进展」的阈值 |
| `DOLA_SIGN_TIMEOUT` / `DOLA_SIGN_RECYCLE_AFTER` | `15` / `500` | BDMS 签名单次超时、长驻进程重启频率 |

## 6. 排障小抄

```bash
curl -s http://127.0.0.1:8000/health | python -m json.tool \
  | grep -A8 -E 'storage|queue'          # 库/磁盘体积、排队与 ETA
ls -la /www/wwwroot/stable-dola-pool/tasks.db      # 正常 100KB~几 MB 量级
tail -f /www/wwwlogs/python/stable-dola-pool/access.log   # stdout：[maint]/[watchdog]/[resume]/[pool]
tail -f /www/wwwlogs/python/stable-dola-pool/error.log    # 异常堆栈
systemctl status stable-dola-pool                        # 服务状态
```

- 面板卡死先看 `tasks.db` 体积和 `select sum(length(reference_images)) from tasks;`
  —— 任务表里**绝不能存 base64/二进制**（历史上被撑到 587MB，接口 MemoryError）。
- 任务长时间没结果看 `[watchdog]` 行：超过 `DOLA_MAX_QUEUE_WAIT_SECONDS` 仍在排队会被判失败并写明原因。
- 大量 `710022002 当前服务访问频繁` = 上游限流，调大 `DOLA_MIN_SUBMIT_INTERVAL_SECONDS`
  或降低 `DOLA_MAX_CONCURRENCY`，别脉冲式批量提交。
