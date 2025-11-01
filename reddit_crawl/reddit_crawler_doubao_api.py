# 该python文件的任务是完成爬虫策略的api接口
import os
import time
import json
import pandas as pd
import csv
from pprint import pprint
from fastapi.responses import JSONResponse
import traceback
import threading
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import sqlite3
import sys
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.append(parent_dir)
from skool_crawl.skool_crawler_doubao import summarize_with_doubao
from volcenginesdkarkruntime import Ark 
from reddit_crawler import RedditCrawler
from notification import (
    load_processed_posts, 
    save_processed_posts,
    log_post_info
)
from anti_crawl_core import ip_pool, smart_strategy
from dotenv import load_dotenv
import mysql.connector
from mysql.connector import errorcode
# 加载上级目录的 .env
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))
MYSQL_URL = os.getenv("MYSQL_URL")
NEO4J_URI = os.getenv("NEO4J_URI")


# ---------------------- 1. 全局配置与初始化 ----------------------
# API服务配置
API_CONFIG = {
    "host": "127.0.0.1",
    "port": 8000,
    "api_key": "RedditCrawler_2024",
    "log_file_path": "reddit_posts.log",
    "max_concurrent": 100,
    "doubao_api_key": "165e659b-a12e-462d-8398-68da89fbcebb",  # 替换为实际API密钥
    "doubao_base_url": "https://ark.cn-beijing.volces.com/api/v3",  # 官方base_url
    "doubao_model": "doubao-1-5-pro-32k-250115"  # 官方指定模型
}

try:
    doubao_client = Ark(
        base_url=API_CONFIG["doubao_base_url"],
        api_key=API_CONFIG["doubao_api_key"]
    )
except Exception as e:
    print(f"豆包客户端初始化失败: {str(e)}")
    doubao_client = None

# 初始化FastApi应用
app = FastAPI(
    title="Reddit爬虫对外API服务",
    description="功能：实现爬虫状态查询、日志读取、反爬策略配置（IP池/智能策略）",
    version="1.0.0",
    default_response_class=JSONResponse,
    responses={
        200: {
            "description": "请求成功",
            "content": {
                "application/json": {
                    "charset": "utf-8",
                    "example": {
                        "code": 200,
                        "message": "操作成功",
                        "data": {}
                    }
                }
            }
        },
        400: {
            "description": "请求参数错误",
            "content": {
                "application/json": {
                    "charset": "utf-8",
                    "example": {
                        "detail": "参数错误，需至少传入一个字段"
                    }
                }
            }
        }
    }
)

# 解决跨域问题
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
NEW_DB_PATH = "reddit_test_posts.db"  # 新DB文件
NEW_CSV_PATH = "reddit_test_posts.csv"  # 新CSV文件
# ---------------------- 2. 豆包API调用工具函数（按官方文档实现） ----------------------
def normalize_posts_to_content(posts: List[Dict]) -> List[Dict]:
    """
    确保每条 post 都有统一的 'content' 字段（优先级按下列 keys）。
    同时确保有 excerpt 与 fetched_at 字段，避免空内容导致 downstream 问题。
    并打印 debug 信息用于定位问题帖。
    """
    if not posts:
        return posts

    possible_keys = ["content", "self_text", "body", "text", "self_text_html", "raw_text"]
    normalized = []
    for idx, p in enumerate(posts):
        p = dict(p)  # shallow copy 防止副作用
        found = False
        for k in possible_keys:
            val = p.get(k)
            if val and isinstance(val, str) and val.strip():
                p["content"] = val.strip()
                found = True
                break
        if not found:
            fallback = ""
            if p.get("excerpt"):
                fallback = str(p.get("excerpt"))
            elif p.get("title"):
                fallback = "标题: " + str(p.get("title"))
            elif p.get("url"):
                fallback = "链接: " + str(p.get("url"))
            else:
                fallback = ""
            p["content"] = fallback

            print(f"[normalize] post #{idx} 没有标准正文字段，已用 fallback 填充（len={len(fallback)})，原 keys: {list(p.keys())}")

        content_for_excerpt = p.get("content", "") or ""
        p["excerpt"] = p.get("excerpt") or (content_for_excerpt[:150] + "..." if len(content_for_excerpt) > 150 else content_for_excerpt)

        # fetched_at 保底
        p["fetched_at"] = p.get("fetched_at") or datetime.now().isoformat()
        normalized.append(p)
    return normalized

def get_post_summary(text: str) -> str:
    """调用豆包API获取帖子综述（严格遵循官方SDK调用方式）"""
    if not doubao_client:
        return "豆包客户端初始化失败，请检查API密钥"
    
    if not text.strip():
        return "帖子内容为空，无法生成综述"
    
    try:
        # 调用豆包官方SDK的chat.completions.create方法
        completion = doubao_client.chat.completions.create(
            model=API_CONFIG["doubao_model"],
            messages=[
                {"role": "system", "content": "你是一名中文科技文章综述专家"},
                {"role": "user", "content": text}
            ]
        )
        # 从返回结果中提取内容（按官方响应格式）
        return completion.choices[0].message.content
    except Exception as e:
        return f"豆包API调用失败: {str(e)}"

