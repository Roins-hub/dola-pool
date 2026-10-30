#!/usr/bin/env bash
# dola-pool 一键更新程序（源码部署，root 运行）。
#
# 由 systemd 的 <服务名>-updater.path 监听 $UPDATE_DIR/ready 触发（deploy/install-updater.sh 安装），
# 面板发起更新、app 等进行中的任务清空后才会写 ready。按 $UPDATE_DIR/request.json 里的版本：
#   备份代码和数据库 → 换成 GitHub 上的 vX.Y.Z（git 仓库用 fetch + fast-forward，否则下载源码包覆盖）
#   → 依赖有变化才 pip install → 重启服务 → 等 /health 报出新版本号
# 任何一步失败都回滚到原版本并重启。进度写 $UPDATE_DIR/agent-status.json（面板显示），结束后删掉
# request.json 和 ready，app 随即恢复派单。账号、数据库、视频、dola.env 一律不动。
set -uo pipefail

CONF="${DOLA_UPDATER_CONF:-/etc/dola-pool-updater.env}"
# shellcheck disable=SC1090
[ -f "$CONF" ] && . "$CONF"

APP_DIR="${APP_DIR:?APP_DIR 未设置}"
SERVICE="${SERVICE:-dola-pool}"
SVC_USER="${SVC_USER:-root}"
UPDATE_DIR="${UPDATE_DIR:-$APP_DIR/update}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8002/health}"
REPO="${REPO:-Roins-hub/dola-pool}"
GIT_URL="${GIT_URL:-https://github.com/$REPO.git}"
TARBALL_URL="${TARBALL_URL:-https://codeload.github.com/$REPO/tar.gz/refs/tags/%s}"
BACKUP_DIR="${BACKUP_DIR:-/opt/dola-pool-backups}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
VERIFY_SECONDS="${VERIFY_SECONDS:-120}"
PY="${PY:-python3}"
AGENT_COPY="${AGENT_COPY:-}"

REQ="$UPDATE_DIR/request.json"
REQ_ID="" FROM="" TO="" TAG="" BK="" METHOD="" OLD_HEAD="" REQS_CHANGED=0 WORK=""

log(){ echo "[updater] $*"; }

as_user(){
  if [ "$SVC_USER" = "$(id -un)" ]; then "$@"; else runuser -u "$SVC_USER" -- "$@"; fi
}

json_get(){
  "$PY" -c 'import json,sys
try: print(json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2]) or "")
except Exception: print("")' "$1" "$2"
}

status(){  # status <state> <message>
  "$PY" - "$UPDATE_DIR/agent-status.json" "$REQ_ID" "$1" "$2" "$FROM" "$TO" <<'PY'
import json, os, sys, time
path, id_, state, message, frm, to = sys.argv[1:7]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump({"id": id_, "state": state, "message": message, "from_version": frm,
               "to_version": to, "updated_at": time.time()}, fh, ensure_ascii=False)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
  log "$1: $2"
}

app_version(){
  grep -oE '^APP_VERSION = "[0-9]+\.[0-9]+\.[0-9]+"' "$1/server.py" 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+'
}

health_version(){
  curl -fsS -m 5 "$HEALTH_URL" 2>/dev/null \
    | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("version") or "")
except Exception: print("")'
}

reqs_hash(){
  cat "$APP_DIR/requirements.txt" "$APP_DIR/deploy-extra-requirements.txt" 2>/dev/null | sha256sum | cut -c1-64
}

finish(){
  rm -f "$UPDATE_DIR/ready" "$REQ"
  [ -n "$WORK" ] && rm -rf "$WORK"
}

fail_unchanged(){  # 还没动代码就失败：只记录原因
  status failed "$1（未改动任何文件）"
  exit 1
}

backup(){
  mkdir -p "$BK" && chmod 700 "$BK" || return 1
  tar -C "$APP_DIR" -cf "$BK/code.tar" \
      --exclude=./.venv --exclude=./.browsers --exclude=./.cloakbrowser --exclude=./accounts \
      --exclude=./downloads --exclude=./thumbs --exclude=./refs --exclude=./update \
      --exclude='*.db' --exclude='*.db-*' --exclude='__pycache__' . || return 1
  "$PY" - "$APP_DIR" "$BK" <<'PY' || return 1
import glob, os, sqlite3, sys
app, bk = sys.argv[1:3]
for path in glob.glob(os.path.join(app, "*.db")):
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = sqlite3.connect(os.path.join(bk, os.path.basename(path)))
    src.backup(dst)
    dst.close()
    src.close()
PY
  if [ -d "$APP_DIR/.git" ]; then as_user git -C "$APP_DIR" rev-parse HEAD > "$BK/HEAD.txt" 2>/dev/null || true; fi
}

