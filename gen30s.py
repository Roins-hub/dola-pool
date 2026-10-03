#!/usr/bin/env python3
"""
CLI tạo video 30s Seedance 2.5 trên Dola qua Cloudflare Relay (Singapore).
"""
import sys
import os
import argparse
import asyncio
import aiohttp

# Nạp hook chuyển tiếp qua Cloudflare Worker
import cf_hook
from dola_client import DolaClient, parse_cookie


def load_cookie(cookie_arg: str = "") -> str:
    if cookie_arg:
        return cookie_arg.strip()
    if os.path.exists("cookies.txt"):
        with open("cookies.txt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return ""


async def run(args):
    cookie = load_cookie(args.cookie)
    if not cookie:
        print("[!] Thiếu Cookie Dola. Hãy truyền --cookie hoặc lưu vào cookies.txt")
        sys.exit(1)

    info = parse_cookie(cookie)
    if not info.get("sessionid"):
        print("[!] Cookie không hợp lệ (thiếu sessionid)")
        sys.exit(1)

    print("[*] Đã nhận cookie đăng nhập.")
    client = DolaClient(cookie)

    # 1. Test chat
    print("[1/3] Kiểm tra kết nối tài khoản (ping chat)...")
    try:
        reply = await client.chat([{"role": "user", "content": "ping"}])
        print(f"      [OK] Phản hồi từ Dola: {str(reply)[:60]}...")
    except Exception as e:
        print(f"      [LỖI] Chat thất bại: {e}")
        print("      Kiểm tra lại cookie hoặc kết nối Cloudflare Relay.")
        sys.exit(1)

    # 2. Tạo video
    print(f"[2/3] Gửi yêu cầu tạo video: Model={args.model} | Duration={args.duration}s | Ratio={args.ratio}...")
    print(f"      Prompt: \"{args.prompt}\"")
    try:
        video_url = await client.generate_video(
            prompt=args.prompt,
            ratio=args.ratio,
            duration=args.duration,
            model=args.model,
            timeout=args.timeout,
        )
        print(f"      [OK] Video đã tạo xong: {video_url}")
    except Exception as e:
        print(f"      [LỖI] Tạo video thất bại: {e}")
        sys.exit(1)

    # 3. Tải video về máy
    if args.out:
        print(f"[3/3] Đang tải video về: {args.out}...")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(video_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        with open(args.out, "wb") as f:
                            f.write(content)
                        print(f"      [OK] Đã lưu file thành công: {os.path.abspath(args.out)} ({len(content)} bytes)")
                    else:
                        print(f"      [CẢNH BÁO] Không tải được file, mã HTTP {resp.status}")
        except Exception as e:
            print(f"      [CẢNH BÁO] Lỗi khi tải file: {e}")


def main():
    parser = argparse.ArgumentParser(description="Tạo video 30s Dola Seedance 2.5")
    parser.add_argument("--cookie", type=str, default="", help="Chuỗi Cookie Dola (hoặc để trống đọc từ cookies.txt)")
    parser.add_argument("--prompt", type=str, default="A majestic cinematic drone shot of cherry blossoms blooming in a Japanese mountain valley at sunset, 4k ultra detailed", help="Nội dung prompt tạo video")
    parser.add_argument("--ratio", type=str, default="16:9", choices=["16:9", "9:16", "1:1", "4:3", "3:4"], help="Tỉ lệ khung hình")
    parser.add_argument("--duration", type=int, default=30, help="Thời lượng video (giây): 5, 10, 15, 30")
    parser.add_argument("--model", type=str, default="seedance_v2.5", help="Tên model: seedance_v2.5 hoặc seedance_v2.0")
    parser.add_argument("--out", type=str, default="output_30s.mp4", help="Đường dẫn file MP4 xuất ra")
    parser.add_argument("--timeout", type=int, default=450, help="Thời gian chờ tối đa (giây)")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