# ---------------------- 3. 爬虫状态管理 ----------------------
class CrawlerState:
    """爬虫运行状态管理（监控并发、延迟、推送响应时间，依赖反爬模块）"""
    def __init__(self):
        self.state = {
            "is_running": False,
            "current_concurrent": 0,
            "recent_crawl_delays": [],
            "last_push_response_time": None,
            "total_crawled_posts": 0,
            "total_pushed_posts": 0
        }
        self.lock = threading.Lock()
        self.crawler_thread: Optional[threading.Thread] = None
        self.crawled_results = []
        self.results_lock = threading.Lock()

    def update_crawl_delay(self, delay: float):
        """更新采集延迟（保留最近10次数据）"""
        with self.lock:
            self.state["recent_crawl_delays"].append(round(delay, 2))
            if len(self.state["recent_crawl_delays"]) > 10:
                self.state["recent_crawl_delays"].pop(0)
    
    def update_push_response_time(self, response_time: float):
        """更新推送响应时间"""
        with self.lock:
            self.state["last_push_response_time"] = round(response_time, 2)
            self.state["total_pushed_posts"] += 1
    
    def increment_concurrent(self) -> bool:
        """增加并发会话数"""
        with self.lock:
            strategy = smart_strategy.get_current_strategy()
            if self.state["current_concurrent"] < strategy["concurrent_limit"]:
                self.state["current_concurrent"] += 1
                return True
            print(f" 并发数已达上限（{strategy['concurrent_limit']}），无法新增会话")
            return False
    
    def decrement_concurrent(self):
        """减少并发会话数"""
        with self.lock:
            if self.state["current_concurrent"] > 0:
                self.state["current_concurrent"] -= 1
                if self.state["current_concurrent"] == 0:
                    print(" 当前并发会话数已减至0")

    def test_single_crawl(self) -> Dict[str, Any]:
        """手动触发一次爬取测试（汇总所有帖子正文生成整体综述）"""
        try:
            print("\n" + "=" * 60)
            print(" 手动触发单次爬取测试（整体综述版，无社群泛化）")
            print("=" * 60)

            # 获取代理与策略
            ip_status = ip_pool.get_pool_status()["statistics"]
            strategy = smart_strategy.get_current_strategy()
            PROXY_HOST = PROXY_PORT = None

            if ip_status["valid_ip_count"] > 0:
                selected_ip = ip_pool.get_random_valid_ip()
                if selected_ip:
                    ip_parts = selected_ip["ip"].split(":")
                    PROXY_HOST, PROXY_PORT = ip_parts[0], int(ip_parts[1])
                    print(f"使用代理IP: {selected_ip['ip']} ({selected_ip['protocol']})")
            else:
                print("未找到可用代理，使用直连模式")

            # 实例化 Reddit 爬虫
            crawler = RedditCrawler(proxy_host=PROXY_HOST, proxy_port=PROXY_PORT)
            time_threshold = datetime.now() - timedelta(hours=24)
            subreddit = strategy.get("target_subreddit", "python")
            print(f"开始爬取子版块：{subreddit}")

            # 执行爬取
            new_posts = crawler.get_new_posts(
                subreddit_name=subreddit,
                limit=10,
                max_comments=0,
                time_threshold=time_threshold.timestamp()
            )

            if not new_posts:
                print("⚠️ 未获取到任何帖子")
                return {
                    "success": False,
                    "crawled_count": 0,
                    "posts": [],
                    "message": f"子版块 {subreddit} 未获取到新帖子"
                }

            # 整理字段与正文
            for p in new_posts:
                p["content"] = p.get("content", "")
                p["excerpt"] = (p["content"][:150] + "...") if len(p["content"]) > 150 else p["content"]
                p["fetched_at"] = datetime.now().isoformat()

            # 保存结果（CSV + SQLite）
            save_posts_to_sqlite(new_posts)
            save_posts_to_csv(new_posts)

            # 调用 summarize_with_doubao 生成整体综述
            print(" 调用 summarize_with_doubao 生成整体中文综述...")
            summary_result = summarize_with_doubao(
                posts=new_posts,
                doubao_key=API_CONFIG["doubao_api_key"],
                model=API_CONFIG["doubao_model"]
            )
            summary_text = summary_result.get("summary", "").strip()
            if not summary_text:
                summary_text = "（综述生成失败或内容为空）"

            summary_path = f"reddit_summary_{subreddit}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(summary_text)
            print(f"帖子综述已保存到 {summary_path}")

            self.add_crawled_result(new_posts)
            result = {
                "success": True,
                "crawled_count": len(new_posts),
                "posts": new_posts,
                "summary_file": summary_path,
                "message": f"成功爬取 {len(new_posts)} 条帖子，并生成整体中文综述"
            }

            pprint("\n🎯 综合综述输出预览：")
            print(summary_text[:500] + "..." if len(summary_text) > 500 else summary_text)
            return result

        except Exception as e:
            traceback.print_exc()
            return {
                "success": False,
                "crawled_count": 0,
                "posts": [],
                "message": f"测试手动爬取失败：{str(e)}"
            }

    def start_crawler(self) -> bool:
        """启动爬虫（后台线程运行）"""
        with self.lock:
            if self.state["is_running"]:
                print(" 爬虫已经在运行，无需重复启动！")
                return False
            self.crawler_thread = threading.Thread(
                target=self._crawler_main_loop,
                daemon=True
            )
            self.crawler_thread.start()
            self.state["is_running"] = True
            print(" 爬虫已启动（后台线程）")
            return True

    def add_crawled_result(self, posts):
        with self.results_lock:
            self.crawled_results.extend(posts)
            if len(self.crawled_results) > 1000:
                self.crawled_results = self.crawled_results[-1000:]

    def get_crawled_posts(self, limit=100):
        with self.results_lock:
            return self.crawled_results[-limit:]

    def stop_crawler(self) -> bool:
        """停止爬虫"""
        with self.lock:
            if not self.state["is_running"]:
                print(" 爬虫已停止，无需重复操作")
                return False
            self.state["is_running"] = False
            if self.crawler_thread and self.crawler_thread.is_alive():
                self.crawler_thread.join(timeout=5)
                if self.crawler_thread.is_alive():
                    print("爬虫线程未及时退出，可能存在未完成任务")  
            save_processed_posts(load_processed_posts())
            print(" 爬虫已停止，已保存处理记录")
            return True
        
    def _crawler_main_loop(self):
        """后台爬虫主循环（批量爬取 + 整体综述生成）"""
        print(f"[后台爬虫] 线程已启动，进入循环（is_running: {self.state['is_running']}）")

        while self.state["is_running"]:
            print(f"\n[后台爬虫] 进入循环迭代（当前时间：{datetime.now().strftime('%H:%M:%S')}）")
            try:
                # ---------- Step 1: 初始化参数 ----------
                PROXY_HOST = PROXY_PORT = None
                ip_status = ip_pool.get_pool_status()["statistics"]

                if ip_status["valid_ip_count"] > 0:
                    if smart_strategy.need_auto_switch_ip():
                        selected_ip = ip_pool.get_random_valid_ip()
                        print(f"🔄 自动切换IP：{selected_ip['ip']}")
                    else:
                        selected_ip = ip_pool.get_current_ip() or ip_pool.get_random_valid_ip()
                        print(f"使用当前IP：{selected_ip['ip']}")
                    if selected_ip:
                        ip_parts = selected_ip["ip"].split(":")
                        PROXY_HOST, PROXY_PORT = ip_parts[0], int(ip_parts[1])
                        print(f"代理设置：{PROXY_HOST}:{PROXY_PORT}")
                else:
                    print("[后台爬虫] 无有效代理，使用直连")

                # ---------- Step 2: 检查并发限制 ----------
                if not self.increment_concurrent():
                    print("[后台爬虫] 并发已满，等待 1 秒...")
                    time.sleep(1)
                    continue
                print(f"[后台爬虫] 并发数 +1，当前并发数：{self.state['current_concurrent']}")

                # ---------- Step 3: 执行爬取 ----------
                strategy = smart_strategy.get_current_strategy()
                subreddit = strategy.get("target_subreddit", "python")
                print(f"[后台爬虫] 开始爬取子版块：{subreddit}")

                start_time = time.time()
                time_threshold = datetime.now() - timedelta(hours=24)

                crawler = RedditCrawler(proxy_host=PROXY_HOST, proxy_port=PROXY_PORT)
                new_posts = crawler.get_new_posts(
                    subreddit_name=subreddit,
                    limit=10,
                    max_comments=0,
                    time_threshold=time_threshold.timestamp()
                )

                if not new_posts:
                    print(f"[后台爬虫] 未获取到新帖子，等待 {strategy['crawl_interval']} 秒后重试")
                    self.decrement_concurrent()
                    time.sleep(strategy["crawl_interval"])
                    continue

                for p in new_posts:
                    p["content"] = p.get("content", "")
                    p["excerpt"] = (p["content"][:150] + "...") if len(p["content"]) > 150 else p["content"]
                    p["fetched_at"] = datetime.now().isoformat()

                # ---------- Step 4: 保存基础帖子 ----------
                save_posts_to_sqlite(new_posts)
                save_posts_to_csv(new_posts)
                init_mysql_table()
                save_posts_to_mysql(new_posts)

                # ---------- Step 5: 生成整体综述 ----------
                print(f"[后台爬虫] 调用 summarize_with_doubao 生成整体中文综述（{len(new_posts)} 条）...")
                summary_result = summarize_with_doubao(
                    posts=new_posts,
                    doubao_key=API_CONFIG["doubao_api_key"],
                    model=API_CONFIG["doubao_model"]
                )
                # 原要求：保存为txt格式文件
                summary_text = summary_result.get("summary", "").strip() or "（综述生成失败或为空）"
                summary_path = f"reddit_summary_{subreddit}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                with open(summary_path, "w", encoding="utf-8") as f:
                    f.write(summary_text)
                print(f" 已保存综述文件：{summary_path}")

                # 易老师新要求：保存为Markdown + Word
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                subreddit = strategy.get("target_subreddit", "reddit")
                base_name = f"reddit_summary_{subreddit}_{timestamp}"
                # 生成 Markdown 文件
                markdown_text = f"""# Reddit 子版块综合综述：{subreddit}
                生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

                ---

                ## 摘要
                {summary_text}

                ---

                ## 数据统计
                - 帖子总数：{len(new_posts)}
                - 数据来源：subreddit r/{subreddit}
                - 模型：Doubao（{API_CONFIG["doubao_model"]}）

                """
                md_path = f"{base_name}.md"
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write(markdown_text)
                print(f" 已生成 Markdown 文件：{md_path}")
                
                # 转换为 Word 文件
                try:
                    import pypandoc
                    docx_path = f"{base_name}.docx"
                    pypandoc.convert_text(markdown_text, "docx", format="md", outputfile=docx_path, extra_args=["--standalone"])
                    print(f" 已生成 Word 文件：{docx_path}")
                except Exception as e:
                    print(f" 生成 Word 文件失败：{e}")

                # ---------- Step 6: 更新运行状态 ----------
                self.add_crawled_result(new_posts)
                crawl_delay = time.time() - start_time
                self.update_crawl_delay(crawl_delay)
                self.state["total_crawled_posts"] += len(new_posts)

                print(f"[后台爬虫] 本轮完成：采集 {len(new_posts)} 条，用时 {crawl_delay:.2f}s")
                print(f"[后台爬虫] 综述摘要预览：{summary_text[:300]}...")

                # ---------- Step 7: 等待下一轮 ----------
                self.decrement_concurrent()
                interval = strategy.get("crawl_interval", 60)
                print(f"[后台爬虫] 等待 {interval} 秒进入下一轮...\n")
                time.sleep(interval)

            except Exception as e:
                traceback.print_exc()
                self.decrement_concurrent()
                print(f"[后台爬虫] 异常：{str(e)}，休眠 5 秒重试...\n")
                time.sleep(5)

    def get_current_state(self) -> Dict[str, Any]:
        """获取爬虫实时状态"""
        with self.lock:
            delays = self.state["recent_crawl_delays"]
            avg_delay = round(sum(delays)/ len(delays), 2) if delays else 0.0

            return {
                "basic_status": {
                    "is_running": self.state["is_running"],
                    "current_concurrent": self.state["current_concurrent"],
                    "total_crawled_posts": self.state["total_crawled_posts"],
                    "total_pushed_posts": self.state["total_pushed_posts"]
                },

                "performance_metrics": {
                    "recent_crawl_delays": self.state["recent_crawl_delays"],
                    "avg_crawl_delay": avg_delay,
                    "last_push_response_time": self.state["last_push_response_time"],
                    "is_crawl_delay_qualified": avg_delay <= 3,
                    "is_push_qualified": (
                        self.state["last_push_response_time"] is None
                        or self.state["last_push_response_time"] <= 120
                    )
                }
            }