fetch_git(){  # 0 = 已切到新版本；1 = 不是 git 仓库（改用源码包）；2 = git 仓库但拉取失败 / 有改动 / 不能 fast-forward
  [ -d "$APP_DIR/.git" ] || return 1
  if [ -n "$(as_user git -C "$APP_DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    GIT_PROBLEM="代码目录有未提交的改动"
    return 2
  fi
  OLD_HEAD="$(as_user git -C "$APP_DIR" rev-parse HEAD)" || return 1
  if ! as_user git -C "$APP_DIR" fetch -q "$GIT_URL" "+refs/tags/$TAG:refs/tags/$TAG"; then
    # git 仓库不退回源码包覆盖：那样会弄脏工作区，之后的更新都会被「未提交的改动」挡住
    GIT_PROBLEM="从 $GIT_URL 拉取 $TAG 失败"
    return 2
  fi
  if ! as_user git -C "$APP_DIR" merge -q --ff-only "refs/tags/$TAG"; then
    GIT_PROBLEM="当前代码不是 $TAG 的祖先（有本地提交或分叉）"
    return 2
  fi
  return 0
}

fetch_tarball(){
  WORK="$(mktemp -d)" || return 1
  local url
  # shellcheck disable=SC2059
  url="$(printf "$TARBALL_URL" "$TAG")"
  curl -fsSL -m 300 "$url" -o "$WORK/src.tgz" || return 1
  mkdir -p "$WORK/src" && tar -xzf "$WORK/src.tgz" -C "$WORK/src" --strip-components=1 || return 1
  [ "$(app_version "$WORK/src")" = "$TO" ] || { log "源码包里的版本号不是 $TO"; return 1; }
  # 只覆盖代码；运行期数据（账号、数据库、视频、配置）一律不碰（与 install.sh 相同的排除规则）
  tar -C "$WORK/src" --exclude=.git --exclude=.venv --exclude=.browsers --exclude=accounts --exclude='*.db' \
      --exclude=downloads --exclude=thumbs --exclude=refs --exclude=update --exclude=dola.env --exclude=cred.key \
      -cf - . | as_user tar -C "$APP_DIR" -xf - || return 1
}

install_deps(){
  [ -x "$APP_DIR/.venv/bin/pip" ] || return 0
  as_user "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt" \
      -r "$APP_DIR/deploy-extra-requirements.txt"
}

wait_version(){  # wait_version <版本>
  local deadline=$(( $(date +%s) + VERIFY_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$(health_version)" = "$1" ] && return 0
    sleep 2
  done
  return 1
}

rollback(){  # 已经动过代码：恢复原版本并重启
  local why="$1"
  status restarting "$why；正在回滚到 $FROM"
  if [ "$METHOD" = git ] && [ -n "$OLD_HEAD" ]; then
    as_user git -C "$APP_DIR" reset -q --hard "$OLD_HEAD"
  else
    as_user tar -C "$APP_DIR" -xf "$BK/code.tar"
  fi
  [ "$REQS_CHANGED" = 1 ] && install_deps
  $SYSTEMCTL restart "$SERVICE"
  if wait_version "$FROM"; then
    status rolled_back "$why；已回滚到 $FROM（备份 $BK）"
  else
    status failed "$why；回滚后服务没有报出 $FROM，请手动检查（备份 $BK）"
  fi
  exit 1
}

main(){
  mkdir -p "$UPDATE_DIR"
  if command -v flock >/dev/null; then
    exec 9>"$UPDATE_DIR/.updater.lock"
    flock -n 9 || { log "已有更新在执行"; exit 0; }
  fi
  if [ ! -f "$REQ" ] || [ ! -f "$UPDATE_DIR/ready" ]; then
    log "没有待执行的更新"
    rm -f "$UPDATE_DIR/ready"
    exit 0
  fi
  trap finish EXIT
  REQ_ID="$(json_get "$REQ" id)"
  TO="$(json_get "$REQ" version)"
  FROM="$(app_version "$APP_DIR")"
  [[ "$TO" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail_unchanged "版本号不合法：$TO"
  [ -n "$FROM" ] || fail_unchanged "读不到当前版本号（$APP_DIR/server.py）"
  TAG="v$TO"
  BK="$BACKUP_DIR/update-$FROM-to-$TO-$(date -u +%Y%m%d-%H%M%S)"
  status started "开始更新 $FROM → $TO"

  status backing_up "备份代码和数据库到 $BK"
  backup || fail_unchanged "备份失败"

  status downloading "下载 $TAG"
  local before_reqs
  before_reqs="$(reqs_hash)"
  GIT_PROBLEM=""
  fetch_git
  case $? in
    0) METHOD=git ;;
    2) fail_unchanged "$GIT_PROBLEM，不能自动更新" ;;
    *) METHOD=tarball
       fetch_tarball || rollback "下载或解压 $TAG 失败" ;;
  esac
  [ "$(app_version "$APP_DIR")" = "$TO" ] || rollback "换代码后版本号不是 $TO"

  if [ "$(reqs_hash)" != "$before_reqs" ]; then
    REQS_CHANGED=1
    status installing "依赖有变化，安装 Python 依赖"
    install_deps || rollback "安装依赖失败"
  fi

  status restarting "重启服务 $SERVICE"
  $SYSTEMCTL restart "$SERVICE" || rollback "重启服务失败"

  status verifying "等待新版本启动"
  wait_version "$TO" || rollback "重启后 ${VERIFY_SECONDS} 秒内 /health 没有报出 $TO"

  if [ -n "$AGENT_COPY" ] && [ -f "$APP_DIR/deploy/updater.sh" ]; then
    install -m 755 "$APP_DIR/deploy/updater.sh" "$AGENT_COPY" 2>/dev/null || true
  fi
  status done "已更新到 $TO（$METHOD；备份 $BK）"
}

main "$@"
