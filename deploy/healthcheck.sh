#!/bin/bash
# 看门狗：/health 连续 3 次不通就重启服务（面板卡死类故障自愈）
# 由 stable-dola-pool-health.timer 每 2 分钟调用一次
set -u
STATE=/run/stable-dola-pool-health.fail
LOG=/www/wwwlogs/python/stable-dola-pool/error.log
code=$(curl -sS -o /dev/null -w '%{http_code}' -m 8 http://127.0.0.1:8000/health 2>/dev/null)
[ -n "$code" ] || code=000
if [ "$code" = "200" ]; then
  rm -f "$STATE"
  exit 0
fi
fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"
echo "[watchdog] $(date '+%F %T') /health -> $code，连续失败 $fails 次" >> "$LOG"
if [ "$fails" -ge 3 ]; then
  echo "[watchdog] 连续 3 次探测失败，重启 stable-dola-pool" >> "$LOG"
  systemctl restart stable-dola-pool
  rm -f "$STATE"
fi
