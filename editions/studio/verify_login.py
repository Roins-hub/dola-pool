"""验证账号 profile 的登录态是否有效。

用法：python verify_login.py <账号名>
核心逻辑在 browser.check_login_state（面板「验证」按钮共用）。
"""
import asyncio
import sys

from browser import check_login_state


async def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    ok = await check_login_state(account)
    # 三态：True 有效 / False 确认失效 / None 无法判断（网络、代理、超时）
    label = "✓ 有效" if ok is True else "✗ 失效" if ok is False else "? 无法判断（网络/代理/超时，未改判）"
    print(f"[{account}] 登录态: {label}")


if __name__ == "__main__":
    asyncio.run(main())