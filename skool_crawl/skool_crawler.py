import argparse
import csv
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, Browser, Page, TimeoutError as PlayTimeoutError

try:
    import openai
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

DEFAULT_STORAGE_STATE = "skool_state.json"
DEFAULT_DB = "skool_scrape.db"
DEFAULT_GROUP_HTML = "skool_{group}.html"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0 Safari/537.36"

# ---------- Utilities ----------
def ensure_dir(dirname: str):
    if dirname and not os.path.exists(dirname):
        os.makedirs(dirname, exist_ok=True)

def save_json(path: str, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def now_str():
    return datetime.now(timezone.utc).isoformat()

# ---------- Playwright login and fetch ----------
def interactive_login_and_save(storage_state_path: str = DEFAULT_STORAGE_STATE, headless: bool = False):
    print("启动 Playwright (请在打开的浏览器中完成 Skool 的登录流程)...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=USER_AGENT)
        page = ctx.new_page()
        page.goto("https://www.skool.com", timeout=60000)
        print("请在打开的浏览器窗口中完成登录，然后在命令行按回车保存 session ...")
        input("完成登录后按回车继续并保存 storage_state.json ...")
        ctx.storage_state(path = storage_state_path)
        print(f"已保存 session 到 {storage_state_path}")
        browser.close()

def fetch_group_html(storage_state: Optional[str], group_slug: str, outfile: Optional[str] = None,
                     max_scrolls: int = 30, scroll_pause: float = 1.0, headless: bool = True):
    if outfile is None:
        outfile = DEFAULT_GROUP_HTML.format(group=group_slug.replace("/", "_"))
    print(f"开始抓取群组页面: {group_slug}，storage_state={storage_state}，headless={headless}")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx_kwargs = {"user_agent": USER_AGENT}
        if storage_state and os.path.exists(storage_state):
            ctx_kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        page.goto(f"https://www.skool.com/{group_slug}", timeout=60000)

        prev_height = -1
        for i in range(max_scrolls):
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
            time.sleep(scroll_pause)
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except PlayTimeoutError:
                pass
            height = page.evaluate("document.body.scrollHeight")
            if height == prev_height:
                print(f"滚动停止：已达到高度 {height}, scroll rounds: {i+1}")
                break
            prev_height = height
            if (i+1) % 5 == 0:
                print(f"已滚动 {i+1} 次, 当前页面高度 {height}")
        html = page.content()
        with open(outfile, "w", encoding= "utf-8") as f:
            f.write(html)
        print(f"已保存渲染后 HTML 到 {outfile}")
        browser.close()
    return outfile


# ---------- Parsing list page ----------
# 对html内容进行提取
def parse_posts_from_html(html: str, group_slug: str) -> List[Dict]: # 最终输出是许多帖子的字典组成的集合
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        "div[class*='PostItemWrapper']",
        "div[class*='PostItemCardWrapper']",
        "div[class*='PostItemCardContent']",
        "div[class*='PostItem']",
        "div[class*='PostListWrapper']",
    ]
    posts = []
    found_sel = None
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            posts = nodes
            found_sel = sel
            break
    # fallback：基于 group slug 的 href 链接寻找候选 a
    fallback_anchors = []
    if not posts:
        print("not posts!找候选")
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = a["href"]
            text = a.get_text(strip=True)
            if href.startswith(f"/{group_slug}/") and not href.startswith(f"/{group_slug}?") and len(text) > 3:
                fallback_anchors.append(a)
        posts = fallback_anchors

    results = []
    for idx, node in enumerate(posts):
        if node.name == "a":
            title_tag = node
            parent = node.find_parent("div") or node
        else:
            title_tag = None
            for a in node.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if href.startswith(f"/{group_slug}/") and not href.startswith(f"/{group_slug}?") and len(text) > 3:
                    # prefer slug-like links
                    if "-" in href or "?p=" in href or "new-video" in href:
                        title_tag = a
                        break
                    if title_tag is None:
                        title_tag = a
            parent = node
        title = title_tag.get_text(" ", strip=True) if title_tag else None
        url = ("https://www.skool.com" + title_tag["href"]) if title_tag else None
        
        author = None
        avatar_img = parent.select_one("div[class*='AvatarWrapper'] img") if parent else None
        if avatar_img and avatar_img.get("alt"):
            author = avatar_img.get("alt")
        else:
            t = parent.find(attrs={"title": True}) if parent else None
            if t and len(t["title"]) < 80:
                author = t["title"]
        
        time_node = parent.select_one("[class*='PostTimeContent'], [class*='PostTime']") if parent else None
        time_text = time_node.get_text(" ", strip=True) if time_node else None

        comments = None
        cnode = parent.select_one("[class*='CommentsCount'], [class*='CommentsCount-sc-']")
        if cnode:
            m = re.search(r"(\d+)", cnode.get_text().replace(",", ""))
            if m:
                comments = int(m.group(1))

        likes = None
        lnode = parent.select_one("[class*='LikesCount']")
        if lnode:
            m = re.search(r"(\d+)", lnode.get_text().replace(",", ""))
            if m:
                likes = int(m.group(1))
        excerpt_node = parent.select_one("[class*='ContentPreviewWrapper'], [class*='ContentPreview']")
        excerpt = excerpt_node.get_text(" ", strip=True) if excerpt_node else None

        preview_url = None
        pnode = parent.select_one("div[class*='YouTubePreviewImage'], div[class*='PreviewImageWrapper']")

        if pnode:
            style = pnode.get("style", "") or ""
            m = re.search(r'url\((["\']?)(https?://[^"\')]+)\1\)', style)
            if m:
                preview_url = m.group(2)

        post_id = None
        for a in parent.find_all("a", href=True):
            if "?p=" in a["href"]:
                mm = re.search(r"[?&]p=([^&]+)", a["href"])
                if mm:
                    post_id = mm.group(1)
                    break
                
        results.append({
            "idx_in_dom": idx,
            "title": title,
            "url": url,
            "post_id": post_id,
            "author": author,
            "time": time_text,
            "comments": comments,
            "likes": likes,
            "excerpt": excerpt,
            "preview_url": preview_url,
            "raw_html_snippet": (str(parent)[:400] + "...") if parent else ""
        })

    return results

# --- 获取评论
def fetch_post_detail(storage_state: Optional[str], post_url: str, headless: bool = True, wait_for_selector: Optional[str] = None):
    # 打开单条帖子详情页并返回渲染后的 HTML
    for attempt in range(3):
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=headless,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--ignore-certificate-errors",
                        "--disable-dev-shm-usage"
                    ],
                )
                ctx_kwargs = {"user_agent": USER_AGENT}
                if storage_state and os.path.exists(storage_state):
                    ctx_kwargs["storage_state"] = storage_state
                ctx = browser.new_context(**ctx_kwargs)
                page = ctx.new_page()

                print(f" 正在打开帖子: {post_url}")
                page.goto(post_url, timeout=60000)

                # 等待页面主内容加载
                if wait_for_selector:
                    try:
                        page.wait_for_selector(wait_for_selector, timeout=8000)
                    except PlayTimeoutError:
                        print("页面主体未完全加载，继续等待评论...")
                # time.sleep(2.0)
                # 向下滚动触发异步加载
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(3)

                try:
                    page.wait_for_selector(
                        "div[class*='CommentItem'], div[class*='CommentWrapper']", timeout=15000
                    )
                    print(" 检测到评论区已加载。")
                except PlayTimeoutError:
                    print(" 未检测到评论区（可能该帖子无评论）")
                # 再次滚动，以防评论分批加载
                for _ in range(2):
                    page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    time.sleep(2)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            print(f"第 {attempt+1} 次抓取失败 ({type(e).__name__}): {e}")
            time.sleep(3)
    raise RuntimeError(f"多次抓取失败: {    post_url}")  


