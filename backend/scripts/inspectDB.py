import sqlite3
conn = sqlite3.connect("data/corpus.db")
cur = conn.cursor()

for table in ['sentences', 'articles', 'structured_stats']:
    print(f"\n=== {table} ===")
    cur.execute(f"PRAGMA table_info({table})")
    print(cur.fetchall())
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    print("Row count:", cur.fetchone())
    cur.execute(f"SELECT * FROM {table} LIMIT 2")
    print("Sample:", cur.fetchall())