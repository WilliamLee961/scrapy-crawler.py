import time
import praw
import os
import requests
import socket
import traceback
from notification import save_processed_posts,load_processed_posts, log_post_info
from datetime import datetime
CLIENT_ID = "6Jyz3PZ9DypeoxFr6DDuOw"
CLIENT_SECRET = "dyWltRMyrGihD09tydBaBRsk_o9U2Q"
# Reddit 推荐的 UA 格式：script:appname:version (by /u/username)
USER_AGENT = "script:my_reddit_crawler:0.1 (by /u/Important_March1134)"
USERNAME = ""
PASSWORD = ""

# PROXY_HOST = "127.0.0.1"
# PROXY_PORT = 7897
# PROXY_PROTOCOLS = ["http"]  # 自动尝试


def test_proxy_connectivity(host, port):
    """测试代理端口是否可达"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            result = s.connect_ex((host, port))
            if result == 0:
                print(f"代理端口 {host}:{port} 可达")
                return True
            else:
                print(f"代理端口 {host}:{port} 不可达 (connect_ex={result})")
                return False
    except Exception as e:
        print(f"代理端口连接失败：{str(e)}")
        return False


def check_reddit_domain_access(proxy_url):
    """检查 Reddit 域名可访问性"""
    test_urls = ["https://www.reddit.com", "https://oauth.reddit.com"]
    ok = True
    for url in test_urls:
        try:
            response = requests.get(
                url,
                proxies={"http": proxy_url, "https": proxy_url},
                timeout=10,
                allow_redirects=True,
                headers= {"User-Agent": USER_AGENT} 
            )
            print(f"成功访问 {url} via {proxy_url}，状态码：{response.status_code}")
        except Exception as e:
            print(f"无法访问 {url} via {proxy_url}：{str(e)}")
            ok = False
    return ok


class RedditCrawler:
    def __init__(self, proxy_host = None, proxy_port = None):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxies = None  # 存储最终的代理配置
        self.reddit = None   # PRAW Reddit 实例
        # 支持的代理协议列表（根据实际需求调整，这里仅保留 HTTP）
        self.supported_protocols = ["http"]  # 若需加 SOCKS5，可改为 ["http", "socks5"]
        try:
            if self.proxy_host and self.proxy_port:
                # 修改：将 PROXY_HOST/PROXY_PORT 改为 self.proxy_host/self.proxy_port
                if not test_proxy_connectivity(self.proxy_host, self.proxy_port):
                    raise ConnectionError("代理端口不可用，请检查代理是否启动")
                # 2.2 循环检测支持的协议，找到第一个可用的协议
                self._init_with_proxy()
            else:
                # 未传入代理
                print("警告！未使用代理，可能无法访问Reddit.尝试直接连接Reddit(可能因网络问题失败)")
                self._init_without_proxy()

            # 3. 验证Reddit 实力是否初始化成功
            if not self.reddit:
                raise ConnectionError("Reddit 实例初始化失败，无法继续爬取")
            # 简单验证：获取一个子版块标题，确认连接有效
            test_sub = self.reddit.subreddit("python")
            print(f"Reddit 实例初始化成功！测试子版块：{test_sub.title}")

        except Exception as e:
            print(f"初始化失败：{str(e)}")
            traceback.print_exc()
            raise # 重新抛出异常，让上层调用者感知错误


    def _init_with_proxy(self):
        """有代理时的初始化逻辑（独立函数，代码更清晰）"""
        success = False
        last_error = None

        # 循环检测支持的协议（如 HTTP）
        for proto in self.supported_protocols:
            # 构造当前协议的代理 URL
            proxy_url = f"{proto}://{self.proxy_host}:{self.proxy_port}"
            print(f"\n--- 尝试使用 {proto} 协议（代理：{proxy_url}）---")

            # 3. 检测代理是否能访问 Reddit 域名
            if not check_reddit_domain_access(proxy_url):
                print(f" {proto} 协议无法访问 Reddit，跳过")
                continue

            # 4. 初始化 PRAW（关键：用 requestor_kwargs 传入代理，替代环境变量）
            try:
                # 设置新的代理环境变量（覆盖 HTTP/HTTPS 流量）
                os.environ["HTTP_PROXY"] = proxy_url
                os.environ["HTTPS_PROXY"] = proxy_url
                os.environ["ALL_PROXY"] = proxy_url
                print(f" 已设置环境变量代理：{proxy_url}")

                self.reddit = praw.Reddit(
                    client_id=CLIENT_ID,
                    client_secret=CLIENT_SECRET,
                    user_agent=USER_AGENT,
                    username=USERNAME,
                    password=PASSWORD,
                    timeout=15  # 延长超时时间，避免代理延迟导致失败
                )
                # 验证 PRAW 代理是否生效（获取当前 IP，可选）
                self._verify_proxy_effective(proxy_url)
                success = True
                self.proxies = {"http": proxy_url, "https": proxy_url}  # 保存可用的代理配置
                print(f"{proto} 协议代理初始化成功")
                break  # 找到可用协议，退出循环

            except Exception as e:
                last_error = e
                print(f"❌ {proto} 协议初始化 PRAW 失败：{str(e)[:60]}")
                traceback.print_exc()

        # 若所有协议都失败，抛出异常
        if not success:
            raise ConnectionError(f"所有代理协议均失败，最后错误：{str(last_error)[:80]}")


    def _init_without_proxy(self):
        """无代理时的初始化逻辑（独立函数，避免代码混乱）"""
        try:
            self.reddit = praw.Reddit(
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                user_agent=USER_AGENT,
                username=USERNAME,
                password=PASSWORD,
                timeout=15
            )
            print("无代理模式初始化 PRAW 成功")
        except Exception as e:
            raise ConnectionError(f"无代理连接 Reddit 失败：{str(e)}")  
    
    def _verify_proxy_effective(self, proxy_url):
        """可选：验证代理是否真的生效（通过获取当前 IP 确认）"""
        # 如果不生效，通过check_url应该得道：{"ip":"111.250.4.84"}
        try: 
            # 调用 IP 查询接口，确认出口 IP 是代理 IP（非本地 IP）
            ip_check_url = "https://api.ipify.org?format=json"
            response = requests.get(
                ip_check_url,
                proxies= {"http": proxy_url, "https": proxy_url},
                timeout=10,
                headers={"User-Agent": USER_AGENT}
            )
            current_ip = response.json()["ip"]
            print(f"当前代理出口 IP：{current_ip}（确认是否为预期代理 IP）")
        except Exception as e:
            print(f"代理 IP 验证失败（不影响爬取）：{str(e)}")

    def _format_post_info(self, post, max_comments=0):
        """
        统一并兼容化帖子字段：
        返回字段包含（尽量满足上游保存函数的期待）：
        - id, title, author, likes, comments, time, created_utc, url,
        excerpt, content, top_comments (list)
        兼容旧字段名（score -> likes, num_comments -> comments, content/selftext）
        """
        try:
            # 获取基础属性（防止属性为 None）
            created_utc = getattr(post, "created_utc", None) or 0
            title = getattr(post, "title", "") or ""
            author_obj = getattr(post, "author", None)
            author_name = author_obj.name if getattr(author_obj, "name", None) else (str(author_obj) if author_obj else "[作者已删除]")
            score = getattr(post, "score", 0) or 0
            num_comments = getattr(post, "num_comments", 0) or 0
            url = getattr(post, "url", "") or ""
            # selftext may be '' (empty) or None; ensure string
            raw_selftext = getattr(post, "selftext", "")
            if raw_selftext is None:
                raw_selftext = ""
            raw_selftext = str(raw_selftext)

            # 组织返回字典（兼容多处使用的字段名）
            post_info = {
                "id": getattr(post, "id", "") or "",
                "title": title,
                "author": author_name,
                "likes": int(score),
                "comments": int(num_comments),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(created_utc)) if created_utc else "",
                "created_utc": created_utc,
                "url": url,
                # 保留短摘录（兼容你原来的 content_excerpt）
                "excerpt": (raw_selftext[:150] + "...") if len(raw_selftext) > 150 else raw_selftext,
                # 统一正文字段名为 content（也是 normalize 函数会优先查找的字段）
                "content": raw_selftext,
                # 兼容老代码中可能查找 selftext 的场景
                "selftext": raw_selftext,
                "top_comments": []
            }

            # 爬取顶级评论（如果需要）
            # try:
            #     post.comments.replace_more(limit=0)
            #     for comment in post.comments.list()[:max_comments]:
            #         if hasattr(comment, 'body'):
            #             c_author = comment.author.name if getattr(comment.author, "name", None) else (str(comment.author) if comment.author else "[已删除]")
            #             c_body = comment.body or ""
            #             post_info["top_comments"].append({
            #                 "author": c_author,
            #                 "body": (c_body[:300] + "...") if len(c_body) > 300 else c_body,
            #                 "score": getattr(comment, "score", 0) or 0,
            #                 "created_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(getattr(comment, "created_utc", 0)))
            #             })
            # except Exception as e:
            #     print(f"⚠️ 获取评论出错（忽略）：{e}")

            # Debug warn 如果正文为空（便于定位为什么content为空）
            if not post_info["content"].strip():
                print(f"[WARN] 帖子 content 为空：id={post_info['id']} title={post_info['title'][:60]} url={post_info['url']}")

            return post_info

        except Exception as e:
            print(f"[ERROR][_format_post_info] 组织 post_info 失败: {e}")
            traceback.print_exc()
            # 返回至少包含关键字段的空结构，避免上游报 KeyError
            return {
                "id": getattr(post, "id", "") or "",
                "title": getattr(post, "title", "") or "",
                "author": getattr(post, "author", "") or "",
                "likes": getattr(post, "score", 0) or 0,
                "comments": getattr(post, "num_comments", 0) or 0,
                "time": "",
                "created_utc": getattr(post, "created_utc", 0),
                "url": getattr(post, "url", "") or "",
                "excerpt": "",
                "content": "",
                "selftext": "",
                "top_comments": []
            }
    # def _format_post_info(self, post, max_comments):
    #     """复用帖子格式化逻辑（减少重复代码）"""
    #     # 基础帖子信息
    #     # 1. 提取原始时间戳（关键：用于排序）
    #     created_utc = post.created_utc  # Reddit帖子对象原生包含的Unix时间戳
    #     post_info = {
    #         "id": post.id,  # 新增 ID，用于去重
    #         "title": post.title,
    #         "author": post.author.name if post.author else "[作者已删除]",
    #         "score": post.score,
    #         "created_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(post.created_utc)),
    #         "created_utc": created_utc,
    #         "url": post.url,
    #         "comment_count": post.num_comments,
    #         "content_excerpt": post.selftext[:1000] + "..." if len(post.selftext) > 1000 else post.selftext,
    #         "top_comments": []
    #     }
    #     # 爬取顶级评论
    #     try:
    #         post.comments.replace_more(limit=0)  # 禁用“加载更多评论”
    #         for comment in post.comments.list()[:max_comments]:
    #             if hasattr(comment, 'body'):
    #                 post_info["top_comments"].append({
    #                     "author": comment.author.name if comment.author else "[已删除]",
    #                     "body": comment.body[:300] + "..." if len(comment.body) > 300 else comment.body,
    #                     "score": comment.score,
    #                     "created_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(comment.created_utc)),
    #                 })
    #     except Exception as e:
    #         print(f"⚠️ 获取评论出错：{str(e)}（跳过该帖子评论）")
    #     return post_info


    def crawl_hot_posts(self, subreddit_name='python', limit=5, max_comments=3):
        """爬取指定子版块的「热门帖子」（遵循 Reddit API 速率限制）"""
        print(f"\n 开始爬取 /r/{subreddit_name} 的热门帖子（最多 {limit} 个）...")
        subreddit = self.reddit.subreddit(subreddit_name)
        collected_posts = []
        try:
            # 通过 .hot() 获取热门排序的帖子，limit 控制数量
            for idx, post in enumerate(subreddit.hot(limit=limit), 1):
                post_info = self._format_post_info(post, max_comments)
                collected_posts.append(post_info)
                f"{idx}. 标题：{post.title}（发布时间：{post_info['time']}）"
            print(f"成功爬取 {len(collected_posts)} 个热门帖子 from /r/{subreddit_name}")
            return collected_posts
        except Exception as e:
            print(f"爬取热门帖子时出错: {e}")
            traceback.print_exc()
            return []
        
    def get_new_posts(self, subreddit_name="python", limit=5, max_comments=3, time_threshold=None):
        """获取最新帖子"""
        subreddit = self.reddit.subreddit(subreddit_name)
        collected_posts = []
        try:
            for idx, post in enumerate(subreddit.new(limit=limit), 1): 
                post_created_utc = getattr(post, "created_utc", 0)

                # 时间过滤：如果设置了time_threshold，只保留晚于该时间的帖子
                if time_threshold is not None and post_created_utc <= time_threshold:
                    print(f"已过滤到时间阈值({datetime.fromtimestamp(time_threshold)})之前的帖子，停止收集")
                    break
                post_info = self._format_post_info(post, max_comments)
                post_info = self._normalize_post_fields(post_info)
                collected_posts.append(post_info)
                print(f"{idx}. 标题：{post.title}（发布时间：{post_info.get('time', '未知时间')}）")
            print(f"成功爬取 {len(collected_posts)} 个符合条件的较新帖子 from /r/{subreddit_name}")
            return collected_posts
        except Exception as e:
            print(f"获取新帖子时出错: {e}")
            traceback.print_exc()
            return []
            
    def monitor_new_posts(self, subreddit_name="python", interval=60, max_push=3):
        """
        持续监控新帖子并推送
        :param subreddit_name: 目标子版块
        :param interval: 检测间隔（秒），设为60则1分钟检测一次
        :param max_push: 每次最多推送多少条新帖子
        """
        processed_ids = load_processed_posts()
        subreddit = self.reddit.subreddit(subreddit_name)
        print(f"开始持续监控 /r/{subreddit_name} 的新帖子，每{interval}秒检测一次...")
        
        try:
            while True:
                new_posts = []
                # 获取最新的帖子（按时间倒序）
                for post in subreddit.new(limit=10):  # 每次拉取10条最新的，避免遗漏
                    if post.id not in processed_ids:
                        new_posts.append(post)
                        processed_ids.append(post.id)
                        if len(new_posts) >= max_push:
                            break  # 达到单次推送上限，停止拉取
                
                if new_posts:
                    print(f"检测到 {len(new_posts)} 条新帖子，开始推送...")
                    for post in new_posts:
                        post_info = self._format_post_info(post, max_comments=2)  # 调用推送方法
                        log_post_info(post_info)
                    save_processed_posts(processed_ids)  # 保存已处理的ID
                else:
                    print(f"未检测到新帖子，{interval}秒后再次检测...")
                
                time.sleep(interval)  # 休眠指定时间后再次检测
        except KeyboardInterrupt:
            print("用户中断监控，保存已处理记录...")
            save_processed_posts(processed_ids)
        except Exception as e:
            print(f"监控过程出错：{e}")
            traceback.print_exc()
            save_processed_posts(processed_ids)

    def _normalize_post_fields(self, post):
        post["likes"] = post.get("likes") or post.get("score", 0)
        post["comments"] = post.get("comments") or post.get("num_comments", 0)
        post["content"] = post.get("content") or post.get("selftext", "")
        return post    

if __name__ == "__main__":
    print("="*60)
    print("         RedditCrawler 测试         ")
    print("="*60)
    try:
        crawler = RedditCrawler(proxy_host="127.0.0.1", proxy_port=7897)
        print("\n API连接验证成功，可以开始爬取")
        collected_get_posts = crawler.get_new_posts(subreddit_name='python', limit=10, max_comments=5)
        print("\n API爬取子模块成功！")
    except Exception as e:
        print("\n API连接验证失败！")
        traceback.print_exc()
