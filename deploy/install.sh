#!/usr/bin/env bash
# dola-pool 一键部署脚本（Ubuntu 20.04+ / Debian 11+，x86_64 / arm64）
#
# 用法一：下载 Release 包解压后，在项目目录里执行
#   sudo bash deploy/install.sh
#
# 用法二：直接一条命令（脚本会自己从 GitHub 拉代码）
#   curl -fsSL https://github.com/Roins-hub/dola-pool/releases/latest/download/install.sh | sudo bash
#
# 带域名 + 自动 HTTPS（需要先把域名 A 记录解析到本机）：
#   curl -fsSL .../install.sh | sudo DOLA_DOMAIN=video.example.com bash
#
# 可选环境变量（都有默认值）：
#   DOLA_DOMAIN       对外域名；设置后自动装 nginx 反代 + Let's Encrypt 证书，服务只监听 127.0.0.1
#   DOLA_EMAIL        申请证书用的邮箱（不填则不登记邮箱）
#   DOLA_DIR          安装目录，默认 /opt/dola-pool
#   DOLA_PORT         服务端口，默认 8002
#   DOLA_SERVICE      systemd 服务名，默认 dola-pool
#   DOLA_USER         运行服务的系统用户，默认执行 sudo 的那个用户（没有则 root）
#   DOLA_DISPLAY      Xvfb 虚拟显示编号，默认 99（和已有 Xvfb 冲突时改掉）
#   DOLA_VERSION      用法二时拉取的 Git 标签/分支，默认 main
#                     装好后可在管理面板「版本」里一键更新到新的正式版（deploy/install-updater.sh）
#   DOLA_REPO         用法二时的仓库地址
#
# 重复执行 = 升级：会更新代码和依赖，但保留 dola.env、账号、数据库、视频等全部数据。
set -euo pipefail

DOLA_DIR="${DOLA_DIR:-/opt/dola-pool}"
DOLA_PORT="${DOLA_PORT:-8002}"
DOLA_SERVICE="${DOLA_SERVICE:-dola-pool}"
DOLA_DOMAIN="${DOLA_DOMAIN:-}"
DOLA_EMAIL="${DOLA_EMAIL:-}"
DOLA_VERSION="${DOLA_VERSION:-main}"
DOLA_REPO="${DOLA_REPO:-https://github.com/Roins-hub/dola-pool.git}"
DOLA_USER="${DOLA_USER:-${SUDO_USER:-root}}"
DOLA_DISPLAY="${DOLA_DISPLAY:-99}"
NODE_MIN_MAJOR=16
NODE_DIST_VERSION="v20.18.1"

c_ok(){ printf '\033[32m[✓]\033[0m %s\n' "$*"; }
c_info(){ printf '\033[36m[-]\033[0m %s\n' "$*"; }
c_warn(){ printf '\033[33m[!]\033[0m %s\n' "$*"; }
die(){ printf '\033[31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash $0"
command -v apt-get >/dev/null || die "目前只支持 Ubuntu / Debian（需要 apt-get）。其它系统请参考 deploy/README.md 手动部署。"
id "$DOLA_USER" >/dev/null 2>&1 || die "系统用户 $DOLA_USER 不存在，请用 DOLA_USER=xxx 指定"
DOLA_GROUP="$(id -gn "$DOLA_USER")"

export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- 1. 系统依赖
c_info "安装系统依赖（python3 / venv / xvfb / ffmpeg / git）..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip xvfb ffmpeg git curl ca-certificates xz-utils >/dev/null
PY=python3
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "需要 Python 3.10 以上，当前是 $($PY -V 2>&1)。请升级系统（Ubuntu 22.04+ / Debian 12+）。"
c_ok "Python $($PY -V 2>&1 | awk '{print $2}')"

# Node.js：纯 API 出片的 bdms 签名依赖它（缺了会导致验证/出片失败）
node_major(){ command -v node >/dev/null && node -v | sed 's/^v\([0-9]*\).*/\1/' || echo 0; }
if [ "$(node_major)" -lt "$NODE_MIN_MAJOR" ]; then
  c_info "安装 Node.js ${NODE_DIST_VERSION}（系统自带的版本太旧或没有）..."
  case "$(uname -m)" in
    x86_64) NARCH=x64 ;; aarch64|arm64) NARCH=arm64 ;; *) die "不支持的 CPU 架构：$(uname -m)" ;;
  esac
  tmp="$(mktemp -d)"
  curl -fsSL "https://nodejs.org/dist/${NODE_DIST_VERSION}/node-${NODE_DIST_VERSION}-linux-${NARCH}.tar.xz" -o "$tmp/node.tar.xz"
  mkdir -p /opt/dola-node
  tar -xJf "$tmp/node.tar.xz" -C /opt/dola-node --strip-components=1
  ln -sfn /opt/dola-node/bin/node /usr/local/bin/node
  rm -rf "$tmp"
