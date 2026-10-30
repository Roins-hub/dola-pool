#!/usr/bin/env bash
# 安装面板「一键更新」的执行程序（源码部署）。root 运行；install.sh 会自动调用，已部署的服务器也可以单独执行：
#   sudo bash deploy/install-updater.sh
#   sudo DOLA_DIR=/srv/dola DOLA_SERVICE=dola bash deploy/install-updater.sh
#
# 装好后：面板发起更新 → app 等任务清空写 $UPDATE_DIR/ready → systemd path 单元拉起 updater.sh（root）
# 执行备份、换版本、重启、验证与回滚。卸载：systemctl disable --now <服务名>-updater.path 并删除下面写入的文件。
set -euo pipefail

die(){ printf '\033[31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
ok(){ printf '\033[32m[✓]\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "请用 root 运行：sudo bash $0"
DOLA_DIR="${DOLA_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DOLA_SERVICE="${DOLA_SERVICE:-dola-pool}"
DOLA_REPO_SLUG="${DOLA_REPO_SLUG:-Roins-hub/dola-pool}"
[ -f "$DOLA_DIR/server.py" ] || die "$DOLA_DIR 不是 dola-pool 目录"
systemctl cat "$DOLA_SERVICE" >/dev/null 2>&1 || die "systemd 里没有服务 $DOLA_SERVICE（用 DOLA_SERVICE=xxx 指定）"
command -v python3 >/dev/null || die "缺少 python3"

SVC_USER="$(systemctl show -p User --value "$DOLA_SERVICE")"
SVC_USER="${SVC_USER:-root}"
ENV_FILE="$DOLA_DIR/dola.env"
env_get(){ [ -f "$ENV_FILE" ] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

PORT="$(systemctl show -p ExecStart --value "$DOLA_SERVICE" | grep -oE -- '--port[ =][0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
PORT="${PORT:-$(env_get DOLA_PORT)}"
PORT="${PORT:-8002}"

# 与 app（config.UPDATE_DIR）同一规则：DOLA_UPDATE_DIR，否则数据库所在目录下的 update/
UPDATE_DIR="$(env_get DOLA_UPDATE_DIR)"
if [ -z "$UPDATE_DIR" ]; then
  DB="$(env_get DOLA_DB_PATH)"; DB="${DB:-tasks.db}"
  case "$DB" in /*) ;; *) DB="$DOLA_DIR/$DB" ;; esac
  UPDATE_DIR="$(dirname "$DB")/update"
fi

LIB="/usr/local/lib/${DOLA_SERVICE}-updater"
CONF="/etc/${DOLA_SERVICE}-updater.env"
mkdir -p "$LIB"
install -m 755 "$DOLA_DIR/deploy/updater.sh" "$LIB/updater.sh"
cat > "$CONF" <<EOF
APP_DIR=$DOLA_DIR
SERVICE=$DOLA_SERVICE
SVC_USER=$SVC_USER
UPDATE_DIR=$UPDATE_DIR
HEALTH_URL=http://127.0.0.1:$PORT/health
REPO=$DOLA_REPO_SLUG
BACKUP_DIR=/opt/dola-pool-backups
AGENT_COPY=$LIB/updater.sh
EOF
chmod 644 "$CONF"

cat > "/etc/systemd/system/${DOLA_SERVICE}-updater.service" <<EOF
[Unit]
Description=dola-pool one-click update (triggered from the admin panel)

[Service]
Type=oneshot
Environment=DOLA_UPDATER_CONF=$CONF
ExecStart=$LIB/updater.sh
TimeoutStartSec=1800
EOF
cat > "/etc/systemd/system/${DOLA_SERVICE}-updater.path" <<EOF
[Unit]
Description=Watch for dola-pool one-click update requests

[Path]
PathExists=$UPDATE_DIR/ready
Unit=${DOLA_SERVICE}-updater.service

[Install]
WantedBy=multi-user.target
EOF

mkdir -p "$UPDATE_DIR"
chown "$SVC_USER" "$UPDATE_DIR"
python3 - "$UPDATE_DIR/agent.json" <<'PY'
import json, sys, time
json.dump({"mode": "source", "installed_at": time.time()}, open(sys.argv[1], "w"))
PY
chown "$SVC_USER" "$UPDATE_DIR/agent.json"
systemctl daemon-reload
systemctl enable --now -q "${DOLA_SERVICE}-updater.path"
ok "一键更新已启用：${DOLA_SERVICE}-updater.path 监听 $UPDATE_DIR/ready（配置 $CONF）"