# 初始化爬虫状态管理
crawler_state = CrawlerState()

def save_posts_to_csv(posts: List[Dict], csv_path: str = NEW_CSV_PATH):
    """保存帖子到CSV，字段与SQLite表完全对齐"""
    # 定义与SQLite表一致的字段列表（顺序也保持一致）
    posts = normalize_posts_to_content(posts)
    fields = [
        "id", "url", "title", "author", "time", 
        "likes", "comments", "excerpt", "content", "fetched_at"
    ]
    
    file_exists = os.path.exists(csv_path)
    
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        
        if not file_exists:
            writer.writeheader()
        
        for post in posts:
            fetched_at = datetime.now().isoformat()
            
            # 构造与SQLite字段对应的行数据
            row = {
                "id": post.get("id", ""),
                "url": post.get("url", ""),
                "title": post.get("title", ""),
                "author": post.get("author", ""),
                "time": post.get("time", "") or post.get("created_utc", ""),
                "likes": post.get("likes", post.get("score", 0)) or 0,
                "comments": post.get("comments", post.get("num_comments", 0)) or 0,
                "excerpt": post.get("excerpt", ""),
                "content": post.get("content", ""),
                "fetched_at": fetched_at
            }
            
            writer.writerow(row)
    
    if posts:
        print(f"[save_posts_to_csv] 已保存 {len(posts)} 条到 {csv_path}；第一条 content 片段：{posts[0].get('content','')[:200]}")
    else:
        print(f"[save_posts_to_csv] 无帖子可保存到 {csv_path}")