fi
c_ok "Node.js $(node -v)"

# ---------------------------------------------------------------- 2. 代码
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
SRC_DIR=""
if [ -f "$SCRIPT_DIR/../server.py" ]; then
  SRC_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
fi

mkdir -p "$DOLA_DIR"
if [ -n "$SRC_DIR" ] && [ "$SRC_DIR" = "$(cd "$DOLA_DIR" && pwd)" ]; then
  c_ok "就地安装：$DOLA_DIR"
elif [ -n "$SRC_DIR" ]; then
  c_info "复制代码 $SRC_DIR → $DOLA_DIR（保留已有数据）..."
  # 只覆盖代码文件；运行期数据（账号、数据库、视频、配置）一律不碰
  tar -C "$SRC_DIR" \
      --exclude=.git --exclude=.venv --exclude=.browsers --exclude=accounts --exclude='*.db' \
      --exclude=downloads --exclude=thumbs --exclude=refs --exclude=dola.env --exclude=cred.key \
      -cf - . | tar -C "$DOLA_DIR" -xf -
else
  c_info "从 GitHub 拉取代码（$DOLA_VERSION）..."
  tmp="$(mktemp -d)"
  git clone -q --depth 1 --branch "$DOLA_VERSION" "$DOLA_REPO" "$tmp/src"
  tar -C "$tmp/src" --exclude=.git -cf - . | tar -C "$DOLA_DIR" -xf -
  rm -rf "$tmp"
fi
chown -R "$DOLA_USER:$DOLA_GROUP" "$DOLA_DIR"
c_ok "代码就绪：$DOLA_DIR"

# ---------------------------------------------------------------- 3. Python 依赖 + 浏览器
as_user(){ runuser -u "$DOLA_USER" -- env HOME="$(getent passwd "$DOLA_USER" | cut -d: -f6)" "$@"; }

c_info "创建虚拟环境并安装 Python 依赖（首次约 2~5 分钟）..."
[ -x "$DOLA_DIR/.venv/bin/python" ] || as_user "$PY" -m venv "$DOLA_DIR/.venv"
as_user "$DOLA_DIR/.venv/bin/pip" install -q --upgrade pip
as_user "$DOLA_DIR/.venv/bin/pip" install -q -r "$DOLA_DIR/requirements.txt" -r "$DOLA_DIR/deploy-extra-requirements.txt"
c_ok "Python 依赖安装完成"

c_info "安装 Chromium 浏览器（浏览器出片路径用）..."
as_user env PLAYWRIGHT_BROWSERS_PATH="$DOLA_DIR/.browsers" "$DOLA_DIR/.venv/bin/python" -m patchright install chromium >/dev/null
"$DOLA_DIR/.venv/bin/python" -m patchright install-deps chromium >/dev/null 2>&1 \
  || c_warn "Chromium 系统依赖安装有报错，浏览器路径可能不可用（纯 API 出片不受影响）"
c_ok "Chromium 安装完成"

# ---------------------------------------------------------------- 4. 配置文件
ENV_FILE="$DOLA_DIR/dola.env"
if [ -n "$DOLA_DOMAIN" ]; then
  LISTEN_HOST=127.0.0.1
  PUBLIC_BASE="https://$DOLA_DOMAIN"
