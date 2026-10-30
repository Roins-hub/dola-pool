#!/bin/sh
# dola-pool 一键更新程序（Docker）。在 docker-compose.yml 的 updater 服务里运行（docker:27-cli 镜像，
# 挂 /var/run/docker.sock 和项目目录）。每 3 秒检查一次 /data/update：
#   app 等进行中的任务清空后写 ready → 把项目目录 .env 里的 DOLA_IMAGE_TAG 改成新版本
#   → 备份数据库（容器内 sqlite 在线备份到 /data/backups）→ docker compose pull + up -d dola-pool
#   → 等 /health 报出新版本号；失败就把 DOLA_IMAGE_TAG 改回原值并重新 up，回到原版本。
# 进度写 /data/update/agent-status.json（面板显示），心跳写 /data/update/agent.json。
# 注意：挂 docker.sock 等同于宿主机 root 权限；不需要一键更新可以删掉 compose 里的 updater 服务。
set -u

DATA_DIR="${DATA_DIR:-/data}"
UPDATE_DIR="$DATA_DIR/update"
APP_SERVICE="${APP_SERVICE:-dola-pool}"
APP_CONTAINER="${APP_CONTAINER:-dola-pool}"
HEALTH_URL="${HEALTH_URL:-http://dola-pool:8000/health}"
VERIFY_SECONDS="${VERIFY_SECONDS:-180}"
IMAGE_VAR="DOLA_IMAGE_TAG"

REQ_ID="" FROM="" TO="" PROJECT="" PROJECT_DIR="" DC="" PROBLEM="" OLD_TAG=""

now(){ date +%s; }
clean(){ printf '%s' "$1" | tr -d '"\\\n\r'; }

beat(){  # beat <错误说明，空=正常>
  mkdir -p "$UPDATE_DIR"
  printf '{"mode":"docker","heartbeat_at":%s,"error":"%s"}' "$(now)" "$(clean "$1")" > "$UPDATE_DIR/.agent.tmp" \
    && mv "$UPDATE_DIR/.agent.tmp" "$UPDATE_DIR/agent.json"
}

status(){  # status <state> <message>
  printf '{"id":"%s","state":"%s","message":"%s","from_version":"%s","to_version":"%s","updated_at":%s}' \
    "$REQ_ID" "$1" "$(clean "$2")" "$FROM" "$TO" "$(now)" > "$UPDATE_DIR/.status.tmp" \
    && mv "$UPDATE_DIR/.status.tmp" "$UPDATE_DIR/agent-status.json"
  echo "[updater] $1: $2"
}

json_str(){  # json_str <file> <key>：只读本程序关心的简单字段
  grep -o "\"$2\": *\"[^\"]*\"" "$1" 2>/dev/null | head -1 | sed 's/.*: *"\(.*\)"/\1/'
}

health_version(){
  wget -qO- -T 5 "$HEALTH_URL" 2>/dev/null | grep -o '"version": *"[^"]*"' | head -1 | sed 's/.*: *"\(.*\)"/\1/'
}

wait_version(){
  deadline=$(( $(now) + VERIFY_SECONDS ))
  while [ "$(now)" -lt "$deadline" ]; do
    [ "$(health_version)" = "$1" ] && return 0
    sleep 3
  done
  return 1
}

detect_project(){  # 从 app 容器的 compose 标签找项目；项目目录必须以同一路径挂进本容器
  PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$APP_CONTAINER" 2>/dev/null)"
  PROJECT_DIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$APP_CONTAINER" 2>/dev/null)"
  files="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$APP_CONTAINER" 2>/dev/null)"
  if [ -z "$PROJECT" ] || [ -z "$PROJECT_DIR" ]; then
    PROBLEM="找不到容器 $APP_CONTAINER 的 compose 标签（需要用 docker compose 启动）"; return 1
  fi
  if [ ! -d "$PROJECT_DIR" ]; then
    PROBLEM="项目目录 $PROJECT_DIR 没有以同一路径挂进 updater 容器（compose 里应为 \${PWD}:\${PWD}）"; return 1
  fi
  DC="docker compose -p $PROJECT --project-directory $PROJECT_DIR"
  old_ifs="$IFS"; IFS=','
  for f in $files; do DC="$DC -f $f"; done
  IFS="$old_ifs"
  $DC config -q 2>/dev/null || { PROBLEM="读取 compose 配置失败（$PROJECT_DIR）"; return 1; }
  PROBLEM=""
  return 0
}

