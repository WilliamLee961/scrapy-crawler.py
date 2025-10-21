from bs4 import BeautifulSoup
import pandas as pd
import re

PATH = "skool_ai_automation.html"

with open(PATH, "r", encoding="utf-8") as f:
    html = f.read()

soup = BeautifulSoup(html, "lxml")
# 找帖子容器（更稳健地匹配 PostItem 开头的 styled class）
posts = soup.select("div[class*='PostItemWrapper']")
if not posts:
    posts = soup.select("div[class*='PostItemCardWrapper'], div[class*='PostItemCardContent']") # 31条帖子
results = []

for post in posts:
    # 标题链接：第一个 href 以 /ai-automation-society/ 开头且看起来像帖子（排除 ?c= 等）
    title_tag = None
    for a in post.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/ai-automation-society/") and not href.startswith("/ai-automation-society?") and len(a.get_text(strip=True))>3:
            if "-" in href or "new-video" in href or "?p=" in href:
                title_tag = a
                break
            if title_tag is None:
                title_tag = a
    title = title_tag.get_text(" ", strip=True) if title_tag else None
    url = ("https://www.skool.com" + title_tag["href"]) if title_tag else None

    # 作者：优先从 avatar img 的 alt 拿
    author = None
    avatar_img = post.select_one("div[class*='AvatarWrapper'] img")
    if avatar_img and avatar_img.get("alt"):
        author = avatar_img.get("alt")

    # 时间 / 评论 / 点赞 / 摘要 / 预览图
    time_node = post.select_one("[class*='PostTimeContent'], [class*='PostTime']")
    time_text = time_node.get_text(" ", strip=True) if time_node else None

    comments_node = post.select_one("[class*='CommentsCount']")
    comments = None
    if comments_node:
        m = re.search(r"(\d+)", comments_node.get_text())
        comments = int(m.group(1)) if m else comments_node.get_text(strip=True)

    likes_node = post.select_one("[class*='LikesCount']")
    likes = None
    if likes_node:
        m = re.search(r"(\d+)", likes_node.get_text())
        likes = int(m.group(1)) if m else likes_node.get_text(strip=True)

    excerpt_node = post.select_one("[class*='ContentPreviewWrapper'], [class*='ContentPreview']")
    excerpt = excerpt_node.get_text(" ", strip=True) if excerpt_node else None

    preview_node = post.select_one("div[class*='YouTubePreviewImage']")
    preview_url = None
    if preview_node:
        style = preview_node.get("style") or ""
        m = re.search(r'url\((["\']?)(https?://[^"\')]+)\1\)', style)
        if m:
            preview_url = m.group(2)
    
    # 尝试从带 ?p= 的链接拿 post_id
    post_id = None
    for a in post.find_all("a", href=True):
        if "?p=" in a["href"]:
            m = re.search(r"[?&]p=([^&]+)", a["href"])
            if m:
                post_id = m.group(1)
                break

    results.append({
        "title": title,
        "url": url,
        "post_id": post_id,
        "author": author,
        "time": time_text,
        "comments": comments,
        "likes": likes,
        "excerpt": excerpt,
        "preview_url": preview_url
    })


df = pd.DataFrame(results)
df.to_csv("skool_posts_extracted.csv", index=False, encoding="utf-8-sig")
print("已保存 skool_posts_extracted.csv，发现帖子数：", len(df))