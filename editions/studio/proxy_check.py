"""按账号代理自检：打印每个账号实际使用的出口，并按出口分组。

用法（项目根目录）：python proxy_check.py
只显示 host:port，绝不打印代理账号/密码。
"""
from pathlib import Path

import config


def main():
    accounts_dir = Path("accounts")
    if not accounts_dir.exists():
        print("没有 accounts/ 目录")
        return
    accounts = sorted(d.name for d in accounts_dir.iterdir()
                      if d.is_dir() and not d.name.startswith("."))
    groups: dict[str, list[str]] = {}
    print(f"映射文件: {config.PROXY_MAP_FILE}  "
          f"({'存在' if Path(config.PROXY_MAP_FILE).exists() else '不存在'})")
    print(f"全局回退 DOLA_PROXY: {config._parse_proxy(config.PROXY)['server'] if config.PROXY else '(无)'}")
    print("-" * 60)
    for a in accounts:
        proxy = config.proxy_for(a)
        server = proxy["server"] if proxy else "(无代理/直连)"
        groups.setdefault(server, []).append(a)
        auth = " (带认证)" if proxy and proxy.get("username") else ""
        print(f"{a:>16} -> {server}{auth}")
    print("-" * 60)
    print("按出口分组（同组 = 同一 IP，即「几号一 IP」）：")
    for server, names in sorted(groups.items()):
        print(f"  {server}: {', '.join(names)}  [{len(names)} 号]")


if __name__ == "__main__":
    main()