# 复用你的save_posts_to_sqlite，但默认保存到新DB
def save_posts_to_sqlite(posts: List[Dict], db_path: str = NEW_DB_PATH):
    posts = normalize_posts_to_content(posts)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        db_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reddit_id TEXT UNIQUE,
        url TEXT UNIQUE,
        title TEXT,
        author TEXT,
        time TEXT,
        likes INTEGER,
        comments INTEGER,
        excerpt TEXT,
        content TEXT,
        fetched_at TEXT
    )
    """)
    c.execute("PRAGMA table_info(posts)")
    cols = [row[1] for row in c.fetchall()]
    if "content" not in cols:
        print("[DB] 检测到旧版数据库结构，自动添加 content 列...")
        c.execute("ALTER TABLE posts ADD COLUMN content TEXT")
        conn.commit()
    for post in posts:
        c.execute("""
            INSERT OR REPLACE INTO posts 
            (reddit_id, url, title, author, time, likes, comments, excerpt, content, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
            post.get("id", ""),
            post.get("url", ""),
            post.get("title", ""),
            post.get("author", ""),
            post.get("time", "") or post.get("created_utc", ""),
            post.get("likes", post.get("score", 0)) or 0,
            post.get("comments", post.get("num_comments", 0)) or 0,
            post.get("excerpt", ""),
            post.get("content", ""),
            post.get("fetched_at", datetime.now().isoformat())
        ))
    conn.commit()
    conn.close()
    print(f"[save_posts_to_sqlite] 已保存 {len(posts)} 条到 {db_path}；第一条 content 片段：{posts[0].get('content','')[:200]}")

