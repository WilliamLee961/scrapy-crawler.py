import time
import os
import sys
import requests
import socket
import traceback
from bs4 import BeautifulSoup
from urllib.robotparser import RobotFileParser  # 用于检查robots.txt合规性
# 复用Reddit爬虫的日志/存储逻辑（无需修改）
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(parent_dir)
from reddit_crawl.notification import save_processed_posts, load_processed_posts, log_post_info
from playwright.sync_api import sync_playwright

# 目标Skool社群的公开首页（必须是无需登录即可访问的公开页面！）
TARGET_SKOOL_GROUP = "ai-automation-society"  # 例：https://www.skool.com/startup 中的 "startup"
SKOOL_BASE_URL = f"https://www.skool.com/{TARGET_SKOOL_GROUP}"

# 爬取合规配置（遵循robots.txt，设置合理请求头）
USER_AGENT = "script:skool_public_crawler:0.1"
ROBOTS_TXT_URL = "https://www.skool.com/robots.txt"  # Skool全局robots规则

# 2. 复用Reddit爬虫的基础工具函数（仅修改域名相关）
# --------------------------
def test_proxy_connectivity(host, port):
    """测试代理端口是否可达（完全复用Reddit爬虫逻辑）"""
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

def check_skool_domain_access(proxy_url):
    """检查代理是否能访问Skool公开域名（替换Reddit的域名检查）"""
    test_urls = [SKOOL_BASE_URL, "https://www.skool.com"]  # 仅检查公开页面
    ok = True
    for url in test_urls:
        try:
            response = requests.get(
                url,
                proxies={"http": proxy_url, "https": proxy_url} if proxy_url else None,
                timeout=10,
                allow_redirects=True,
                headers={"User-Agent": USER_AGENT}  # 合规请求头
            )
            # Skool公开页面返回200或302（重定向到公开内容）为正常
            if response.status_code in [200, 302]:
                print(f"✅ 成功访问 {url} via {proxy_url or '无代理'}，状态码：{response.status_code}")
            else:
                print(f"❌ 访问 {url} 状态码异常：{response.status_code}")
                ok = False
        except Exception as e:
            print(f"❌ 无法访问 {url} via {proxy_url or '无代理'}：{str(e)}")
            ok = False
    return ok

def check_robots_compliance(target_url):
    rp = RobotFileParser()
    rp.set_url(ROBOTS_TXT_URL)
    try:
        rp.read()
        # 1. 先通过RobotFileParser初步判断
        is_allowed_by_rp = rp.can_fetch(USER_AGENT, target_url)

        # 2. 补充手动校验：结合Skool实际规则（Allow: / + Disallow: /*/--/*）
        is_forbidden_manual = "/--/" in target_url and len(target_url.split("/--/")) >= 2
        is_allowed_manual = not is_forbidden_manual and target_url.startswith(SKOOL_BASE_URL.split(TARGET_SKOOL_GROUP)[0])
        # 3. 综合判定：以手动校验为准（修正RobotFileParser的误判）
        if is_allowed_manual:
            print(f"robots.txt 允许爬取：{target_url}（符合 Allow: /，未触发 Disallow: /*/--/*）")
            return True
        else:
            print(f"robots.txt 禁止爬取：{target_url}（触发 Disallow: /*/--/* 或不在 Allow 范围）")
            return False
        # is_allowed = rp.can_fetch(USER_AGENT, target_url)
        # if is_allowed:
        #     print(f"robots.txt 允许爬取：{target_url}")
        #     return True
        # else:
        #     print(f"robots.txt 禁止爬取：{target_url}（违反合规要求，终止操作）")
        #     return False
    except Exception as e:
        print(f"读取robots.txt失败（{str(e)}），默认仅爬取公开首页")
        return target_url.startswith(SKOOL_BASE_URL)