set_tag(){  # set_tag <tag>：改写项目目录 .env 里的 DOLA_IMAGE_TAG（其它行原样保留）
  envf="$PROJECT_DIR/.env"
  touch "$envf"
  grep -v "^$IMAGE_VAR=" "$envf" > "$envf.tmp" || true
  echo "$IMAGE_VAR=$1" >> "$envf.tmp"
  mv "$envf.tmp" "$envf"
}

current_tag(){ grep "^$IMAGE_VAR=" "$PROJECT_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2-; }

finish(){ rm -f "$UPDATE_DIR/ready" "$UPDATE_DIR/request.json"; }

rollback(){  # rollback <原因>
  status restarting "$1；正在回滚到 $FROM"
  if [ -n "$OLD_TAG" ]; then set_tag "$OLD_TAG"; else
    grep -v "^$IMAGE_VAR=" "$PROJECT_DIR/.env" > "$PROJECT_DIR/.env.tmp"; mv "$PROJECT_DIR/.env.tmp" "$PROJECT_DIR/.env"
  fi
  $DC up -d --no-deps "$APP_SERVICE"
  if wait_version "$FROM"; then
    status rolled_back "$1；已回滚到 $FROM"
  else
    status failed "$1；回滚后服务没有报出 $FROM，请手动检查（docker compose ps / logs）"
  fi
  finish
}

run_update(){
  REQ_ID="$(json_str "$UPDATE_DIR/request.json" id)"
  TO="$(json_str "$UPDATE_DIR/request.json" version)"
  FROM="$(health_version)"
  case "$TO" in
    *[!0-9.]*|"") status failed "版本号不合法：$TO（未改动任何东西）"; finish; return ;;
  esac
  status started "开始更新 $FROM → $TO"
  detect_project || { status failed "$PROBLEM（未改动任何东西）"; finish; return; }
  OLD_TAG="$(current_tag)"

  status backing_up "备份数据库到 /data/backups"
  docker exec -i "$APP_CONTAINER" python - <<'PY' || { status failed "备份数据库失败（未改动任何东西）"; finish; return; }
import glob, os, sqlite3, time
dest = f"/data/backups/update-{time.strftime('%Y%m%d-%H%M%S')}"
os.makedirs(dest, exist_ok=True)
for path in glob.glob("/data/*.db"):
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    dst = sqlite3.connect(os.path.join(dest, os.path.basename(path)))
    src.backup(dst); dst.close(); src.close()
print(dest)
PY

  status downloading "拉取镜像 $TO"
  set_tag "$TO"
  if ! $DC config 2>/dev/null | grep -q "dola-pool:$TO"; then
    if [ -n "$OLD_TAG" ]; then set_tag "$OLD_TAG"; fi
    status failed "compose 文件的 image 没有使用 \${$IMAGE_VAR}，不能自动更新（未改动任何东西）"
    finish; return
  fi
  if ! $DC pull -q "$APP_SERVICE"; then
    if [ -n "$OLD_TAG" ]; then set_tag "$OLD_TAG"; else sed -i "/^$IMAGE_VAR=/d" "$PROJECT_DIR/.env"; fi
    status failed "拉取镜像 $TO 失败（未改动任何东西）"
    finish; return
  fi

  status restarting "用新镜像重建 $APP_SERVICE"
  $DC up -d --no-deps "$APP_SERVICE" || { rollback "重建容器失败"; return; }

  status verifying "等待新版本启动"
  wait_version "$TO" || { rollback "重启后 ${VERIFY_SECONDS} 秒内 /health 没有报出 $TO"; return; }
  status done "已更新到 $TO"
  finish
}

echo "[updater] 启动：监听 $UPDATE_DIR"
last_check=0
PROBLEM=""
while true; do
  if [ $(( $(now) - last_check )) -ge 30 ]; then
    detect_project
    last_check=$(now)
  fi
  beat "$PROBLEM"
  if [ -f "$UPDATE_DIR/ready" ] && [ -f "$UPDATE_DIR/request.json" ]; then
    run_update
    last_check=0
  fi
  sleep 3
done
