"""批量导入 cookie 到号池。

为每个账号创建 accounts/<name>/ profile 目录，并把 cookie 写入
accounts/<name>/cookies.json；browser.launch_account_context 会在启动
浏览器时自动注入（patchright 的 add_cookies 不落盘，必须每次注入）。

用法:
    python import_cookies.py <cookie文件>            # 只导入
    python import_cookies.py <cookie文件> --verify   # 导入后逐个验证登录态

文件格式（每行一个账号）:
    账号名----完整cookie字符串
以 # 开头的行会被忽略。
"""
import asyncio
import json
import re
import sys
from pathlib import Path

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_cookie_string(raw: str) -> list[tuple[str, str]]:
    pairs = []
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            pairs.append((key, value))
    return pairs


def parse_file(path: str) -> list[tuple[str, str]]:
    accounts = []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "----" not in line:
            continue
        name, cookie = line.split("----", 1)
        name, cookie = name.strip(), cookie.strip()
        if not name or not cookie:
            continue
        if not NAME_RE.match(name):
            print(f"[skip] 非法账号名: {name!r}")
            continue
        accounts.append((name, cookie))
    return accounts


def import_account(name: str, cookie_str: str) -> int:
    profile = Path("accounts") / name
    profile.mkdir(parents=True, exist_ok=True)
    pairs = parse_cookie_string(cookie_str)
    payload = [{"name": k, "value": v} for k, v in pairs]
    (profile / "cookies.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(payload)


async def verify_all(accounts: list[tuple[str, str]]):
    from browser import check_login_state
    print("\n=== 验证登录态 ===")
    ok_n = 0
    unknown_n = 0
    for name, _ in accounts:
        try:
            ok = await check_login_state(name)
            if ok is None:
                unknown_n += 1
                print(f"{name}: UNKNOWN（网络/代理/超时，未改判）")
            else:
                ok_n += 1 if ok else 0
                print(f"{name}: {'OK' if ok else 'INVALID'}")
        except Exception as e:
            print(f"{name}: 异常 {type(e).__name__}: {str(e)[:120]}")
    print(f"\n有效 {ok_n}/{len(accounts)}，无法判断 {unknown_n}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    src = sys.argv[1]
    accounts = parse_file(src)
    print(f"解析到 {len(accounts)} 个账号，开始导入...")
    total = 0
    for i, (name, cookie) in enumerate(accounts, 1):
        try:
            n = import_account(name, cookie)
            total += n
            print(f"[{i}/{len(accounts)}] {name}: 写入 {n} 条 cookie")
        except Exception as e:
            print(f"[{i}/{len(accounts)}] {name}: 失败 {type(e).__name__}: {str(e)[:160]}")
    print(f"完成：{len(accounts)} 个账号，共 {total} 条 cookie -> accounts/<name>/cookies.json")
    if "--verify" in sys.argv[2:]:
        asyncio.run(verify_all(accounts))


if __name__ == "__main__":
    main()