def parse_comments_from_post_html(html: str) -> List[Dict]:
    # 从帖子详情页的 HTML 中解析评论（尝试多种常见 class 前缀）。
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        "div[class*='CommentItem']",
        "div[class*='CommentWrapper']",
        "div[class*='Comment']",
        "li[class*='Comment']",
    ]
    comment_nodes = []
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            comment_nodes = nodes
            break
    if not comment_nodes:
        # 查找包含'class'或'text'中的'comment'的元素
        potential = [n for n in soup.find_all(True) if n.get("class") and any("comment" in c.lower() for c in " ".join(n.get("class")).split())]
        comment_nodes = potential
    results = []
    for node in comment_nodes:
        author = None
        avatar_img = node.select_one("div[class*='AvatarWrapper'] img")
        if avatar_img and avatar_img.get("alt"):
            author = avatar_img.get("alt")
        else:
            t = node.find(attrs={"title": True})
            if t and len(t["title"]) < 80:
                author = t["title"]
        # body
        body_node = node.select_one("div[class*='CommentBody'], p, div[class*='CommentText']")
        body = body_node.get_text(" ", strip=True) if body_node else node.get_text(" ", strip=True)

        # time
        time_node = node.select_one("[class*='CommentTime'], [class*='PostTime']")
        time_text = time_node.get_text(" ", strip=True) if time_node else None

        results.append({
            "author": author,
            "body": body,
            "time": time_text,
            "raw_html_snippet": (str(node)[:300] + "...")
        })
    return results


