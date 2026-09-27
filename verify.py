"""dola cookie 验证 + 视频实测工具。

用法：
  python verify.py "完整cookie字符串"    # 直接传 cookie
  python verify.py                        # 从 cookies.txt 读第一个非注释行

前置：环境变量 HTTPS_PROXY 需指向 JP/KR 出口代理（dola.com 地区墙）
"""
import os
import sys
import asyncio

from dola_client import DolaClient


def _load_cookie(argv) -> str:
    if len(argv) > 1:
        return argv[1]
    if os.path.exists("cookies.txt"):
        with open("cookies.txt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return ""


async def main():
    cookie = _load_cookie(sys.argv)
    if not cookie:
        print("未提供 cookie。用法：python verify.py \"<完整cookie>\" 或填 cookies.txt")
        return

    client = DolaClient(cookie)

    print("[1/2] 验证 cookie（chat 探活）...")
    try:
        reply = await client.chat([{"role": "user", "content": "用一句话介绍你自己"}])
        print(f"      chat OK -> {str(reply)[:100]}")
    except Exception as e:
        print(f"      chat FAIL -> {type(e).__name__}: {e}")
        print("      cookie 可能失效或代理不通，请检查。")
        return

    print("[2/2] 视频实测（seedance_v2.0 / 3秒 / 9:16）...")
    try:
        url = await client.generate_video("一只猫在草地上追蝴蝶", ratio="9:16", duration=3)
        print(f"      video OK -> {url}")
    except Exception as e:
        print(f"      video FAIL -> {type(e).__name__}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