else
  LISTEN_HOST=0.0.0.0
  PUBLIC_IP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
  PUBLIC_BASE="http://${PUBLIC_IP}:${DOLA_PORT}"
fi

if [ -f "$ENV_FILE" ]; then
  c_ok "沿用已有配置：$ENV_FILE"
  API_KEY="$(grep -E '^DOLA_API_KEYS=' "$ENV_FILE" | head -1 | cut -d= -f2- | cut -d, -f1)"
  LISTEN_HOST="$(grep -E '^DOLA_HOST=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"; LISTEN_HOST="${LISTEN_HOST:-127.0.0.1}"
  DOLA_PORT="$(grep -E '^DOLA_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)"; DOLA_PORT="${DOLA_PORT:-8002}"
  PUBLIC_BASE="$(grep -E '^DOLA_PUBLIC_BASE=' "$ENV_FILE" | head -1 | cut -d= -f2- || echo "$PUBLIC_BASE")"
  # 老配置没写浏览器位置时补上，否则找不到刚装到 .browsers 里的 Chromium
  grep -qE '^PLAYWRIGHT_BROWSERS_PATH=.+' "$ENV_FILE" \
    || echo "PLAYWRIGHT_BROWSERS_PATH=${DOLA_DIR}/.browsers" >> "$ENV_FILE"
else
  API_KEY="sk-dola-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sed -e "s#^DOLA_HOST=.*#DOLA_HOST=${LISTEN_HOST}#" \
      -e "s#^DOLA_PORT=.*#DOLA_PORT=${DOLA_PORT}#" \
      -e "s#^DOLA_PUBLIC_BASE=.*#DOLA_PUBLIC_BASE=${PUBLIC_BASE}#" \
      -e "s#^DOLA_API_KEYS=.*#DOLA_API_KEYS=${API_KEY}#" \
      -e "s#^PLAYWRIGHT_BROWSERS_PATH=.*#PLAYWRIGHT_BROWSERS_PATH=${DOLA_DIR}/.browsers#" \
      -e "s#^DOLA_NODE=.*#DOLA_NODE=$(command -v node)#" \
      "$DOLA_DIR/deploy/dola.env.example" > "$ENV_FILE"
  chown "$DOLA_USER:$DOLA_GROUP" "$ENV_FILE"; chmod 600 "$ENV_FILE"
  c_ok "已生成配置：$ENV_FILE"
fi

# 严格 Cookie 模式才安装可选 CloakBrowser wrapper；默认纯 API 部署不下载它。
if grep -qE '^DOLA_COOKIE_RUNTIME=cloak([[:space:]]*|$)' "$ENV_FILE"; then
  if [ -f "$DOLA_DIR/deploy-cloak-requirements.txt" ]; then
    c_info "安装 CloakBrowser Python wrapper（严格 Cookie 隔离模式）..."
    as_user "$DOLA_DIR/.venv/bin/pip" install -q -r "$DOLA_DIR/deploy-cloak-requirements.txt"
    c_ok "CloakBrowser wrapper 安装完成（首次启动时按官方渠道校验/下载内核）"
  else
    c_warn "配置启用了 DOLA_COOKIE_RUNTIME=cloak，但缺少 deploy-cloak-requirements.txt"
  fi
fi

# ---------------------------------------------------------------- 5. systemd
c_info "注册 systemd 服务 ${DOLA_SERVICE} ..."
cat > /etc/systemd/system/${DOLA_SERVICE}-xvfb.service <<EOF
[Unit]
Description=Xvfb virtual display for dola-pool
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/Xvfb :${DOLA_DISPLAY} -screen 0 1280x1024x24 -ac
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

cat > "/etc/systemd/system/${DOLA_SERVICE}.service" <<EOF
[Unit]
Description=dola-pool OpenAI compatible video API
After=network.target ${DOLA_SERVICE}-xvfb.service
Wants=${DOLA_SERVICE}-xvfb.service

