import sqlite3

conn = sqlite3.connect('/Users/juy/Projects/dola-pool/dola.db')
cursor = conn.cursor()
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
print("Tables:", cursor.fetchall())

cursor.execute("SELECT * FROM proxies;")
print("Proxies:", cursor.fetchall())

cursor.execute("SELECT * FROM accounts;")
print("Accounts count:", len(cursor.fetchall()))
