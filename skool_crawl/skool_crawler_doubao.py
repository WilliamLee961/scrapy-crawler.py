# 帖子内容抓取并调用豆包1.6对最新20条帖子形成综述

# Usage example:
# python skool_crawler_doubao.py --group ai-automation-society --limit 3 --storage_state skool_state.json --output_csv skool_posts.csv --output_db skool_scrape.db --summary_out summary_doubao.json --doubao_key 165e659b-a12e-462d-8398-68da89fbcebb --debug

import os
import re
import time
import json
import sqlite3
import argparse
import requests
import pandas as pd
from typing import List, Dict, Optional
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlayTimeoutError
from volcenginesdkarkruntime import Ark

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
DEFAULT_DOUBAO_API_KEY = "165e659b-a12e-462d-8398-68da89fbcebb"
DOUBAO_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completion"

DEFAULT_DB = "skool_scrape.db"
DEFAULT_CSV = "skool_posts.csv"
DEFAULT_SUMMARY_JSON = "summary_doubao.json"

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def ensure_dir_for_file(path: str):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def fetch_group_html(storage_state: str, group_slug: str, outfile: str, headless=True, max_scrolls=10, scroll_pause=3):
    """使用 Playwright 登录后滚动加载页面"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(storage_state=storage_state)
        page = ctx.new_page()

        url = f"https://www.skool.com/{group_slug}"
        print(f"访问 {url}")
        page.goto(url, timeout=60000)

        last_height = 0
        for i in range(max_scrolls):
            page.mouse.wheel(0, 20000)
            time.sleep(scroll_pause)
            height = page.evaluate("document.body.scrollHeight")
            if height == last_height:
                print(f"滚动停止：已达到高度 {height}, scroll rounds: {i+1}")
                break
            last_height = height

        html = page.content()
        with open(outfile, "w", encoding="utf-8") as f:
            f.write(html)
        print(f" 已保存渲染后 HTML 到 {outfile}")

        browser.close()
        return html


def parse_posts_from_html(html: str, group_slug: str) -> List[Dict]:
    soup = BeautifulSoup(html, "lxml")
    selectors = [
        "div[class*='PostItemWrapper']",
        "div[class*='PostItemCardWrapper']",
        "div[class*='PostItemCardContent']",
        "div[class*='PostItem']",
        "div[class*='PostListWrapper']",
    ]
    posts = []
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            posts = nodes
            break

    if not posts:
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = a["href"]
            text = a.get_text(strip=True)
            if href.startswith(f"/{group_slug}/") and not any(skip in href for skip in ["calendar", "classroom", "members", "?c="]):
                if len(text) > 3:
                    posts.append(a)

    results = []
    for idx, node in enumerate(posts):
        is_pinned = False
        # 检查节点内是否有"置顶"或"Pinned"文本（多语言兼容）
        pinned_texts = node.find_all(string=re.compile(r"置顶|Pinned", re.I))
        if pinned_texts:
            is_pinned = True
        # 检查节点是否有置顶相关的class（如"pinned", "sticky"）
        if "class" in node.attrs:
            node_classes = " ".join(node.attrs["class"])
            if re.search(r"pinned|sticky", node_classes, re.I):
                is_pinned = True

        # 解析标题，URL等       
        if node.name == "a":
            title_tag = node
            parent = node.find_parent("div") or node
        else:
            title_tag = None
            for a in node.find_all("a", href=True):
                href = a["href"]
                text = a.get_text(strip=True)
                if href.startswith(f"/{group_slug}/") and len(text) > 3 and not any(x in href for x in ["calendar", "classroom", "members", "?c="]):
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

        time_node = parent.select_one("[class*='PostTimeContent'], [class*='PostTime']")
        time_text = time_node.get_text(" ", strip=True) if time_node else None

        excerpt_node = parent.select_one("[class*='ContentPreviewWrapper'], [class*='ContentPreview']")
        excerpt = excerpt_node.get_text(" ", strip=True) if excerpt_node else None

        results.append({
            "idx": idx,
            "title": title,
            "url": url,
            "author": author,
            "time": time_text,
            "excerpt": excerpt,
            "is_pinned": is_pinned
        })

    return results


def parse_time(text: str) -> datetime:
    """解析英文时间文本（新增支持 Jun '24 等格式），返回UTC时间"""
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    
    # 预处理：移除末尾特殊符号（空格、•、点等）
    text = text.strip()
    text = re.sub(r'[•.\s]+$', '', text)  # 清理冗余符号
    now = datetime.now(timezone.utc)
    current_year = now.year

    # 处理英文相对时间（优先级最高）
    if re.search(r"just now|moments ago", text, re.I):
        return now
    if m := re.match(r"^(\d+)\s*m$", text, re.I):  # 分钟（5m）
        return now - timedelta(minutes=int(m.group(1)))
    if m := re.match(r"^(\d+)\s*h$", text, re.I):  # 小时（3h）
        return now - timedelta(hours=int(m.group(1)))
    if m := re.match(r"^(\d+)\s*d$", text, re.I):  # 天（2d）
        return now - timedelta(days=int(m.group(1)))
    if re.search(r"yesterday", text, re.I):  # 昨天
        return now - timedelta(days=1)
    if re.search(r"today", text, re.I):  # 今天
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if m := re.match(r"^(\d+)\s*w$", text, re.I):  # 周（1w）
        return now - timedelta(weeks=int(m.group(1)))
    if m := re.match(r"^(\d+)\s*mo$", text, re.I):  # 月（6mo）
        return now - timedelta(days=int(m.group(1)) * 30)
    if m := re.match(r"^(\d+)\s*y$", text, re.I):  # 年（2y）
        return now - timedelta(days=int(m.group(1)) * 365)

    # 处理具体日期格式（新增对 Jun '24 等格式的支持）
    # 1. 月份缩写 + ' + 两位年份（如 Jun '24 → 2024年6月1日）
    if m := re.match(r"^\s*(\w{3})\s*'(\d{2})\s*$", text, re.I):  # 允许前后空格
        month_abbr, year_short = m.groups()
        try:
            # 补全年份（'24 → 2024），默认当月1日
            full_year = f"20{year_short}"
            return datetime.strptime(
                f"{month_abbr} 01 {full_year}",  # 格式：月份 日 年
                "%b %d %Y"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass  # 无效月份时跳过

    # 2. 月份缩写 + 日期（如 Aug 31 → 2024-08-31）
    if m := re.match(r"^(\w{3}) (\d{1,2})$", text):
        month_abbr, day = m.groups()
        try:
            return datetime.strptime(
                f"{month_abbr} {day} {current_year}",
                "%b %d %Y"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # 3. 月份缩写 + 日期, 年份（如 Jun 01, 2024）
    if m := re.match(r"^(\w{3}) (\d{1,2}), (\d{4})$", text):
        try:
            return datetime.strptime(text, "%b %d, %Y").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # 4. 年-月-日（如 2024-06-01）
    if m := re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        try:
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # 未匹配的格式
    print(f"⚠️ 未识别的时间格式: {text}")
    return datetime.min.replace(tzinfo=timezone.utc)


# 打开帖子的详情页，滚动触发全部正文加载，并返回正文的纯文本
def fetch_post_detail_content(post_url: str, storage_state: Optional[str] = None,
                              headless: bool = True, scroll_rounds: int = 3, scroll_pause: float = 1.2,
                              timeout_ms: int = 60000) -> str:
    """打开帖子详情页并返回渲染后的 HTML（包含正文）"""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx_kwargs = {"user_agent": USER_AGENT}
        if storage_state and os.path.exists(storage_state):
            ctx_kwargs["storage_state"] = storage_state
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        print(f"[fetch_post_detail_content] 打开 {post_url}")
        page.goto(post_url, timeout=timeout_ms)
        page.wait_for_load_state("load")

        # 滚动加载正文，注意及时停止，避开下面的评论       
        try:
            # 等待正文区域加载完成
            print("[fetch_post_detail_content] 等待正文区域加载...")
            page.wait_for_selector("[class*='PostContent'], [class*='PostBody'], article", timeout=5000)
            print("[fetch_post_detail_content] 正文区域已加载")
        except PlayTimeoutError:
            print("下下下！[fetch_post_detail_content] 警告：正文区域加载超时，可能影响内容完整性")
        # 处理多个"see more"按钮（每次只处理一个可见的）
        see_more_selector = page.locator(
            "[class*='PostContent'], [class*='PostBody'], article"
        ).get_by_text(
            re.compile(r"see more|展开|查看更多", re.IGNORECASE),
            exact=False
        )
        
        # 最多尝试点击5次（防止无限循环）
        max_clicks = 5
        click_count = 0
        while click_count < max_clicks:
            try:
                # 检查是否有可见的按钮（只看第一个匹配的）
                if see_more_selector.first.is_visible() and see_more_selector.first.is_enabled():
                    see_more_selector.first.click()  # 只点击第一个可见按钮
                    click_count += 1
                    print(f"[fetch_post_detail_content] 已点击第 {click_count} 个see more按钮")
                    time.sleep(1.5)  # 等待内容展开
                else:
                    break  # 没有可见按钮了，退出循环
            except Exception as e:
                print(f"[fetch_post_detail_content] 点击see more失败（已尝试{click_count}次）：{e}")
                break

        # 原有滚动逻辑（确保剩余内容加载）
        last_h = 0
        for _ in range(scroll_rounds):
            try:
                page.evaluate("window.scrollBy(0, document.body.scrollHeight);")
            except Exception:
                pass
            time.sleep(scroll_pause)
            try:
                page.wait_for_load_state("networkidle", timeout=2000)
            except PlayTimeoutError:
                pass
            new_h = page.evaluate("document.body.scrollHeight")
            if new_h == last_h:
                break
            last_h = new_h

        html = page.content()
        browser.close()
    return html


# 从帖子详情页 HTML 中提取正文文本。返回纯文本（去除脚本与样式）。
# 使用若干selector来找到内容区域。
def parse_post_content_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    #  移除评论相关元素（参考skool_crawler.py的评论选择器）
    comment_selectors = [
        "div[class*='CommentItem']",
        "div[class*='CommentWrapper']",
        "div[class*='Comment']",
        "li[class*='Comment']",
        "section[class*='CommentsSection']",
        "div[class*='CommentList']"
    ]
    for sel in comment_selectors:
        for elem in soup.select(sel):
            elem.decompose()
            
    # 移除其他无关元素
    for bad in soup(["script", "style", "nav", "footer", "table", "thead", "tbody", "form", "aside"]):
        bad.decompose()

    # 尝试常见selector
    content_selectors = [
        "div[class*='PostContent']",
        "div[class*='PostBody']",
        "article[class*='Post']",
        "div[class*='PostItemContent']",
        "div[class*='PostDetails']"
    ]
    content_node = None
    for sel in content_selectors:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) >80:
            content_node = node
            break

    if content_node is None:
        # 找到main下含有最大文本量的标签作为替代
        main_content = soup.find("main") or soup.body
        if main_content:
            # 找到main中最长的文本块
            candidates = []
            for tag in main_content.find_all(['div', 'section', 'article']):
                text = tag.get_text(" ", strip=True)
                if len(text) > 150 and not re.search(r"copyright|terms|policy", text, re.I):
                    candidates.append((len(text), tag))
            if candidates:
                candidates.sort(reverse=True, key=lambda x: x[0])
                content_node = candidates[0][1]  
    # 提取最终文本
    if content_node:
        text = content_node.get_text(" ", strip=True)
    else:
        text = soup.get_text(" ", strip=True)

    # 清理文本
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


# 将抓取到的文本保存为csv格式文件：
def save_posts_to_csv(posts: List[Dict], path: str):
    ensure_dir_for_file(path)
    df = pd.DataFrame(posts)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"[save_posts_to_csv] 已保存 {len(posts)} 条到 {path}")


