import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

STORAGE_FILE = "processed_posts.json"
LOG_FILE = "reddit_posts.log"          # 帖子日志存储路径（新增）
LOG_ENCODING = "utf-8"                 # 日志文件编码（避免中文乱码）

def load_processed_posts():
    """加载已推送的帖子ID，避免重复推送"""
    if os.path.exists(STORAGE_FILE):
        try:
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                processed_data = json.load(f)  # 格式：[{"id": "abc123", "processed_time": "2024-10-15 10:00:00"}, ...]
            # 过滤掉超过24小时的旧ID
            now = datetime.now()
            valid_data = []
            for item in processed_data:
                processed_time = datetime.strptime(item["processed_time"], "%Y-%m-%d %H:%M:%S")
                if (now - processed_time) <= timedelta(hours=24):  # 保留24小时内的ID
                    valid_data.append(item)
            # 提取有效ID列表
            processed_ids = [item["id"] for item in valid_data]
            print(f" 加载24小时内已处理ID，共 {len(processed_ids)} 条")
            return processed_ids
        except Exception as e:
            print(f" processed_posts.json 加载出错：{e}")
            return [] 
    else:
        print("无已处理ID文件，将创建新文件")
        return []

def save_processed_posts(processed_ids):
    """保存已推送的帖子ID与当前时间，用于过期清理"""
    try:
        # 先加载已有数据，避免覆盖
        existing_data = []
        if os.path.exists(STORAGE_FILE):
            with open(STORAGE_FILE, "r", encoding="utf-8") as f:
                existing_data = json.load(f)
        # 去重（避免同一ID重复存储）
        existing_ids = {item["id"] for item in existing_data}
        new_data = []   
        for post_id in processed_ids:
            if post_id not in existing_ids:
                new_data.append({
                    "id": post_id,
                    "processed_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
        # 合并并保存
        all_data = existing_data + new_data
        with open(STORAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f" 保存 processed_posts.json 出错: {e}")

def push_email(post, smtp_config):
    """邮件推送新帖子（需配置SMTP）"""
    smtp_server, smtp_port, smtp_user, smtp_pass, sender, receiver = smtp_config
    try:
        subject = f"[Reddit新帖子提醒] {post['title']}"
        body = (
            f"作者: {post['author']}\n"
            f"发布时间: {post['created_time']}\n"
            f"链接: {post['url']}\n\n"
            f"内容摘要:\n{post['content_excerpt']}\n"
        )
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = receiver
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender, receiver, msg.as_string())

        print(f" 邮件推送成功 -> {receiver}: {subject}")
    except Exception as e:
        print(f" 邮件推送失败: {e}")   

def log_post_info(post_info):
    """
    记录帖子信息到【控制台+本地日志文件】
    :param post_info: 帖子字典，需包含字段：title, author, created_time, url, content_excerpt, comment_count, top_comments
    """
    # 1. 校验必要字段（避免KeyError崩溃）
    required_fields = [
        "title", "author", "created_time", "url",
        "content_excerpt", "comment_count", "top_comments"
    ]
    missing_fields = [f for f in required_fields if f not in post_info]
    if missing_fields:
        print(f" 帖子日志记录失败： 缺少字段 {missing_fields}")
    
    # 2.构造日志内容（带时间戳）
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S") # 日志时间戳
    comment_log = ""
    # 处理评论日志（避免无评论时报错）
    if post_info["top_comments"] and isinstance(post_info["top_comments"], list):
        for i, comment in enumerate (post_info["top_comments"][:3]): # 只记录前3条评论
            # 评论字段校验
            comment_author = comment.get("author", "匿名用户")
            comment_body = comment.get("body", "无评论内容")[:300]  # 评论截断
            comment_score = comment.get("score", 0)
            comment_log += (
                f"评论{i+1}：\n "
                f"    作者：u/{comment_author}\n"
                f"    内容：{comment_body}...\n"
                f"    点赞数：{comment_score}\n"
            )
    else:
        comment_log = " 暂无有效评论\n"
    
    # 3. 日志文本格式(控制台+文件通用)
    log_text = (
        f"[{timestamp}]  Reddit 帖子日志\n"
        f"========================================\n"
        f"标题：{post_info['title'][:100]}...\n"  # 标题截断（避免换行混乱）
        f"作者：u/{post_info['author']}\n"
        f"发布时间：{post_info['created_time']}\n"
        f"直达链接：{post_info['url']}\n"
        f"评论总数：{post_info['comment_count']} 条\n"
        f"\n内容摘要：\n{post_info['content_excerpt'][:300]}...\n"  # 摘要截断
        f"\n前3条热门评论：\n{comment_log}"
        f"========================================\n\n"
    )
    # 4. 输出到控制台
    print(log_text)
    
    # 写入本地日志文件（追加模式，避免覆盖历史）
    try:
        with open(LOG_FILE, "a", encoding=LOG_ENCODING) as f:
            f.write(log_text)
        # 可选：日志文件过大时切割（比如超过10MB新建文件）
        if os.path.getsize(LOG_FILE) > 10 * 1024 * 1024:  # 10MB
            backup_log = f"reddit_posts_{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}.log"
            os.rename(LOG_FILE, backup_log)
            print(f" 日志文件超过10MB，已备份为：{backup_log}")
    except Exception as e:
        print(f" 日志写入 {LOG_FILE} 失败：{str(e)}")        
