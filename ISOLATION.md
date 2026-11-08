# Cookie 账号严格隔离

本文说明如何启用 Cookie 账号严格隔离（CloakBrowser），包括服务器脚本部署和 Docker 部署两种方式。代理的配置方法见 [`PROXY.md`](PROXY.md)。

## 1. 功能说明

默认情况下，Cookie 导入的 dola 账号走纯 API：服务器直接发送请求，不打开浏览器，并发高、资源占用低。

启用严格隔离后，每个 Cookie 账号使用一个独立的 CloakBrowser 浏览器环境，并固定绑定自己的代理出口：

- 每个账号有独立的浏览器资料目录 `accounts/<账号>/cloak-profile/`，浏览器指纹、Cookie 和本地存储互不共享。
- 同一出口同一时间只允许一个账号提交请求。
- 被限流时不自动更换 IP，避免同一登录态在短时间内出现设备或 IP 跳变。

### 适用场景

| 场景 | 建议 |
| --- | --- |
| Cookie 账号经常掉线、被风控 | 启用 |
| 需要使用「2.0 的 15 秒」和 30 秒视频 | 必须安装 CloakBrowser（不一定要启用严格隔离，见第 7 节） |
| 账号数量多、需要高并发 | 不建议，严格隔离下每个任务都要打开浏览器 |

### 影响范围

- **只影响 Cookie 导入的 dola 账号**。通过账号密码登录（Google 登录）的账号本来就使用各自独立的浏览器资料目录，不受此设置影响。
- **豆包账号不受影响**，仍走纯 API。
- 隔离的是浏览器环境和网络出口，不是每个账号一台虚拟机。

## 2. 准备工作

1. **为每个账号准备独立出口**：每个账号绑定一个静态 IP，或使用隧道网关为每个账号分配独立 session（见 [`PROXY.md`](PROXY.md)）。多个账号共用同一出口会互相排队。
2. **评估服务器资源**：每个并发任务运行一个浏览器。4 核 8 GB 的服务器建议全局并发 4 至 6。
3. **确认部署方式**：服务器脚本部署和 Docker 部署都支持，步骤分别见第 3、4 节。

## 3. 服务器脚本部署

适用于使用 `deploy/install.sh` 安装、由 systemd 管理的部署。安装脚本会同时配置 Xvfb 虚拟显示器，CloakBrowser 有头运行需要它。

### 3.1 修改配置

在 `/opt/dola-pool/dola.env` 中添加：

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_CLOAK_HEADLESS=0
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_EGRESS_THROTTLE_ROTATE=0
DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
CLOAKBROWSER_CACHE_DIR=/opt/dola-pool/.cloakbrowser
```

除第一行外都是启用 `cloak` 后的默认值，写出来是为了明确意图，可按需调整，含义见第 6 节。

### 3.2 安装 CloakBrowser

**新安装**：先写好 `dola.env` 再运行安装脚本。脚本检测到 `DOLA_COOKIE_RUNTIME=cloak` 时会自动安装 CloakBrowser。

**已安装的服务器**：手动安装后重启服务：

```bash
sudo -u ubuntu /opt/dola-pool/.venv/bin/pip install -r /opt/dola-pool/deploy-cloak-requirements.txt
sudo systemctl restart dola-pool
```

将 `ubuntu` 换成实际运行服务的用户。浏览器内核在第一次启动浏览器时自动下载到 `CLOAKBROWSER_CACHE_DIR`（约 700 MB），约需一到两分钟。

> **关于 CloakBrowser 版本**：`deploy-cloak-requirements.txt` 固定为 `cloakbrowser==0.6.3`。0.7 起下载浏览器内核需要授权码（`CLOAKBROWSER_LICENSE_KEY`），没有授权码时浏览器无法启动，工作模式会全部失败。请不要自行升级；如果之前装过 0.7 以上的版本，重新执行上面的 `pip install` 命令即可降回 0.6.3。

### 3.3 确认生效

- 面板右上角设置中的「2.0 的 15 秒」可以打开（未安装 CloakBrowser 时开关为灰色）。
- 对一个 Cookie 账号点击「验证」，成功后服务器上会出现 `accounts/<账号>/cloak-profile/` 目录。

## 4. Docker 部署

官方镜像只包含纯 API 依赖，不含 CloakBrowser。仓库提供 `Dockerfile.cloak` 和 `docker-compose.cloak.yml`，在官方镜像的基础上加入 CloakBrowser、Xvfb、中日文字体和浏览器运行库，在本机构建。

### 4.1 获取文件

需要以下文件，放在 `docker-compose.yml` 所在目录：

- `Dockerfile.cloak`
- `docker-compose.cloak.yml`
- `docker/entrypoint.sh`
- `deploy-cloak-requirements.txt`

使用 `git clone` 部署时这些文件已经在仓库中。Release 中的 Docker 压缩包从 v2.7.6 起也已包含这四个文件。使用更早的压缩包时，从仓库下载这四个文件，保持上述目录结构，例如：

```bash
V=main   # 也可以写成具体的版本 tag，例如 v2.7.6
base=https://raw.githubusercontent.com/Roins-hub/dola-pool/$V
mkdir -p docker
for f in Dockerfile.cloak docker-compose.cloak.yml docker/entrypoint.sh deploy-cloak-requirements.txt; do
  curl -fsSL "$base/$f" -o "$f"
