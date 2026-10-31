"""批量检测号池登录态，只对「明确确认失效」的号写 login_ok=0。

用法：python batch_verify.py [--concurrency N]
- 复用 browser.check_login_state 三态逻辑：
    True  = 有效   -> 写 login_ok=1
    False = 确认掉登录 -> 写 login_ok=0（死号，调度跳过）
    None  = 网络/代理/超时 -> 保持原状态，绝不误判
- 默认并发 2（对应两条 proxiware 订阅各 1 并发的硬限制，避免抢线 407）。
- 检测完打印报告 + 死号名单，不删除任何 profile（删除走面板 delete-invalid 软删除）。
"""
import asyncio
import sqlite3
import sys
import time

from browser import check_login_state


def _flag_value(name, default=None):
    for i, a in enumerate(sys.argv[1:]):
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
        if a == f"--{name}" and i + 1 < len(sys.argv[1:]):
            return sys.argv[i + 2]
    return default


def _concurrency():
    v = _flag_value("concurrency")
    return int(v) if v else 2


def _max_retries():
    v = _flag_value("retries")
    return int(v) if v else 2


async def main():
    conc = _concurrency()
    conn = sqlite3.connect("pool_usage.db")
    conn.row_factory = sqlite3.Row
    names = [r["name"] for r in conn.execute(
        "SELECT name FROM accounts_meta ORDER BY name")]
    print(f"共 {len(names)} 个账号，并发 {conc}，开始检测...", flush=True)

    sem = asyncio.Semaphore(conc)
    retries = _max_retries()
    valid, dead, unknown = [], [], []
    t0 = time.time()

    async def one(name):
        ok = None
        for attempt in range(retries + 1):
            async with sem:
                ok = await check_login_state(name)
            if ok is not None:
                break
            if attempt < retries:
                # 网络抖动是随机的，稍等后重试一次往往就成功
                await asyncio.sleep(2)
        if ok is True:
            valid.append(name)
            conn.execute(
                "UPDATE accounts_meta SET login_ok=1, login_checked_at=? WHERE name=?",
                (time.time(), name))
        elif ok is False:
            dead.append(name)
            conn.execute(
                "UPDATE accounts_meta SET login_ok=0, login_checked_at=? WHERE name=?",
                (time.time(), name))
        else:
            unknown.append(name)
        conn.commit()
        print(f"[{len(valid)+len(dead)+len(unknown)}/{len(names)}] {name}: "
              f"{'有效' if ok is True else '失效' if ok is False else '未知'}",
              flush=True)

    # 分批调度，避免一次性 gather 308 个协程
    for i in range(0, len(names), conc * 10):
        batch = names[i:i + conc * 10]
        await asyncio.gather(*(one(n) for n in batch))

    dt = time.time() - t0
    print("\n" + "=" * 50)
    print(f"检测完成，耗时 {dt:.0f}s")
    print(f"  有效:   {len(valid)}")
    print(f"  失效:   {len(dead)}  <- 死号（已写 login_ok=0）")
    print(f"  未知:   {len(unknown)}  <- 网络/代理故障，未改判")
    if dead:
        print("\n死号名单（login_ok=0）：")
        for n in sorted(dead):
            print(f"  {n}")
    if unknown:
        print(f"\n未知名单（{len(unknown)} 个，可能是 A 线网络抖动，建议稍后复测）：")
        for n in sorted(unknown):
            print(f"  {n}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
