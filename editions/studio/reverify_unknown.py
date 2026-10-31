"""复测首轮「未知」号，把它们判定清楚。"""
import asyncio
import sqlite3
import time

from browser import check_login_state

UNKNOWN = ['2026_10_05_4', '39doula1a1003', '39doula1a1006', '39doula1a1010',
           '39doula1a1009', '39doula1a1019', '39doula1a1025', '39doula1a1031',
           '39doula1a1058', '39doula1a1157', '39doula1a1182']


async def main():
    conn = sqlite3.connect("pool_usage.db")
    sem = asyncio.Semaphore(2)
    results = {}

    async def one(name):
        ok = None
        for _ in range(3):
            async with sem:
                ok = await check_login_state(name)
            if ok is not None:
                break
            await asyncio.sleep(3)
        results[name] = ok
        if ok is True:
            conn.execute(
                "UPDATE accounts_meta SET login_ok=1, login_checked_at=? WHERE name=?",
                (time.time(), name))
        elif ok is False:
            conn.execute(
                "UPDATE accounts_meta SET login_ok=0, login_checked_at=? WHERE name=?",
                (time.time(), name))
        conn.commit()
        label = "有效" if ok is True else ("失效" if ok is False else "仍未知")
        print(f"{name}: {label}", flush=True)

    await asyncio.gather(*(one(n) for n in UNKNOWN))
    conn.close()
    dead = [n for n, v in results.items() if v is False]
    still = [n for n, v in results.items() if v is None]
    print("\n=== 复测结果 ===")
    print(f"新增死号({len(dead)}): {dead}")
    print(f"仍未知({len(still)}): {still}")


if __name__ == "__main__":
    asyncio.run(main())