def summarize_comments_extractive(comments: List[Dict], max_sentences: int = 5) -> str:
    # 简单的抽取式摘要：把评论按长度/频次排序并拼接 top N。(或者简单用openai key)
    if not comments:
        print("该帖子没有收集到评论！")
        return ""
    
    # 优先选择最新的几条（列表假设按页面顺序是从上到下）
    top_comments = [c.get("body") for c in comments[:max_sentences] if c.get("body")]
    summary = " | ".join(top_comments)
    if len(summary) > 1000:
        return summary[:1000] + "..."
    return summary


def summarize_comments_openai(comments: List[Dict], openai_api_key: str, max_tokens: int = 300) -> str:
    # 使用 OpenAI 做评论摘要（需要 openai 包 & KEY）
    if not OPENAI_AVAILABLE:
        raise RuntimeError("请先pip install openai")
    openai.api_key = openai_api_key
    fragments = []
    for c in comments[:40]:
        author = c.get("author") or "User"
        body = c.get("body") or ""
        fragments.append(f"{author}: {body}")
    text = "\n\n".join(fragments)
    prompt = (
        "请根据下面的评论内容用中文或英文（根据输入语言）给出简短的总结（要点化）:\n\n"
        + text
        + "\n\n输出要点：1) 核心观点 2) 常见问题/请求 3) 情感倾向（积极/中性/负面）。"
    )
    resp = openai.ChatCompletion.create(
        model= "gpt-4o-mini" if "gpt-4o-mini" else "gpt-4o",
        messages = [{"role": "user", "content": prompt}],
        max_tokens = max_tokens,
        temperature = 0.2,
    )
    return resp["choices"][0]["message"]["content"].strip()

def init_db(db_path: str = DEFAULT_DB):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_slug TEXT,
        post_id TEXT,
        title TEXT,
        url TEXT,
        author TEXT,
        time TEXT,
        comments INTEGER,
        likes INTEGER,
        excerpt TEXT,
        preview_url TEXT,
        raw_html_snippet TEXT,
        last_seen TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_url TEXT,
        author TEXT,
        body TEXT,
        time TEXT,
        raw_html_snippet TEXT,
        fetched_at TEXT
    )
    """)

    conn.commit()
    conn.close()

def upsert_posts_to_db(posts: List[Dict], group_slug: str, db_path: str = DEFAULT_DB):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    for p in posts:
        key = p.get("post_id") or p.get("url")
        c.execute("SELECT id FROM posts WHERE group_slug=? AND (post_id=? OR url=?)", (group_slug, p.get("post_id"), p.get("url")))
        row = c.fetchone()
        now = now_str()
        if row: # 已经采集过，只需要进行更新
            c.execute("""
            UPDATE posts SET title=?, author=?, time=?, comments=?, likes=?, excerpt=?, preview_url=?, raw_html_snippet=?, last_seen=?
            WHERE id=?
            """, (p.get("title"), p.get("author"), p.get("time"), p.get("comments"), p.get("likes"), p.get("excerpt"), p.get("preview_url"), p.get("raw_html_snippet"), now, row[0]))
        else:
            c.execute("""
            INSERT INTO posts (group_slug, post_id, title, url, author, time, comments, likes, excerpt, preview_url, raw_html_snippet, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (group_slug, p.get("post_id"), p.get("title"), p.get("url"), p.get("author"), p.get("time"), p.get("comments"), p.get("likes"), p.get("excerpt"), p.get("preview_url"), p.get("raw_html_snippet"), now))
    conn.commit()
    conn.close()