done
```

### 4.2 修改配置

在 `dola.env` 中添加：

```env
DOLA_COOKIE_RUNTIME=cloak
DOLA_ISOLATE_SHARED_EGRESS=1
DOLA_EGRESS_THROTTLE_ROTATE=0
DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS=1
DOLA_MIN_SUBMIT_INTERVAL_SECONDS=15
```

`DOLA_CLOAK_HEADLESS`、`CLOAKBROWSER_CACHE_DIR` 和 `DISPLAY` 已在镜像中设置，无需填写。

在同目录的 `.env` 中指定基础版本，例如：

```env
DOLA_IMAGE_TAG=2.7.5
```

### 4.3 构建并启动

```bash
docker compose -f docker-compose.yml -f docker-compose.cloak.yml up -d --build
```

- 第一次构建约需几分钟，会下载系统依赖、字体和 CloakBrowser 0.6.3（版本说明见 3.2）。
- 浏览器内核在第一次使用时下载到数据卷的 `./data/.cloakbrowser`，重建容器后不会重复下载。
- 容器启动时会自动运行 Xvfb 虚拟显示器。
- 已为容器分配 1 GB 共享内存（`shm_size`），Chromium 默认的 64 MB 不够用。

之后所有 `docker compose` 命令都需要同时带上两个 `-f` 参数。也可以在 `.env` 中加一行，之后直接使用 `docker compose up -d --build`、`docker compose logs -f` 等命令：

```env
COMPOSE_FILE=docker-compose.yml:docker-compose.cloak.yml
```

（Windows 下用分号 `;` 分隔。）

### 4.4 确认生效

```bash
# Xvfb 和 CloakBrowser 是否就绪
docker compose exec dola-pool sh -c 'pgrep -a Xvfb; pip show cloakbrowser | grep Version'
```

输出中应有 `Xvfb :99` 和 `Version: 0.6.3`。然后在面板中确认：

- 「2.0 的 15 秒」开关可以打开。
- 对一个 Cookie 账号点击「验证」，成功后 `./data/accounts/<账号>/cloak-profile/` 目录出现。
- 第一次使用浏览器后，`./data/.cloakbrowser/` 中出现浏览器内核。

### 4.5 更新版本

自建镜像不在镜像仓库中，**面板的一键更新不可用**，请手动更新：

1. 修改 `.env` 中的 `DOLA_IMAGE_TAG` 为新版本号。
2. 按 4.1 的方法下载新版本的四个文件（`git clone` 部署执行 `git pull`）。
3. 重新构建并启动：

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.cloak.yml up -d --build
   ```

建议从 `docker-compose.yml` 中删除 `updater` 服务，避免误点一键更新。

### 4.6 Docker 部署的限制

- 只支持 Cookie 导入的账号。Docker 镜像没有安装账号密码登录所需的浏览器，不能通过 Google 登录添加账号。
- 一键更新不可用，见 4.5。

## 5. 导入账号

严格隔离下，账号第一次访问 Dola 就应该使用自己的出口。推荐顺序：

1. 「账号管理 → 批量添加」，导入 Cookie 时勾选「**跳过在线验证**」。
2. 勾选导入的账号，点「更改代理」，为每个账号绑定各自的代理。
3. 逐个点击「验证」。

如果先验证再绑定代理，第一次访问会经过默认代理 `DOLA_PROXY`，同一份登录态会在两个出口出现。