# 将抓取到的文本保存到SQLite
def save_posts_to_sqlite(posts: List[Dict], db_path: str = DEFAULT_DB):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    for p in posts:
        now = now_iso()
        c.execute("""
        INSERT OR REPLACE INTO posts (url, title, author, time, likes, comments, excerpt, content, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (p.get("url"), p.get("title"), p.get("author"), p.get("time"),
              p.get("likes"), p.get("comments"), p.get("excerpt"), p.get("content"), now))
    conn.commit()
    conn.close()
    print(f"[save_posts_to_sqlite] 已保存 {len(posts)} 条到 {db_path}")

# 使用豆包1.6对文本进行总结
def summarize_with_doubao(posts: List[Dict], doubao_key: str,
                          model: str = "doubao-1-5-pro-32k-250115") -> Dict:
    # 将多条帖子内容合并，然后调用 Doubao API 生成中文综合摘要（主题 + 技术要点）
    if not posts:
        return {"summary": "", "raw_response": None}
    snippets = []
    for p in posts:
        content = p.get("content") or p.get("excerpt") or ""
        if not content:
            continue
        # keep each content short enough to fit in token limit
        snippets.append(p.get("title", "") + ": " + (content[:1000]))
    merged_text = "\n\n".join(snippets)
    # system_prompt = (
    #     "你是一个专业的中文技术内容总结助手。"
    # )
    user_prompt = (
        "下面是来自一个社群的多条帖子正文（已去重、按时间排序）。请基于这些内容：\n\n"
        "1) 给出一段中文的综合摘要，开头用“这些帖子主要讨论了：”并用一到两段描述主要主题；\n"
        "2) 提取关键的**技术要点**（要点化，最多 8 条，每条一句话）；\n"
        "3) 给出整体的情感/态度判断（积极/中性/负面），并简要说明依据（1-2 句）；\n\n"
        "请注意输出格式：先输出 <SUMMARY> 段（纯文本），接着输出 <KEY_POINTS> 列表（每条前有 -），最后输出 <SENTIMENT>。"
        "\n\n输入文本如下：\n\n" + merged_text
    )

    try:
        # 初始化豆包客户端
        client = Ark(
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            api_key=doubao_key
        )

        # 创建对话补全请求
        print("~~~[summarize_with_doubao] 调用 Doubao API 生成摘要（可能需要几秒）...")
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一名中文科技文章综述专家。"},
                {"role": "user", "content": user_prompt},
            ],
        )

        summary_text = completion.choices[0].message.content
        return {
            "summary": summary_text.strip(),
            "raw_response": completion.model_dump() if hasattr(completion, "model_dump") else str(completion)
        }
    
    except Exception as e:
        print(f"[summarize_with_doubao] 调用豆包失败: {e}")
        # 回退到抽取式摘要
        summary_text = _fallback_extractive_summary(posts)
        return {
            "summary": summary_text,
            "raw_response": {"error": str(e)}
        }


def _fallback_extractive_summary(posts: List[Dict]) -> str:
    # 简单抽取式：取每条前200字拼接，并做简单合并与要点抽取（词频）
    if not posts:
        return ""
    head_texts = [ (p.get("title") or "") + "：" + ( (p.get("content") or p.get("excerpt") or "")[:200] ) for p in posts[:10] ]
    merged = "\n\n".join(head_texts)
    # 生成简单要点：统计高频词（排除常见停用词）
    text_for_freq = re.sub(r"[^\w\u4e00-\u9fff]+", " ", merged.lower())
    words = text_for_freq.split()
    stop = set(["the","and","that","this","with","for","using","use","ai","is","are","to","of","in","a","on","我们","的","在","和","是","与","也","可以","通过"])
    freq = {}
    for w in words:
        if len(w) < 2: continue
        if w in stop: continue
        freq[w] = freq.get(w,0)+1
    top = sorted(freq.items(), key=lambda x:-x[1])[:8]
    key_points = [f"- {w} ({c} 次)" for w,c in top]
    summary = "这些帖子主要讨论了：" + (merged[:400] + "...") + "\n\n关键技术要点：\n" + ("\n".join(key_points) if key_points else "- 无明显高频技术关键词")
    return summary


def run(args):
    html_file = f"skool_{args.group}.html"
    html = fetch_group_html(args.storage_state, args.group, html_file, headless=not args.debug)
    posts_meta = parse_posts_from_html(html, args.group)
    for post in posts_meta:
        post["datetime"] = parse_time(post["time"])
    
    posts_meta_sorted = sorted(
        posts_meta,
        key=lambda x: x["datetime"],
        reverse=True  # 最新的排在前面
    )

    posts_meta = posts_meta_sorted[:args.limit]

    posts = []
    for idx, meta in enumerate(posts_meta):
        url = meta.get("url")
        if not url:
            print("遭啦！正文的URL找不到了呜呜呜~")
            continue
        try:
            post_html = fetch_post_detail_content(url, storage_state=args.storage_state, headless=not args.debug,
                                                  scroll_rounds=args.post_scrolls, scroll_pause=args.post_scroll_pause)
            content = parse_post_content_from_html(post_html)
            entry = dict(meta)
            entry["content"] = content
            posts.append(entry)
            print(f"[run] ({idx+1}/{len(posts_meta)}) 已抓取正文，长度 {len(content)}")
        except Exception as e:
            print(f"[run] 抓取详情失败: {url} -> {e}")
        time.sleep(args.delay_between_posts)
    if args.output_csv:
        save_posts_to_csv(posts, args.output_csv)
    if args.output_db:
        save_posts_to_sqlite(posts, args.output_db)
    
    print("[run] 开始调用 Doubao 生成综合摘要（基于抓取到的所有帖子正文）")
    summary_res = summarize_with_doubao(posts, args.doubao_key, model=args.model)
    summary_text = summary_res.get("summary") if isinstance(summary_res, dict) else str(summary_res)
    raw = summary_res.get("raw_response") if isinstance(summary_res, dict) else None
    summary_obj = {
        "generated_at": now_iso(),
        "group": args.group,
        "limit": args.limit,
        "summary": summary_text,
        "raw_response": raw
    }

    with open(args.summary_out, "w", encoding="utf-8") as f:
        json.dump(summary_obj, f, ensure_ascii=False, indent=2)
    print(f"[run] 已保存综合摘要到 {args.summary_out}")

    # 易老师要求1：保存为 Markdown 文件
    from textwrap import dedent
    md_path = args.summary_out.replace(".json", ".md")

    markdown_text = dedent(f"""
    # 🧭 Skool 社群摘要：{args.group}
    生成时间：{summary_obj['generated_at']}

    ---

    ## 摘要
    {summary_obj['summary']}

    ---

    ## 原始数据
    - 帖子数量：{len(posts)}
    - 来源数据库：{args.output_db or '无'}
    - Doubao 模型：{args.model}
    """)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(markdown_text)

    print(f"[run] 已保存 Markdown 摘要到 {md_path}")

    # 易老师要求2：保存为 Word 文件
    import pypandoc

    docx_path = args.summary_out.replace(".json", ".docx")
    pypandoc.download_pandoc()
    pypandoc.convert_text(markdown_text, "docx", format="md", outputfile=docx_path, extra_args=["--standalone"])
    print(f"[run] 已保存 Word 文件到 {docx_path}")
    # 打印预览
    preview = summary_text[:3000] if summary_text else ""
    print("---- 豆包摘要预览（最多3000字符） ----")
    print(preview)
    print("---- 结束 ----")

def build_parser():
    p = argparse.ArgumentParser(description="Skool 爬虫 + Doubao 综合摘要")
    p.add_argument("--group", required=True, help="skool group slug, e.g. ai-automation-society")
    p.add_argument("--limit", type=int, default=20, help="要抓取的最新帖子数量")
    p.add_argument("--storage_state", default="skool_state.json", help="Playwright storage_state.json")
    p.add_argument("--output_csv", default=DEFAULT_CSV, help="输出 CSV 路径（设为空不保存）")
    p.add_argument("--output_db", default=DEFAULT_DB, help="输出 SQLite DB 路径（设为空不保存）")
    p.add_argument("--summary_out", default=DEFAULT_SUMMARY_JSON, help="摘要输出 JSON 路径")
    p.add_argument("--doubao_key", default=DEFAULT_DOUBAO_API_KEY, help="Doubao API Key")
    p.add_argument("--model", default="doubao-1-5-pro-32k-250115", help="Doubao 模型名（若无权限会回退）")
    p.add_argument("--max_scrolls", type=int, default=30, help="列表页最大滚动次数")
    p.add_argument("--scroll_pause", type=float, default=1.0, help="列表页滚动间隔秒")
    p.add_argument("--post_scrolls", type=int, default=8, help="详情页滚动次数")
    p.add_argument("--post_scroll_pause", type=float, default=1.2, help="详情页滚动间隔秒")
    p.add_argument("--delay_between_posts", dest="delay_between_posts", type=float, default=1.2, help="抓取详情间延迟")
    p.add_argument("--debug", action="store_true", help="显示浏览器（调试）")
    return p

if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()

    if args.output_csv == "":
        args.output_csv = None
    if args.output_db == "":
        args.output_db = None

    run(args)
    