[Service]
Type=simple
User=${DOLA_USER}
WorkingDirectory=${DOLA_DIR}
Environment=DISPLAY=:${DOLA_DISPLAY}
EnvironmentFile=${ENV_FILE}
ExecStart=${DOLA_DIR}/.venv/bin/uvicorn server:app --host ${LISTEN_HOST} --port ${DOLA_PORT} --workers 1 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q ${DOLA_SERVICE}-xvfb "$DOLA_SERVICE"
systemctl restart ${DOLA_SERVICE}-xvfb "$DOLA_SERVICE"

# ---------------------------------------------------------------- 6. nginx + HTTPS（可选）
if [ -n "$DOLA_DOMAIN" ]; then
  c_info "配置 nginx 反代 + HTTPS：$DOLA_DOMAIN ..."
  apt-get install -y -qq nginx certbot python3-certbot-nginx >/dev/null
  cat > "/etc/nginx/sites-available/${DOLA_SERVICE}" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOLA_DOMAIN};
    client_max_body_size 64m;

    location / {
        # 面板页面不带 Cache-Control 时浏览器会缓存旧版本，no-cache = 每次回源校验
        add_header Cache-Control "no-cache" always;
        proxy_pass http://127.0.0.1:${DOLA_PORT};
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 1800s;
        proxy_send_timeout 1800s;
    }
}
EOF
  ln -sfn "/etc/nginx/sites-available/${DOLA_SERVICE}" "/etc/nginx/sites-enabled/${DOLA_SERVICE}"
  nginx -t >/dev/null 2>&1 || die "nginx 配置检查失败，请执行 nginx -t 查看原因"
  systemctl reload nginx
  if [ -n "$DOLA_EMAIL" ]; then CERT_MAIL=(-m "$DOLA_EMAIL"); else CERT_MAIL=(--register-unsafely-without-email); fi
  if certbot --nginx -d "$DOLA_DOMAIN" --non-interactive --agree-tos --redirect "${CERT_MAIL[@]}" >/dev/null 2>&1; then
    c_ok "HTTPS 证书已签发"
  else
    c_warn "证书申请失败（域名没解析到本机，或 80 端口没放行）。修好后执行：certbot --nginx -d $DOLA_DOMAIN"
  fi
fi

# ---------------------------------------------------------------- 7. 自检
c_info "等待服务启动..."
ok=0
for _ in $(seq 1 30); do
  if curl -fsS -m 3 "http://127.0.0.1:${DOLA_PORT}/v1/models" >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
[ "$ok" = 1 ] || die "服务没起来，查看日志：journalctl -u ${DOLA_SERVICE} -n 80 --no-pager"
c_ok "服务运行中"

# ---------------------------------------------------------------- 8. 面板一键更新
if DOLA_DIR="$DOLA_DIR" DOLA_SERVICE="$DOLA_SERVICE" bash "$DOLA_DIR/deploy/install-updater.sh"; then
  :
else
  c_warn "一键更新程序安装失败，面板里的「更新」不可用（可稍后执行 sudo bash $DOLA_DIR/deploy/install-updater.sh）"
fi

cat <<EOF

==================================================================
  dola-pool 部署完成
------------------------------------------------------------------
  管理面板   ${PUBLIC_BASE}/
  默认账号   admin / admin123   ← 登录后立刻到「用户管理」改密码！
  API 地址   ${PUBLIC_BASE}/v1/videos
  API Key    ${API_KEY}
  配置文件   ${ENV_FILE}
------------------------------------------------------------------
  常用命令
    查看状态   systemctl status ${DOLA_SERVICE}
    查看日志   journalctl -u ${DOLA_SERVICE} -f
    重启服务   systemctl restart ${DOLA_SERVICE}
    升级版本   重新执行本脚本（数据保留）
------------------------------------------------------------------
  下一步：添加代理 → 添加账号 → 验证 → 测试生成
  详细步骤见 USAGE.md：https://github.com/Roins-hub/dola-pool/blob/main/USAGE.md
==================================================================
EOF
if [ -z "$DOLA_DOMAIN" ]; then
  c_warn "没有配置域名：请在云服务器安全组/防火墙里放行 TCP ${DOLA_PORT} 端口，正式使用建议加域名和 HTTPS。"
fi
