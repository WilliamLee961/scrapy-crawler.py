import sqlite3
import pandas as pd

DB_PATH = "skool_scrape.db"

conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(posts);")
print(cursor.fetchall())
df = pd.read_sql_query("SELECT * FROM posts ORDER BY fetched_at DESC LIMIT 50;", conn)
conn.close()

print(df[["author", "time"]].head(100))