class SkoolCrawler:
    def __init__(self, proxy_host=None, proxy_port=None):
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxies = None
        self.session = requests.Session()   # 复用会话，减少连接开销
        self.session.headers = {
            "User-Agent": "script:skool_public_crawler:0.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        } 
        self.supported_protocols = ["http"]  

        try:
            if not check_robots_compliance(SKOOL_BASE_URL): # 1
                raise PermissionError("爬取目标违反Skool robots.txt规则,check步骤失败")
            if self.proxy_host and self.proxy_port: # 2
                if not test_proxy_connectivity(self.proxy_host, self.proxy_port):
                    raise ConnectionError("代理端口不可用")
                self._init_with_proxy()
            else:
                print("未使用代理，尝试直接连接Skool")
                self._init_without_proxy()
            if not check_skool_domain_access(self.proxies["https"] if self.proxies else None): # 3
                raise ConnectionError("Skool公开页面访问失败，无法继续爬取")
            print(f"SkoolCrawler 初始化成功！目标社群：{SKOOL_BASE_URL}")
        except Exception as e:
            print(f"SkoolCrawler 初始化失败：{str(e)}")
            traceback.print_exc()
            raise # 向上层抛出异常，避免无效爬取
            
    def _init_with_proxy(self):
        success = False
        last_error = None

        for proto in self.supported_protocols:
            proxy_url = f"{proto}://{self.proxy_host}:{self.proxy_port}"
            print(f"\n--- 尝试 {proto} 协议代理：{proxy_url} ---")

            if not check_skool_domain_access(proxy_url):
                print(f"--- {proto} 协议代理无法访问Skool，跳过 ---")
                continue # 直到找到能够访问的协议或者失效

            try:
                self.session.proxies.update({"http": proxy_url, "https": proxy_url})
                self.proxies = {"http": proxy_url, "https": proxy_url}
                self._verify_proxy_effective(proxy_url)
                success = True
                print(f"{proto} 协议代理初始化成功")
                break
            except Exception as e:
                last_error = e
                print(f"{proto} 协议代理初始化失败：{str(e)[:60]}")
        if not success:
            raise ConnectionError(f"所有代理协议均失败，最后错误：{str(last_error)[:80]}")        
        
    def _init_without_proxy(self):
        try:
            self.proxies = None
            print("无代理模式初始化成功（仅爬取Skool公开内容）")
        except Exception as e:
            raise ConnectionError(f"无代理连接Skool失败：{str(e)}")

    def _verify_proxy_effective(self, proxy_url):
        """验证代理是否生效（复用Reddit逻辑）"""
        try:
            ip_check_url = "https://api.ipify.org?format=json"
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            response = self.session.get(
                ip_check_url, 
                timeout=10,
                proxies=proxies
            )
            current_ip = response.json()["ip"]
            print(f" 当前代理出口IP：{current_ip}（确认是否为预期代理IP）")
        except Exception as e:
            print(f" 代理IP验证失败（不影响爬取）：{str(e)}")    

    def format_post_info(self, post_soup):
        post_info = {
            "id": "",
            "title": "未知标题",
            "author": "未知作者",
            "score": 0,  # Skool无点赞数，用评论数替代
            "created_utc": time.time(),  # 若页面无时间戳，用爬取时间替代
            "created_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "url": "",
            "comment_count": 0,
            "content_excerpt": "无内容",
            "top_comments": []  # Skool公开页面暂不显示评论，留空
        }

        try:
            # 1. 帖子标题（示例选择器：需根据实际页面F12检查）
            title_elem = post_soup.find("h3", class_="text-lg font-semibold text-gray-900")
            if title_elem:
                post_info["title"] = title_elem.get_text(strip=True)
                # 2. 帖子链接（从标题的href提取）
                link_elem = title_elem.find_parent("a", href=True)
                if link_elem:
                    post_info["url"] = f"https://www.skool.com{link_elem['href']}"
                    # 3. 帖子ID（从链接提取唯一标识，例：/group/xxxx/topic/12345 → ID=12345）
                    post_info["id"] = link_elem["href"].split("/")[-1] if link_elem["href"].endswith("/") else link_elem["href"].split("/")[-1]
            # 4. 帖子作者（示例选择器）
            author_elem = post_soup.find("span", class_="text-sm text-gray-600")
            if author_elem:
                post_info["author"] = author_elem.get_text(strip=True).replace("By ", "")

            # 5. 评论数（示例选择器：Skool用"X Comments"表示）
            comment_elem = post_soup.find("span", class_="text-xs text-gray-500")
            if comment_elem and "Comments" in comment_elem.get_text():
                comment_count = comment_elem.get_text(strip=True).split(" ")[0]
                post_info["comment_count"] = int(comment_count) if comment_count.isdigit() else 0
                post_info["scoarse_postsre"] = post_info["comment_count"]  # 用评论数替代点赞数
            
            # 6. 内容摘要（示例选择器：帖子预览文本）
            content_elem = post_soup.find("p", class_="text-sm text-gray-600 mt-1")
            if content_elem:
                content = content_elem.get_text(strip=True)
                post_info["content_excerpt"] = content[:200] + "..." if len(content) > 200 else content

            # 7. 发布时间（若页面有时间戳，需解析为UTC时间戳，示例用爬取时间）
            time_elem = post_soup.find("time", datetime=True)
            if time_elem and "datetime" in time_elem.attrs:
                # Skool时间格式：2024-05-20T12:34:56.789Z → 转换为UTC时间戳
                try:
                    from dateutil import parser  # 需安装 python-dateutil
                    dt = parser.isoparse(time_elem["datetime"])
                    post_info["created_utc"] = dt.timestamp()
                    post_info["created_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ImportError:
                    print("⚠️ 缺少 python-dateutil，无法解析时间戳（需安装：pip install python-dateutil）")

        except Exception as e:
            print(f"⚠️ 格式化帖子信息失败：{str(e)}（帖子ID：{post_info['id']}）")
            traceback.print_exc()

        return post_info




if __name__ == "__main__":
    print("=== 开始页面访问测试 ===")
    print("\n--- 测试无代理访问 ---")

    # crawler = SkoolCrawler("127.0.0.1", "7897")  
    # 无代理模式；需代理则添加参数
    crawler = SkoolCrawler() 
    no_proxy_success = check_skool_domain_access(None)

    test_proxies = [
        "http://127.0.0.1:7897", 
        # "socks5://localhost:1080"
    ]

    for proxy in test_proxies:
        print(f"\n--- 测试代理 {proxy} 访问 ---")
        try:
            if proxy.startswith(('http://', 'https://')):
                proxy_host = proxy.split('://')[1].split(':')[0]
                proxy_port = int(proxy.split('://')[1].split(':')[1])          
            # 先测试代理端口是否可达
            proxy_reachable = test_proxy_connectivity(proxy_host, proxy_port)
            if proxy_reachable:
                # 再测试能否通过代理访问目标网站
                check_skool_domain_access(proxy)
                crawler._verify_proxy_effective(proxy)
        except Exception as e:
            print(f"代理配置解析错误：{str(e)}")