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
    print(f"[{account}] 登录态: {'✓ 有效' if ok else '✗ 失效'}")


if __name__ == "__main__":
    asyncio.run(main())