## 6. 配置项说明

| 变量 | 启用 cloak 后的默认值 | 说明 |
| --- | --- | --- |
| `DOLA_COOKIE_RUNTIME` | `pure` | 设为 `cloak` 启用严格隔离 |
| `DOLA_CLOAK_HEADLESS` | `0` | `0` 为有头运行（需要 Xvfb），风控识别概率更低 |
| `DOLA_ISOLATE_SHARED_EGRESS` | `1` | 同一出口同一时间只允许一个账号提交 |
| `DOLA_EGRESS_THROTTLE_ROTATE` | `0` | 出口被限流时不自动换 IP |
| `DOLA_EGRESS_THROTTLE_MIN_ACCOUNTS` | `1` | 一个账号被限流即对该出口退避 |
| `DOLA_MIN_SUBMIT_INTERVAL_SECONDS` | `15` | 选号时的提交间隔参数 |
| `CLOAKBROWSER_CACHE_DIR` | 未设置 | 浏览器内核缓存目录，建议放在持久化目录中 |
| `DOLA_CLOAK_GEOIP` | `0` | 是否按出口 IP 推断时区和语言 |
| `DOLA_CLOAK_HUMANIZE` | `0` | 是否模拟人类的鼠标和键盘节奏 |
| `CLOAKBROWSER_LICENSE_KEY` | 空 | CloakBrowser 授权码。固定的 0.6.3 不需要，留空即可 |

`DOLA_MIN_SUBMIT_INTERVAL_SECONDS` 只在选号时生效，不是对所有请求的强制限速。需要降低整体压力时，请同时调低全局并发。

## 7. 与 15 秒、30 秒视频的关系

「2.0 的 15 秒」和 30 秒视频通过 Dola 工作模式生成，**无论是否启用严格隔离，都使用 CloakBrowser**，因为只有它能通过工作模式的风控。

- **只想使用 15 秒和 30 秒**：安装 CloakBrowser 即可（服务器部署按 3.2 安装，Docker 部署使用 `Dockerfile.cloak`），`DOLA_COOKIE_RUNTIME` 可以保持 `pure`。
- **同时启用严格隔离**：按本文完整配置。

安装 CloakBrowser 后还有一个附带作用：Google 登录的号出 5 秒、10 秒时平时仍用自己的浏览器，提交被 `710022002` 拒绝时，会把登录态复制进临时 CloakBrowser 再提交一次（`DOLA_CLOAK_RETRY=1`，默认开启）。

## 8. 数据与安全

- `accounts/<账号>/cloak-profile/` 中保存该账号的浏览器资料，其中 `.cloak-session-cookies.json` 包含登录凭据（Linux 下权限为 600）。不要分享，也不要提交到 Git。
- **不要复制 profile**：不要把一个账号的 `cloak-profile/` 或 `.cloakbrowser-seed` 复制给另一个账号，否则两个账号会使用相同的浏览器指纹。
- **备份**：备份时连同 `accounts/` 目录一起备份；Docker 部署备份整个 `./data` 目录。
- 普通 Cookie JSON 不一定包含 localStorage 和 IndexedDB，导入 Cookie 不能完整还原原设备的浏览器环境。

## 9. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 「2.0 的 15 秒」开关为灰色 | 未安装 CloakBrowser | 服务器部署按 3.2 安装；Docker 部署使用 `Dockerfile.cloak` |
| 浏览器启动失败，日志提到 license 或授权码 | 安装了 0.7 以上的 CloakBrowser | 按 3.2 重新安装 0.6.3；Docker 部署确认 `deploy-cloak-requirements.txt` 是最新的并重新构建 |
| 启动浏览器报找不到显示器 | 没有运行 Xvfb | 服务器部署检查 `dola-pool-xvfb` 服务；Docker 部署确认使用了新的 `docker/entrypoint.sh` |
| 页面崩溃或空白 | 共享内存不足 | Docker 部署确认 `shm_size` 已生效 |
| 页面文字显示为方块 | 缺少中日文字体 | 服务器安装 `fonts-noto-cjk`；`Dockerfile.cloak` 已包含 |
| 任务排队时间变长 | 同一出口的账号在排队 | 为账号分配更多独立出口 |
| 账号频繁掉线 | 同一份 Cookie 在其他浏览器中使用，或首次验证经过了默认代理 | 按第 5 节的顺序重新导入 |
