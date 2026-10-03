#!/usr/bin/env python3
"""
Kiểm tra tiến độ và tải video cho conversation_id đang render.
"""
import os
import sys
import json
from pathlib import Path

os.environ["DOLA_PURE_REGION"] = "SG"
os.environ["DOLA_PROXY"] = ""
import cf_hook
from protocol import dola_pure_api as dola

CONV_ID = sys.argv[1] if len(sys.argv) > 1 else "38417886157640209"
STATE_FILE = "user_state.json"

print(f"[*] Đang kiểm tra tiến độ video conversation: {CONV_ID}...")

out_dir = Path("./downloads")
out_dir.mkdir(exist_ok=True)
out_file = out_dir / f"video_30s_{CONV_ID}.mp4"

result = dola.fetch_or_download_conversation(
    CONV_ID,
    timeout=60,
    poll_interval=10,
    region="SG",
    download=True,
    output=str(out_file),
    remove_watermark=True,
    login_pure=True,
    login_state_file=STATE_FILE,
)

print(json.dumps(result, ensure_ascii=False, indent=2))
if result.get("status") == "succeeded":
    print(f"\n[OK] Video đã hoàn thành: {out_file.resolve()}")