import pymysql
from sqlalchemy import create_engine, text

def init_mysql_table():
    """初始化 MySQL reddit_posts 表"""
    try:
        engine = create_engine(MYSQL_URL)
        with engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS reddit_posts (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    reddit_id VARCHAR(50) UNIQUE,
                    url TEXT UNIQUE,
                    title TEXT,
                    author VARCHAR(255),
                    time VARCHAR(255),
                    excerpt TEXT,
                    content LONGTEXT,
                    fetched_at VARCHAR(255)
                ) CHARACTER SET utf8mb4;
            """))
            print("[MySQL] reddit_posts 表已初始化")
    except Exception as e:
        print(f"[MySQL] 初始化失败: {e}")

def save_posts_to_mysql(posts: List[Dict]):
    """保存帖子到 MySQL 数据库 crawler_db"""
    if not MYSQL_URL:
        print("[MySQL] MYSQL_URL 未设置，跳过保存")
        return

    posts = normalize_posts_to_content(posts)
    try:
        engine = create_engine(MYSQL_URL)
        with engine.begin() as conn:
            for post in posts:
                conn.execute(text("""
                    INSERT INTO reddit_posts 
                    (reddit_id, url, title, author, time, excerpt, content, fetched_at)
                    VALUES (:reddit_id, :url, :title, :author, :time, :excerpt, :content, :fetched_at)
                    ON DUPLICATE KEY UPDATE
                        title = VALUES(title),
                        author = VALUES(author),
                        time = VALUES(time),
                        excerpt = VALUES(excerpt),
                        content = VALUES(content),
                        fetched_at = VALUES(fetched_at)
                """), {
                    "reddit_id": post.get("id", ""),
                    "url": post.get("url", ""),
                    "title": post.get("title", ""),
                    "author": post.get("author", ""),
                    "time": post.get("time", "") or post.get("created_utc", ""),
                    "excerpt": post.get("excerpt", ""),
                    "content": post.get("content", ""),
                    "fetched_at": post.get("fetched_at", datetime.now().isoformat())
                })
        print(f"[MySQL] 已保存 {len(posts)} 条帖子到 MySQL")
    except Exception as e:
        print(f"[MySQL] 保存失败: {e}")


def load_posts_from_files(db_path: str = NEW_DB_PATH, csv_path: str = NEW_CSV_PATH) -> List[Dict]:
    """从新DB或CSV读取帖子（优先读DB，DB不存在则读CSV）"""
    posts = []
    # 尝试从SQLite读取
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql_query("SELECT * FROM posts", conn)
        conn.close()
        posts = df.to_dict("records")  # 转换为字典列表
        print(f"[load_posts_from_files] 从DB读取 {len(posts)} 条帖子")
        return posts
    except Exception as e:
        print(f"[load_posts_from_files] 从DB读取失败，尝试读取CSV: {e}")
    
    # DB读取失败则从CSV读取
    try:
        df = pd.read_csv(csv_path)
        posts = df.to_dict("records")
        print(f"[load_posts_from_files] 从CSV读取 {len(posts)} 条帖子")
        return posts
    except Exception as e:
        print(f"[load_posts_from_files] 从CSV读取失败: {e}")
        return []
    
def crawl_save_and_summarize(crawler, strategy, doubao_key, 
                             new_db=NEW_DB_PATH, new_csv=NEW_CSV_PATH):
    """完整流程：爬取帖子→保存到新文件→生成综述"""
    # 步骤1：批量爬取所有新帖子（使用你修改后的get_new_posts，带时间过滤）
    try:
        print("\n===== 开始批量爬取帖子 =====")
        time_threshold = datetime.now() - timedelta(hours=24)   # 你的时间阈值（如24小时前）
        all_new_posts = crawler.get_new_posts(
            subreddit_name=strategy["target_subreddit"],
            limit=10,
            max_comments=0,
            time_threshold=time_threshold.timestamp()
        )
        all_new_posts = normalize_posts_to_content(all_new_posts)
        print(f"[crawl_save_and_summarize] 本次爬取到 {len(all_new_posts)} 条帖子。第一条 content ：{all_new_posts[0].get('content','')}")
        if not all_new_posts:
            msg = f"===== 未爬取到新帖子，流程终止（subreddit={strategy['target_subreddit']}）====="
            print(msg)
            return {
                "success": False,
                "message": msg,
                "summary": "",
                "data": []
            }
        
        # 步骤2：保存到新的DB和CSV
        print("\n===== 开始保存到新文件 =====")
        for p in all_new_posts:
            p["content"] = p.get("content", "")
            p["excerpt"] = (p["content"][:150] + "...") if len(p["content"]) > 150 else p["content"]
        save_posts_to_sqlite(all_new_posts, db_path=new_db)
        save_posts_to_csv(all_new_posts, csv_path=new_csv)
        init_mysql_table()
        save_posts_to_mysql(all_new_posts)
        
        # 步骤3：从新文件读取帖子，调用豆包生成综述
        print("\n===== 开始生成综合综述 =====")
        posts_for_summary = load_posts_from_files(db_path=new_db, csv_path=new_csv)
        if not posts_for_summary:
            msg = "===== 无帖子可生成综述 ====="
            print(msg)
            return {
                "success": False,
                "message": msg,
                "summary": "",
                "data": []
            }
        
        # 调用你的summarize_with_doubao函数生成综述
        summary_result = summarize_with_doubao(
            posts=posts_for_summary,
            doubao_key=doubao_key,
            model="doubao-1-5-pro-32k-250115"  # 使用正确的模型名
        )
        
        summary_text = summary_result.get("summary", "").strip() or "（综述生成失败或为空）"
        with open("summary_result_test.txt", "w", encoding="utf-8") as f:
            f.write(summary_text)
        print("\n===== 综述生成完成，已保存到 summary_result_test.txt =====")
        print("综述内容：\n", summary_result["summary"])

        return {
            "success": True,
            "message": f"成功爬取 {len(all_new_posts)} 条帖子并生成整体综述",
            "summary": summary_text,
            "data": all_new_posts
            }

    except Exception as e:
        error_msg = f"crawl_save_and_summarize 出错: {str(e)}"
        traceback.print_exc()
        return {
            "success": False,
            "message": error_msg,
            "summary": "",
            "data": []
        }
    
# ---------------------- 4. API鉴权 ----------------------
def verify_api_key(api_key: str = Query(..., description="API访问密钥")):
    if api_key != API_CONFIG["api_key"]:
        raise HTTPException(status_code=401, detail="无效的API密钥,请检查密钥是否正确")
    return api_key


# ---------------------- 5. API请求/响应模型 ----------------------
class IPAddRequest(BaseModel):
    ip: str
    protocol: Optional[str] = "http"

class StrategyUpdateRequest(BaseModel):
    concurrent_limit: Optional[int] = None
    crawl_interval: Optional[int] = None
    ip_switch_interval: Optional[int] = None
    retry_count: Optional[int] = None
    target_subreddit: Optional[str] = None
    max_posts_per_crawl: Optional[int] = None  # 该参数实际已被固定为10


# ---------------------- 6. API端点实现 ----------------------
@app.get("/api/crawler/state", tags=["1. 爬虫状态管理"])
def get_crawler_state_api(
    api_key: str = Depends(verify_api_key)
    ) -> Dict[str, Any]:
    return {
        "code": 200,
        "message": "success",
        "data": crawler_state.get_current_state()
    }

@app.post("/api/crawler/start", tags=["1. 爬虫状态管理"])
def start_crawler_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    success = crawler_state.start_crawler()
    if success:
        try:
            summarize_result = crawl_save_and_summarize(
                crawler=RedditCrawler(),
                strategy=smart_strategy.get_current_strategy(),
                doubao_key=API_CONFIG["doubao_api_key"]
            )
            for post in summarize_result.get("posts", []):
                for item in post["data"]:
                    item.pop("selftext", None)
            return {
                "code": 200,
                "message": f"爬虫已启动并完成摘要化处理，共处理 {len(summarize_result)} 条帖子",
                "data": summarize_result
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"爬虫启动成功，但crawl_save_and_summarize失败: {str(e)}")
    raise HTTPException(status_code=400, detail="爬虫已在进行中，无需重复启动")

@app.get("/api/crawler/results", tags=["1.爬虫状态管理"])
def get_crawler_results(
    limit: int = Query(100, description="返回结果的最大数量"),
    api_key = Depends(verify_api_key)
) -> Dict[str, Any]:
    results = crawler_state.get_crawled_posts(limit)
    return {
        "code": 200,
        "message": f"成功获取 {len(results)} 条结果",
        "data": {
            "posts": results,
            "total": len(results)
        }
    }

@app.post("/api/crawler/test-crawl", tags=["1. 爬虫状态管理"])
def test_crawl_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    test_result = crawler_state.test_single_crawl()
    if test_result["success"]:
        return {
            "code": 200,
            "message": test_result["message"],
            "data": {
                "crawled_count": test_result["crawled_count"],
                "posts": test_result["posts"],
                "container_total": len(crawler_state.crawled_results)
            }
        }
    raise HTTPException(status_code=500, detail=test_result["message"])


@app.post("/api/crawler/stop", tags=["1. 爬虫状态管理"])
def stop_crawler_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    success = crawler_state.stop_crawler()
    if success:
        return {
            "code": 200,
            "message": "爬虫已停止， 已保存处理记录",
            "data": {}
        }
    raise HTTPException(status_code=400, detail="爬虫已停止，无需重复操作")

@app.get("/api/log", tags =["2. 日志管理"])
def get_logs_api(
    start_time: Optional[datetime] = Query(None, description= "日志开始时间（如 2025-09-19T12:00:00）"),
    end_time: Optional[datetime] = Query(None, description="日志结束时间（如 2025-09-19T12:00:00）"),
    limit: int = Query(100, description="最多返回条数(<=1000)"),
    api_key: str = Depends(verify_api_key)
) ->Dict[str, Any]:
    limit = min(limit, 100)
    log_file = API_CONFIG["log_file_path"]

    if not os.path.exists(log_file):
        return {
            "code": 200,
            "message": "日志文件不存在",
            "data": {"total": 0, "logs": []}
        }

    logs = []
    with open (log_file, "r", encoding="utf-8") as f:
        current_log = ""
        for line in f:
            if line.startswith("[") and len(line) >=20:
                if current_log:
                    logs.append(current_log.strip())
                current_log = line
            else:
                current_log += line
        if current_log:
            logs.append(current_log.strip())
    
    filtered_logs = []
    for log in logs:
        if not log.startswith("["):
            continue
        log_time_str = log[1:20]
        try:
            log_time = datetime.strptime(log_time_str,"%Y-%m-%d %H:%M:%S")
        except:
            continue

        if start_time and log_time < start_time:
            continue
        if end_time and log_time > end_time :
            continue
        filtered_logs.append(
            {
                "log_time": log_time_str,
                "content": log
            }
        )

    filtered_logs.sort(key=lambda x: x["log_time"], reverse=True)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "total": len(filtered_logs),
            "limit": limit,
            "logs": filtered_logs[:limit]
        }
    }

@app.get("/api/anti-crawl/ip-pool", tags=["3. 反爬策略管理-IP池"])
def get_ip_pool_status_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    return {
        "code": 200,
        "message": "success! 成功获取反爬策略",
        "data": ip_pool.get_pool_status()
    }

@app.post("/api/anti-crawl/ip-pool/add", tags=["3.反爬策略管理-IP池"])
def add_ip_api(
    req: IPAddRequest,
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    if ":" not in req.ip:
        raise HTTPException(status_code=400, detail="IP格式错误，需为 'ip:port'（如 1.2.3.4:7891）")
    ip_parts = req.ip.split(":")
    if not ip_parts[1].isdigit():
        raise HTTPException(status_code=400, detail="IP端口必须为数字（如 1.2.3.4:7891）")
    
    success = ip_pool.add_ip(req.ip, req.protocol)
    if success:
        return {
            "code":200,
            "message": f"IP {req.ip} 添加成功（已验证有效）",
            "data": {}
        }
    raise HTTPException(status_code=400, detail=f"IP {req.ip} 无效或已在池中")

@app.post("/api/anti-crawl/ip-pool/remove", tags=["3. 反爬策略管理-IP池"])
def remove_ip_api(
    ip: str = Query(..., description="待删除IP(格式： ip:port)"),
    api_key: str = Depends(verify_api_key)
) ->  Dict[str, Any]:
    success = ip_pool.remove_ip(ip)
    if success:
        return {
            "code": 200,
            "message": f"IP {ip} 已从池中删除",
            "data": {}
        }
    raise HTTPException(status_code=404, detail=f"IP {ip} 不在池中，删除失败")

@app.get("/api/anti-crawl/strategy", tags=["3. 反爬策略管理-智能参数"])
def get_strategy_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    return {
        "code": 200,
        "message": "success!成功获取模型智能反爬策略",
        "data": smart_strategy.get_current_strategy()
    }

@app.post("/api/anti-crawl/strategy/update", tags=["3. 反爬策略管理-智能参数"])
def update_strategy_api(
    req: StrategyUpdateRequest,
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    new_params = req.model_dump(exclude_none=True)
    if not new_params:
        raise HTTPException(status_code=400, detail= "需至少传入一个待更新的策略参数")
    
    updated_strategy = smart_strategy.update_strategy(new_params)
    return {
        "code": 200,
        "message": "策略更新成功",
        "data": updated_strategy
    }

# ---------------------- 7. API服务启动入口 ----------------------
if __name__ == "__main__":
    print("="*60)
    print("         Reddit爬虫API服务启动中         ")
    print("="*60)
    print(f" API地址：http://{API_CONFIG['host']}:{API_CONFIG['port']}")
    print(f" 接口文档：http://{API_CONFIG['host']}:{API_CONFIG['port']}/docs")
    print(f" API密钥：{API_CONFIG['api_key']}（请求时需携带）")
    print(f" 豆包API状态：{'已初始化' if doubao_client else '初始化失败'}")
    print(f" 反爬模块：已加载 IP池（初始有效IP：{ip_pool.get_pool_status()['statistics']['valid_ip_count']}）")
    print("="*60)
    
    uvicorn.run(
        app="__main__:app",
        host=API_CONFIG["host"],
        port=API_CONFIG["port"],
        workers=1,
        reload=False
    )

# if __name__ == "__main__":
#     print("="*60)
#     print("          Reddit爬虫功能测试          ")
#     print("="*60)

#     # 1. 初始化测试
#     print("\n[测试1] 组件初始化检查")
#     print(f"豆包客户端状态: {'已就绪' if doubao_client else '未初始化'}")
#     print(f"初始IP池状态: {ip_pool.get_pool_status()['statistics']}")
#     print(f"初始反爬策略: {smart_strategy.get_current_strategy()}")

#     print("\n[测试1.5] crawl_save_and_summarize 完整流程测试")
#     crawl_result = crawl_save_and_summarize(
#         crawler=RedditCrawler(), 
#         strategy=smart_strategy.get_current_strategy(),
#         doubao_key=API_CONFIG["doubao_api_key"],
#     )
#     print(f"测试结果: {crawl_result['message']}")
#     if crawl_result["success"]:
#         print("第一条帖子标题:", crawl_result["data"][0]["title"])
#         print("综述片段:", crawl_result["summary"][:200])

#     # 2. 测试单次爬取功能
#     print("\n[测试2] 单次爬取测试（无代理）")
#     test_result = crawler_state.test_single_crawl()
#     pprint({
#         "爬取结果": test_result["message"],
#         "获取数量": test_result["crawled_count"]
#     })

#     # 3. 测试IP池操作
#     print("\n[测试3] IP池功能测试")
#     test_ip = "127.0.0.1:7897"
#     add_result = ip_pool.add_ip(test_ip, "http")
#     print(f"添加测试IP {test_ip}: {'成功' if add_result else '失败'}")
#     print("更新后IP池状态:", ip_pool.get_pool_status()["statistics"])
    
#     # 4. 测试反爬策略更新
#     print("\n[测试4] 反爬策略更新测试")
#     new_strategy = {
#         "concurrent_limit": 5,
#         "crawl_interval": 10,
#         "target_subreddit": "python"  # 测试用子版块
#     }
#     updated = smart_strategy.update_strategy(new_strategy)
#     print("更新后策略:", updated)

#     # 5. 测试后台爬虫运行
#     print("\n[测试5] 后台爬虫启动测试（持续15秒）")
#     start_success = crawler_state.start_crawler()
#     if start_success:
#         print("爬虫启动成功，等待15秒...")
#         for i in range(3):
#             time.sleep(5)
#             print(f"\n运行{5*(i+1)}秒后状态:")
#             pprint(crawler_state.get_current_state()["basic_status"])
        
#         # 6. 测试结果获取
#         print("\n[测试6] 爬取结果获取与存储")
#         results = crawler_state.get_crawled_posts(limit=5)
#         print(f"获取到{len(results)}条结果，准备保存到文件...")
#         if results:
#             # 保存到SQLite和CSV（与表结构对齐）
#             save_posts_to_sqlite(results)
#             save_posts_to_csv(results)
#             print(f"已将{len(results)}条结果保存到 {NEW_DB_PATH} 和 {NEW_CSV_PATH}")
#             print("第一条保存的标题:", results[0]["title"])
#         else:
#             print("无爬取结果可保存")
        

#         # 7. 停止爬虫
#         print("\n[测试7] 停止爬虫")
#         stop_success = crawler_state.stop_crawler()
#         print("爬虫停止:", "成功" if stop_success else "失败")

#     # 8. 测试数据存储
#     print("\n[测试8] 数据存储检查")
#     saved_posts = load_posts_from_files()
#     print(f"从文件加载到{len(saved_posts)}条数据")
#     if saved_posts:
#         print("存储的第一条数据URL:", saved_posts[0]["url"])
#         print("存储字段检查:", saved_posts[0].keys())

#     print("\n" + "="*60)
#     print("          所有测试执行完毕          ")
#     print("="*60)