import sqlite3

DB_PATH = "skool_scrape.db"  # 确保路径和你的数据库一致

# 连接数据库
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()

# 查询数据库中所有的表（SQLite系统表sqlite_master存储表信息）
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()

# 打印所有表名
print("数据库中的表：")
for table in tables:
    print(f"- {table[0]}")  # table是元组，取第一个元素即表名

conn.close()