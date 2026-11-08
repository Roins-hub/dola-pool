#!/bin/sh
# 容器启动：把要持久化的东西指到 /data，再起服务。
# 数据库 / 成片 / 参考图 / 缩略图的路径已由 Dockerfile 的环境变量指到 /data；
# 账号目录（代码里写死 accounts/）和凭据加密文件（cred.key / creds.json 在代码目录旁）用软链接过去
# （目标文件还不存在也没关系：程序第一次写的时候会顺着链接写进 /data）。
set -e
mkdir -p /data/accounts /data/downloads /data/refs /data/thumbs
ln -sfn /data/accounts /app/accounts
ln -sfn /data/cred.key /app/cred.key
ln -sfn /data/creds.json /app/creds.json
cd /app
# 严格隔离镜像（Dockerfile.cloak）里有 Xvfb：先起虚拟显示器，CloakBrowser 有头运行要用；官方镜像没有，跳过
if command -v Xvfb >/dev/null 2>&1; then
  export DISPLAY="${DISPLAY:-:99}"
  Xvfb "$DISPLAY" -screen 0 1280x1024x24 -ac >/tmp/xvfb.log 2>&1 &
  sleep 1
fi
exec python -m uvicorn server:app --host 0.0.0.0 --port "${PORT:-8000}"
