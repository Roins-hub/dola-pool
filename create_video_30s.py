#!/usr/bin/env python3
"""
Script gọi Pure API tạo video 30s Seedance 2.5 qua Cloudflare Relay Singapore.
"""
import os
import sys
from pathlib import Path

# Đảm bảo Cloudflare Relay và region SG được nạp
os.environ["DOLA_PURE_REGION"] = "SG"
os.environ["DOLA_PROXY"] = ""
import cf_hook
from pure_api_gen import generate_pure_video

def translate_error(err_str: str) -> str:
    translations = [
        ("生成的视频可能涉及版权限制，请更换输入内容后重试", "[Content Flagged] Video rejected by ByteDance safety/copyright filter. Rephrase without direct combat/violence keywords."),
        ("今天的生成次数已经达到上限，明天再来免费生成吧", "[Quota Exceeded] Daily free generation limit reached for this account."),
        ("数据不存在", "Data not found / Conversation expired on server."),
        ("登录失效", "Login session expired."),
        ("出片未成功", "Video generation uncompleted"),
        ("未解析到下载 URL", "Could not resolve download URL"),
    ]
    out = err_str
    for zh, en in translations:
        out = out.replace(zh, en)
    return out

def main():
    state_file = Path("user_state.json")
    if not state_file.exists():
        print(f"[!] Không tìm thấy {state_file}")
        sys.exit(1)

    prompt = sys.argv[1] if len(sys.argv) > 1 else "A majestic drone shot of cherry blossoms blooming in a mountain valley at golden hour, cinematic lighting, 4k ultra detailed"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    ratio = sys.argv[3] if len(sys.argv) > 3 else "16:9"

    print("==================================================")
    print(" BẮT ĐẦU TẠO VIDEO 30S VỚI SEEDANCE 2.5 (PURE API)")
    print(f" Prompt: {prompt}")
    print(f" Duration: {duration}s | Ratio: {ratio}")
    print(f" Relay: Singapore Cloudflare Worker")
    print("==================================================")

    def on_conv_callback(account, conv_id, deadline):
        print(f"[+] SUBMITTED: conversation_id={conv_id}", flush=True)
        import json
        with open("latest_task.json", "w") as f:
            json.dump({"conversation_id": conv_id, "prompt": prompt, "duration": duration, "ratio": ratio}, f)

    def on_poll_callback(account, conv_id, poll_data):
        print(f"[*] Đang render (conv_id: {conv_id})...", flush=True)

    def on_balance_callback(left, source):
        print(f"[*] Quota còn lại: {left} ({source})")

    try:
        result = generate_pure_video(
            account="main_account",
            prompt=prompt,
            ratio=ratio,
            duration=duration,
            model="seedance_v2.5",
            cookie_state_path=state_file,
            output_dir=Path("./downloads"),
            region="SG",
            remove_watermark=True,
            on_conversation_id=on_conv_callback,
            on_poll=on_poll_callback,
            on_balance=on_balance_callback,
        )
        print("==================================================")
        print(" TẠO VIDEO THÀNH CÔNG!")
        print(f" File video: {result.get('local_path')}")
        local_path = result.get('local_path')
        if local_path and os.path.exists(local_path):
            p = Path(local_path)
            fhd_path = p.with_name(f"{p.stem}_1080p.mp4")
            print(f"[*] Đang upscale lên 1080p: {fhd_path}...")
            import subprocess
            subprocess.run(["ffmpeg", "-y", "-i", str(local_path), "-vf", "scale=1920:1080:flags=lanczos", "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "copy", str(fhd_path)], check=False)
            if fhd_path.exists():
                print(f"[OK] File 1080p: {fhd_path}")
        print(f" Conversation ID: {result.get('conversation_id')}")
        print(f" Credits used: {result.get('credits_used')}")
        print(f" Credits left: {result.get('credits_left')}")
        print("==================================================")
    except Exception as e:
        cleaned_msg = translate_error(str(e))
        print(f"[!] Error: {cleaned_msg}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
