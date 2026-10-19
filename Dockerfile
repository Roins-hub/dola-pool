# dola-pool Docker 镜像：只跑纯 API（Cookie 导入的 dola / 豆包号），不装浏览器。
# 数据（账号、数据库、成片、参考图）全部在 /data，挂成数据卷，升级镜像不丢。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    DOLA_DB_PATH=/data/tasks.db \
    DOLA_POOL_DB_PATH=/data/pool_usage.db \
    DOLA_DOWNLOAD_DIR=/data/downloads \
    DOLA_REFERENCE_FILE_DIR=/data/refs \
    DOLA_THUMB_DIR=/data/thumbs \
    DOLA_PURE_API=1

# node：bdms 签名器（需 16+，bookworm 自带 18）；ffmpeg：成片转码 / ffprobe 复核时长
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs ffmpeg ca-certificates curl libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt deploy-extra-requirements.txt ./
RUN pip install -r requirements.txt -r deploy-extra-requirements.txt

COPY . .
RUN chmod +x docker/entrypoint.sh

EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1
ENTRYPOINT ["/app/docker/entrypoint.sh"]
