#!/usr/bin/env bash
# dola-pool 本地一键启动（macOS / Linux 桌面）
# 用法：bash start-local.sh
# 首次运行会自动：创建虚拟环境 → 安装依赖 → 下载 Chromium → 生成 dola.env，然后在前台启动服务。
# 部署到服务器长期运行请用 deploy/install.sh（会注册 systemd 服务、可配域名 HTTPS）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

ok(){ printf '\033[32m[✓]\033[0m %s\n' "$*"; }
info(){ printf '\033[36m[-]\033[0m %s\n' "$*"; }
warn(){ printf '\033[33m[!]\033[0m %s\n' "$*"; }
die(){ printf '\033[31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }

export PIP_CACHE_DIR="$ROOT/.cache/pip"
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.browsers"

PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || die "没找到 Python，请安装 Python 3.10+"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "需要 Python 3.10 以上，当前 $($PY -V 2>&1)"
ok "$($PY -V 2>&1)"

if command -v node >/dev/null; then ok "Node.js $(node -v)"
else warn "没找到 Node.js：Cookie 账号的验证和出片会失败，请安装 Node.js 18+"; fi

[ -x .venv/bin/python ] || { info "创建虚拟环境 .venv ..."; "$PY" -m venv .venv; }
REQ_HASH="$(cat requirements.txt deploy-extra-requirements.txt | cksum | awk '{print $1}')"
if [ "$(cat .venv/.deps-ok 2>/dev/null || true)" != "$REQ_HASH" ]; then
  info "安装 Python 依赖（首次约 2~5 分钟）..."
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt -r deploy-extra-requirements.txt
  echo "$REQ_HASH" > .venv/.deps-ok
fi
ok "Python 依赖就绪"

if [ ! -d .browsers ]; then
  info "下载 Chromium 浏览器（约 150 MB）..."
  .venv/bin/python -m patchright install chromium \
    || warn "Chromium 下载失败：浏览器出片路径不可用（纯 API 出片不受影响）"
fi

if [ ! -f dola.env ]; then
  KEY="sk-dola-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  sed -e "s#^DOLA_API_KEYS=.*#DOLA_API_KEYS=${KEY}#" \
      -e "s#^PLAYWRIGHT_BROWSERS_PATH=.*#PLAYWRIGHT_BROWSERS_PATH=${PLAYWRIGHT_BROWSERS_PATH}#" \
      deploy/dola.env.example > dola.env
  chmod 600 dola.env
  ok "已生成配置 dola.env（API Key: $KEY）"
fi

# 严格 Cookie 隔离模式的可选依赖；默认纯 API 模式不安装。
if grep -qE '^DOLA_COOKIE_RUNTIME=cloak([[:space:]]*|$)' dola.env 2>/dev/null && [ -f deploy-cloak-requirements.txt ]; then
  info "安装 CloakBrowser wrapper（严格 Cookie 隔离模式）..."
  .venv/bin/pip install -q -r deploy-cloak-requirements.txt
  ok "CloakBrowser wrapper 就绪"
fi

# 读入 dola.env（只认 KEY=VALUE 行，跳过注释）
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ''|\#*) continue ;; *=*) export "${line%%=*}=${line#*=}" ;; esac
done < dola.env
: "${PLAYWRIGHT_BROWSERS_PATH:=$ROOT/.browsers}"; export PLAYWRIGHT_BROWSERS_PATH
HOST="${DOLA_HOST:-127.0.0.1}"; PORT="${DOLA_PORT:-8002}"

cat <<EOF

==============================================================
  管理面板   http://127.0.0.1:${PORT}/
  默认账号   admin / admin123（登录后到「用户管理」改密码）
  API Key    ${DOLA_API_KEYS:-}
  配置文件   $ROOT/dola.env
  dola.com 只允许日本/韩国 IP：本机不在日韩时，请在面板「代理管理」配代理，
  或在 dola.env 里设置 DOLA_PROXY（例如本机 Clash：http://127.0.0.1:7890）
  按 Ctrl+C 停止服务
==============================================================

EOF
exec .venv/bin/uvicorn server:app --host "$HOST" --port "$PORT" --workers 1