def save_comments_to_db(post_url: str, comments: List[Dict], db_path: str = DEFAULT_DB):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    now = now_str()
    for com in comments:
        c.execute("""
        INSERT INTO comments (post_url, author, body, time, raw_html_snippet, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (post_url, com.get("author"), com.get("body"), com.get("time"), com.get("raw_html_snippet"), now))
    conn.commit()
    conn.close()


# CLI编排
def cmd_login(args):
    interactive_login_and_save(storage_state_path=args.storage_state, headless=False)

def cmd_fetch_list(args):
    # 先解析outfile
    outfile = args.outfile if getattr(args, "outfile", None) else DEFAULT_GROUP_HTML.format(group=args.group)
    outdir = os.path.dirname(outfile) or "."
    ensure_dir(outdir)
    out = fetch_group_html(
        storage_state=args.storage_state, 
        group_slug=args.group, 
        outfile=args.outfile,
        max_scrolls=args.max_scrolls, 
        scroll_pause=args.scroll_pause, 
        headless=not args.debug)
    print("列表页 HTML 已保存:", out)


def cmd_parse_list(args):
    html_path = args.html
    if not html_path or not os.path.exists(html_path):
        html_path = DEFAULT_GROUP_HTML.format(group=args.group)
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    posts = parse_posts_from_html(html, args.group)
    # 保存csv
    csv_out = args.csv or f"skool_{args.group}_posts.csv" 
    keys = ["idx_in_dom", "title", "url", "post_id", "author", "time", "comments", "likes", "excerpt", "preview_url"]
    with open(csv_out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for p in posts:
            writer.writerow({k: p.get(k) for k in keys})
    print(f"已保存 CSV 到 {csv_out}，共 {len(posts)} 条")
    # save to sqlite
    init_db(args.db)
    upsert_posts_to_db(posts, args.group, db_path=args.db)
    print(f"已写入 sqlite ({args.db})")


def cmd_fetch_details(args):
    init_db(args.db)
    # 从数据库或csv文件加载帖子
    conn = sqlite3.connect(args.db)
    c = conn.cursor()
    c.execute("SELECT url FROM posts WHERE group_slug=? ORDER BY last_seen DESC", (args.group,))
    rows = c.fetchall()
    conn.close()
    urls = [r[0] for r in rows if r[0]]
    if args.limit:
        urls = urls[:args.limit]
    print(f"将抓取 {len(urls)} 条帖子详情(并保存评论到 {args.db})")
    for url in urls:
        try:
            html = fetch_post_detail(storage_state=args.storage_state, post_url=url, headless=not args.debug)
            comments = parse_comments_from_post_html(html)
            save_comments_to_db(url, comments, db_path=args.db)
            print(f"抓取完成：{url} -> 评论 {len(comments)} 条")
            time.sleep(args.delay)
        except Exception as e:
            print("抓取详情出错：", url, e)

def cmd_summarize(args):
    init_db(args.db)
    conn = sqlite3.connect(args.db)
    c = conn.cursor()
    c.execute("SELECT DISTINCT post_url FROM comments WHERE post_url LIKE ?", (f"%{args.group}%",))
    rows = c.fetchall()
    conn.close()
    post_urls = [r[0] for r in rows]
    if args.limit:
        post_urls = post_urls[:args.limit]
    summaries = []
    for url in post_urls:
        conn = sqlite3.connect(args.db)
        c = conn.cursor()
        c.execute("SELECT author, body, time FROM comments WHERE post_url=? ORDER BY id DESC LIMIT ?", (url, args.comment_limit))
        cm_rows = c.fetchall()
        conn.close()
        comments = [{"author": r[0], "body": r[1], "time": r[2]} for r in cm_rows]
        if args.openai_key and OPENAI_AVAILABLE:
            try:
                summary = summarize_comments_openai(comments, args.openai_key)
            except Exception as e:
                print("OpenAI 摘要失败，退回到抽取式摘要:", e)
                summary = summarize_comments_extractive(comments)
        else:
            summary = summarize_comments_extractive(comments)
        summaries.append({"post_url": url, "summary": summary})
        print(f"SUMMARY for {url}:\n{summary}\n----\n")
    # save summaries
    out = args.output or f"summaries_{args.group}.json"
    save_json(out, summaries)
    print("已保存摘要到", out)    


def cmd_run_all(args):
    # 1) fetch list
    html_out = DEFAULT_GROUP_HTML.format(group=args.group)
    fetch_group_html(storage_state=args.storage_state, group_slug=args.group, outfile=html_out,
                     max_scrolls=args.max_scrolls, scroll_pause=args.scroll_pause, headless=not args.debug)
    # 2) parse list
    with open(html_out, "r", encoding="utf-8") as f:
        html = f.read()
    posts = parse_posts_from_html(html, args.group)
    init_db(args.db)
    upsert_posts_to_db(posts, args.group, db_path=args.db)
    # 3) fetch details
    urls = [p.get("url") for p in posts if p.get("url")]
    if args.limit:
        urls = urls[:args.limit]
    for url in urls:
        html = fetch_post_detail(storage_state=args.storage_state, post_url=url, headless=not args.debug)
        comments = parse_comments_from_post_html(html)
        save_comments_to_db(url, comments, db_path=args.db)
        time.sleep(args.delay)
    # 4) summarize
    if args.openai_key:
        cmd_summarize(args)


def build_argparser():
    p = argparse.ArgumentParser(description="Skool crawler: Playwright + BeautifulSoup pipeline")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("login", help="open browser to login and save storage_state.json")
    sp.add_argument("--storage_state", default=DEFAULT_STORAGE_STATE)

    sp = sub.add_parser("fetch-list", help="fetch group list page (rendered HTML)")
    sp.add_argument("--group", required=True)
    sp.add_argument("--storage_state", default=DEFAULT_STORAGE_STATE)
    sp.add_argument("--outfile", default=None)
    sp.add_argument("--max_scrolls", type=int, default=30)
    sp.add_argument("--scroll_pause", type=float, default=1.0)
    sp.add_argument("--debug", action="store_true", help="show browser")

    sp = sub.add_parser("parse-list", help="parse saved html into CSV + sqlite")
    sp.add_argument("--group", required=True)
    sp.add_argument("--html", default=None)
    sp.add_argument("--csv", default=None)
    sp.add_argument("--db", default=DEFAULT_DB)

    sp = sub.add_parser("fetch-details", help="fetch details & comments for posts in DB")
    sp.add_argument("--group", required=True)
    sp.add_argument("--storage_state", default=DEFAULT_STORAGE_STATE)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--delay", type=float, default=1.0)
    sp.add_argument("--debug", action="store_true")

    sp = sub.add_parser("summarize", help="summarize comments stored in DB")
    sp.add_argument("--group", required=True)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--comment_limit", type=int, default=40)
    sp.add_argument("--openai_key", default=None)
    sp.add_argument("--output", default=None)

    sp = sub.add_parser("run_all", help="one-shot: fetch -> parse -> fetch details -> summarize")
    sp.add_argument("--group", required=True)
    sp.add_argument("--storage_state", default=DEFAULT_STORAGE_STATE)
    sp.add_argument("--db", default=DEFAULT_DB)
    sp.add_argument("--max_scrolls", type=int, default=30)
    sp.add_argument("--scroll_pause", type=float, default=1.0)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("--delay", type=float, default=1.0)
    sp.add_argument("--comment_limit", type=int, default=40)
    sp.add_argument("--output", default=None)
    sp.add_argument("--openai_key", default=None)
    sp.add_argument("--debug", action="store_true")

    return p


def main():
    parser = build_argparser()
    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return

    if args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "fetch-list":
        cmd_fetch_list(args)
    elif args.cmd == "parse-list":
        cmd_parse_list(args)
    elif args.cmd == "fetch-details":
        cmd_fetch_details(args)
    elif args.cmd == "summarize":
        cmd_summarize(args)
    elif args.cmd == "run_all":
        cmd_run_all(args)
    else:
        print("未知命令,请按照规范指令行进行操作")

if __name__ == "__main__":
    main()