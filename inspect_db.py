import sqlite3
import os

print("Files in repo:")
for f in ["pool_usage.db", "tasks.db", "dola.db"]:
    if os.path.exists(f):
        print(f" - {f}: {os.path.getsize(f)} bytes")

if os.path.exists("pool_usage.db"):
    conn = sqlite3.connect("pool_usage.db")
    c = conn.cursor()
    c.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [r[0] for r in c.fetchall()]
    print("pool_usage.db tables:", tables)
    for t in tables:
        c.execute(f"SELECT count(*) FROM {t}")
        print(f"   {t}: {c.fetchone()[0]} rows")

if os.path.exists("accounts"):
    print("accounts/ dir contents:", os.listdir("accounts"))
