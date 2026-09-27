"""调试：打印 dola /chat/completion 的原始响应（状态码 + 前 N 字节 SSE）"""
import asyncio
import aiohttp

from dola_client import DolaClient, FAKE_HEADERS, build_query_params


async def main():
    with open("cookies.txt", encoding="utf-8") as f:
        cookie = next(l.strip() for l in f if l.strip() and not l.startswith("#"))

    client = DolaClient(cookie)
    body = client._build_video_body("一只猫追蝴蝶", "9:16", 3)

    async with aiohttp.ClientSession(trust_env=True) as session:
        url = f"{client.api_base}/chat/completion"
        headers = {
            **FAKE_HEADERS,
            "Cookie": cookie,
            "Content-Type": "application/json",
            "agw-js-conv": "str, str",
            "Referer": f"{client.api_base}/chat/",
        }
        params = build_query_params(cookie)
        async with session.post(url, params=params, json=body, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=40)) as resp:
            print("STATUS:", resp.status)
            print("CONTENT-TYPE:", resp.headers.get("content-type"))
            buf = b""
            try:
                async for chunk in resp.content:
                    buf += chunk
                    if len(buf) > 4000:
                        break
            except Exception as e:
                print("READ ERR:", e)
            print("===== RAW SSE (前4000字节) =====")
            print(buf.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    asyncio.run(main())
