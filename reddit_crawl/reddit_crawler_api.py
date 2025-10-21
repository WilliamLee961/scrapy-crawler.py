# 该python文件的任务是完成爬虫策略的api接口
import os
import time
import json
from fastapi.responses import JSONResponse  # 导入JSONResponse
import traceback
import threading
from datetime import datetime
from typing import List, Dict, Optional, Any
from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel  # 数据校验
import uvicorn
# 导入你的现有模块
from reddit_crawl.reddit_crawler import RedditCrawler # 基础爬虫模块
from reddit_crawl.notification import (
    load_processed_posts, 
    save_processed_posts,
    log_post_info
)
from reddit_crawl.anti_crawl_core import ip_pool, smart_strategy # 反爬核心模块,调用类（包含类中函数）

# ---------------------- 1. 全局配置与初始化 ----------------------
# API服务配置
API_CONFIG = {
    "host": "127.0.0.1",        # 允许外部访问
    "port": 8000,             # API端口
    "api_key": "RedditCrawler_2024",  # API鉴权密钥（避免未授权访问）
    "log_file_path": "reddit_posts.log",  # 日志文件路径（与notification.py一致）
    "max_concurrent": 100     # 支持最大并发会话（满足甲方指标）
}

# 初始化FastApi应用
app = FastAPI(
    title = "Reddit爬虫对外API服务",
    description="功能：实现爬虫状态查询、日志读取、反爬策略配置（IP池/智能策略）",
    version= "1.0.0", 
     default_response_class=JSONResponse,
    # 2. 正确配置responses：键为状态码（int），值为该状态码的响应配置
    responses={
        200: {  # 针对200状态码的响应配置（最常用）
            "description": "请求成功",
            "content": {
                "application/json": {  # MIME类型放在content下
                    "charset": "utf-8",  # 显式指定UTF-8编码，解决中文乱码
                    "example": {  # 可选：添加示例，方便调试
                        "code": 200,
                        "message": "操作成功",
                        "data": {}
                    }
                }
            }
        },
        400: {  # 可选：针对400错误状态码的配置（示例）
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

# 解决跨域问题（允许前端/其他服务调用API）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 生产环境需替换为具体域名（如 ["https://your-frontend.com"]）
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------- 2. 爬虫状态管理（与API强相关，放在API文件中） ----------------------
class CrawlerState:
    """爬虫运行状态管理（监控并发、延迟、推送响应时间，依赖反爬模块）"""
    def __init__(self):
        self.state = {
            "is_running": False, # 爬虫运行状态
            "current_concurrent": 0, # 当前并发会话数
            "recent_crawl_delays": [], # 最近10次采集延迟（秒）
            "last_push_response_time": None, # 最后一次推送响应时间(从上次推送响应到这次推送响应)
            "total_crawled_posts": 0, # 累计采集帖子
            "total_pushed_posts": 0 # 累计推送帖子
        }
        self.lock = threading.Lock()
        self.crawler_thread: Optional[threading.Thread] = None # 爬虫后台线程
        self.crawled_results = []
        self.results_lock = threading.Lock() # 保护结果列表的线程锁

    def update_crawl_delay(self, delay: float):
        """更新采集延迟（保留最近10次数据，用于计算平均延迟）"""
        with self.lock:
            self.state["recent_crawl_delays"].append(round(delay, 2)) # 保留两位小数
            if len(self.state["recent_crawl_delays"])> 10:
                self.state["recent_crawl_delays"].pop(0) # 把最旧的弹出，只算最近10次
    
    def update_push_response_time(self, response_time: float):
        """更新推送响应时间（记录最后一次，用于验证≤2分钟指标） 应该是从帖子发出到我们应用推送到个人邮箱"""
        with self.lock:
            self.state["last_push_response_time"] = round(response_time, 2)
            self.state["total_pushed_posts"] +=1
    
    def increment_concurrent(self) -> bool:
        """增加并发会话数（不超过反爬策略的并发限制）"""
        with self.lock:
            strategy = smart_strategy.get_current_strategy()
            if self.state["current_concurrent"] < strategy["concurrent_limit"]:
                self.state["current_concurrent"] +=1
                return True
            print(f" 并发数已达上限（{strategy['concurrent_limit']}），无法新增会话")
            return False
    
    def decrement_concurrent(self):
        """减少并发会话数（采集完成/失败时调用）"""
        with self.lock:
            if self.state["current_concurrent"] >0:
                self.state["current_concurrent"]-=1
                # 当减少后变为0时，打印提醒
                if self.state["current_concurrent"] == 0:
                    print(" 当前并发会话数已减至0")

    def test_single_crawl(self) -> Dict[str, Any]:
        """手动触发一次爬取，返回详细结果（用于测试，不依赖后台线程）"""
        try:
            print("\n" + "="*60)
            print(" 手动触发单次爬取测试")
            print("="*60)

            # 1.获取IP和策略
            ip_pool_status = ip_pool.get_pool_status()["statistics"]
            strategy = smart_strategy.get_current_strategy()

            # 2. 获取IP
            if ip_pool_status["valid_ip_count"] > 0:
                selected_ip = ip_pool.get_random_valid_ip()
                if selected_ip:
                    PROXY_HOST = selected_ip["ip"].split(":")[0]
                    PROXY_PORT = int(selected_ip["ip"].split(":")[1])
                    print(f"测试用IP：{selected_ip['ip']}（协议：{selected_ip['protocol']}）")
            else:
                print("测试：IP池无有效IP，用无代理")
                
            # 3. 执行爬取
            print(f"测试爬取参数：子版块={strategy['target_subreddit']}， limit={strategy['max_posts_per_crawl']}")
            crawler = RedditCrawler(proxy_host=PROXY_HOST, proxy_port=PROXY_PORT)
            new_posts = crawler.get_new_posts(
                subreddit_name=strategy["target_subreddit"],
                limit=strategy["max_posts_per_crawl"],
                max_comments=3
            )

            # 4. 存储结果（如果有）
            result = {
                "success": True,
                "crawled_count": len(new_posts),
                "posts": new_posts,
                "message": f"测试手动爬取成功，获取 {len(new_posts)} 条帖子"
            }

            if new_posts:
                self.add_crawled_result(result)
                result["message"] += "（已存入结果容器）"
            print(f" 测试中手动爬取结果：{result['message']}")
            return result
        
        except Exception as e:
            error_message = f"测试手动爬取失败: str{e}"
            return {
                "success": False,
                "crawled_count": 0,
                "posts": [],
                "message": f"测试手动爬取失败，获取0条帖子！ "
            }

    def start_crawler(self) -> bool:
        """启动爬虫（后台线程运行，避免阻塞API）"""
        with self.lock:
            if self.state["is_running"]:
                print(" 爬虫已经在运行， 无需重复启动！")
                return False
            self.crawler_thread = threading.Thread(
                target= self._crawler_main_loop,
                daemon=True
            )
            self.crawler_thread.start()
            self.state["is_running"] = True
            print(" 爬虫已启动（后台线程）")
            return True

    def add_crawled_result(self, posts):
        with self.results_lock:
            self.crawled_results.extend(posts) #  合并爬取的帖子
            if len(self.crawled_results) > 1000:
                self.crawled_results = self.crawled_results[-1000:]  # 只保留最近1000条

    def get_crawled_posts(self, limit=100):
        with self.results_lock:
            # 返回副本，避免外部修改
            return self.crawled_results[-limit:] # 只保留最近1000条

    def stop_crawler(self) -> bool:
        """停止爬虫（安全退出，保存已处理记录）"""
        with self.lock:
            if not self.state["is_running"]:
                print(" 爬虫已停止，无需重复操作")
                return False
            self.state["is_running"] = False
            #  等待线程退出（最多5秒，避免强制终止导致数据丢失）
            if self.crawler_thread and self.crawler_thread.is_alive():
                self.crawler_thread.join(timeout=5)
                if self.crawler_thread.is_alive():
                    print("爬虫线程未及时退出，可能存在未完成任务")  
            # 退出前保存已处理记录
            save_processed_posts(load_processed_posts())
            print(" 爬虫已停止，已保存处理记录")
            return True
        
    def _crawler_main_loop(self):
        """爬虫主循环（核心逻辑：调用反爬策略+原有爬虫+通知）"""
        print(f"[后台爬虫] 线程已启动，进入循环（is_running: {self.state['is_running']}）")  # 新增：确认线程启动
        while self.state["is_running"]:
            print(f"\n[后台爬虫] 进入循环迭代（当前时间：{datetime.now().strftime('%H:%M:%S')}）")  # 新增：确认循环执行
            try:
                print(f"[后台爬虫] 开始检查IP池状态")  # 新增：定位到IP池环节
                # PROXY_HOST = None
                # PROXY_PORT = None
                # 1. 检查是否需要自动切换IP（调用反爬模块的智能策略）
                ip_pool_status = ip_pool.get_pool_status()["statistics"]
                if ip_pool_status["valid_ip_count"] > 0:
                    print(f"[后台爬虫] IP池有 {ip_pool_status['valid_ip_count']} 个有效IP")  # 新增：确认IP池有效
                    if smart_strategy.need_auto_switch_ip():
                        print(f"🔄 需要切换IP，从IP池获取新代理")
                        selected_ip = ip_pool.get_random_valid_ip()
                    else:
                        # 不需要切换时，获取当前已选中的IP（如果没有则随机取一个）
                        print("不需要切换IP!直接开始爬取任务！") # 增加输出来监控代码运行情况
                        selected_ip = ip_pool.get_current_ip() or ip_pool.get_random_valid_ip()
                    
                    # 只要获取到IP，就更新代理参数
                    if selected_ip:
                        # 新增调试打印：查看 selected_ip 的类型和值
                        print(f"调试：selected_ip 类型 = {type(selected_ip)}，值 = {selected_ip}")
                        # 更新爬虫的代理配置（修改RedditCrawler的全局代理参数）
                        ip_parts = selected_ip["ip"].split(":")
                        PROXY_HOST = ip_parts[0]  # 局部变量，仅在当前if块内有效
                        PROXY_PORT = int(ip_parts[1])
                        print(f"已获取代理IP：{PROXY_HOST}:{PROXY_PORT}（协议：{selected_ip['protocol']}）")
                else:
                    print(f"[后台爬虫] IP池无有效IP，尝试无代理爬取")  # 新增：定位IP池问题
                    print("IP池无有效IP，将尝试无代理连接（可能失败）")
                
                # 2.检查并发限制（调用反爬模块的策略）
                print(f"[后台爬虫] 检查并发限制（当前：{self.state['current_concurrent']}，上限：{smart_strategy.get_current_strategy()['concurrent_limit']}）")  # 新增：确认并发数
                if not self.increment_concurrent():
                    print(f"[后台爬虫] 并发数达上限，等待1秒")  # 新增：定位并发拦截
                    strategy = smart_strategy.get_current_strategy()
                    print(f"并发数达上限（当前：{self.state['current_concurrent']}，上限：{strategy['concurrent_limit']}），等待1秒")
                    time.sleep(1)
                    continue
                print(f"并发数已增加，当前：{self.state['current_concurrent']}")
                print(f"[后台爬虫] 并发数已增加，开始爬取")  # 新增：确认进入爬取
                # 3.执行采集（调用原有爬虫模块，记录延迟）
                strategy = smart_strategy.get_current_strategy()
                print(f"[后台爬虫] 开始执行爬取（子版块：{smart_strategy.get_current_strategy()['target_subreddit']}）")  # 新增：确认爬取目标
                start_time = time.time()

                # 初始化爬虫（使用当前代理和策略函数）
                crawler = RedditCrawler(
                    proxy_host=PROXY_HOST,
                    proxy_port=PROXY_PORT
                )
                print("爬虫主循环中开始爬虫！")
                new_posts = crawler.get_new_posts(
                    subreddit_name=strategy["target_subreddit"],
                    limit = strategy["max_posts_per_crawl"],
                    max_comments=3 # 固定取前3条评论（可后续加入策略参数）
                )

                if new_posts:
                    self.add_crawled_result(new_posts)
                    print(f"已将 {len(new_posts)} 条帖子存入结果容器")

                # 记录采集延迟
                crawl_delay = time.time() - start_time
                self.update_crawl_delay(crawl_delay)
                self.state["total_crawled_posts"] += len(new_posts)
                print(f"采集完成：{len(new_posts)} 条帖子，延迟：{crawl_delay:.2f}秒")

                # 4 . 执行推送（调用原有通知模块，记录响应时间）
                if new_posts:
                    push_start = time.time()
                    processed_ids = load_processed_posts()
                    # 筛选为处理的新帖子（去重）
                    new_undetected = [p for p in new_posts if p["id"] not in processed_ids]

                    if new_undetected:
                        # 按发布时间正序记录日志（符合阅读习惯）
                        new_undetected_sorted = sorted(
                            new_undetected,
                            key=lambda x: x["created_utc"]
                        )
                        for post in new_undetected_sorted:
                            log_post_info(post)
                
                        # 保存更新后的已处理记录
                        save_processed_posts(processed_ids + [p["id"] for p in new_undetected]) 
                        # processed_ids 是列表（从 load_processed_posts 获取），而 p["id"] for p in new_undetected 是生成器
                        # （类型为 generator），两者无法用 + 拼接（列表只能与列表拼接）!；
                        
                        # 记录推送响应时间
                        push_delay = time.time() - push_start
                        self.update_push_response_time(push_delay)
                        print(f"推送完成：{len(new_undetected)} 条新帖子，响应时间：{push_delay:.2f}秒")

                # 5. 释放并发数， 按策略休眠（调用反爬模块的间隔参数）
                self.decrement_concurrent() # 每个任务的启动（+1）必然对应一个任务的结束（-1），确保 current_concurrent 始终准确反映 “当前正在运行的任务数量”
                print(f"按策略休眠 {strategy['crawl_interval']} 秒...\n")
                time.sleep(strategy["crawl_interval"])

            except Exception as e:
                # 采集/推送失败： 释放并发数， 打印错误日志
                print(f"[后台爬虫] 循环内异常：{str(e)}")
                self.decrement_concurrent()
                error_msg = f"爬虫循环出错：{str(e)[:100]}"
                print(error_msg)
                traceback.print_exc()
                print(f" 出错后休眠5秒，重试...\n")
                time.sleep(5) # 出错后休眠5秒再重试，避免频繁报错

    def get_current_state(self) -> Dict[str, Any]:
        """获取爬虫实时状态（含指标达标情况：采集延迟≤3秒、推送≤2分钟）"""
        with self.lock:
            delays = self.state["recent_crawl_delays"] # 注意这个只有近10次
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
                    # 甲方指标达标判断---采集延迟小于3秒， 推送延迟小于2分钟
                    "is_crawl_delay_qualified": avg_delay <= 3,
                    "is_push_qualified": (
                        self.state["last_push_response_time"] is None #考虑第一次推送的情况
                        or self.state["last_push_response_time"] <= 120
                    )
                }
            }

# 初始化爬虫状态管理（全局唯一）
crawler_state = CrawlerState()


# ---------------------- 3. API鉴权（避免未授权访问） ----------------------
def verify_api_key(api_key: str = Query(..., description="API访问密钥")):
    """API密钥鉴权（所有接口需携带该参数，依赖注入实现）"""
    if api_key != API_CONFIG["api_key"]:
        raise HTTPException(status_code=401, detail="无效的API密钥,请检查密钥是否正确")
    return api_key


# ---------------------- 4. API请求/响应模型（Pydantic定义） ----------------------
class IPAddRequest(BaseModel):
    """添加IP到IP池的请求模型（参数校验）"""
    ip: str # 格式"ip:port"（如 "127.0.0.1:7891"）
    protocol: Optional[str] = "http" # 可选： "sock5" or "http"

class StrategyUpdateRequest(BaseModel):
    """更新智能策略的请求模型（仅允许修改预设字段）"""
    # None表示"无值"或"未设置"，是一个明确的空值状态。
    concurrent_limit: Optional[int] = None
    crawl_interval: Optional[int] = None
    ip_switch_interval: Optional[int] = None
    retry_count: Optional[int] = None
    target_subreddit: Optional[str] = None
    max_posts_per_crawl: Optional[int] = None


# ---------------------- 5. API端点实现（按功能分类） ----------------------
@app.get("/api/crawler/state", tags=["1. 爬虫状态管理"]) # get是获取数据（查询）
def get_crawler_state_api(
    api_key: str = Depends(verify_api_key)
    ) -> Dict[str, Any]:
    """
    获取爬虫实时状态
    - 基础状态：运行状态、并发数、累计采集/推送数
    - 性能指标：平均采集延迟、推送响应时间、指标达标情况
    """
    return {
        "code": 200,
        "message": "success",
        "data": crawler_state.get_current_state()
    }

@app.post("/api/crawler/start", tags=["1. 爬虫状态管理"]) # post是提交数据
def start_crawler_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """启动爬虫（后台线程运行，不阻塞API）"""
    success = crawler_state.start_crawler()
    if success:
        return {
            "code": 200,
            "message": "爬虫已启动",
            "data": {}
        }
    raise HTTPException(status_code=400, detail="爬虫已在进行中，无需重复启动")

@app.get("/api/crawler/results", tags=["1.爬虫状态管理"])
def get_crawler_results(
    limit: int = Query(100, description="返回结果的最大数量"),
    api_key = Depends(verify_api_key)
) -> Dict[str, Any]:
    """获取已爬取的帖子结果（从结果容器中读取）"""
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
    """
    手动触发一次爬取（用于测试！不依赖后台线程）
    - 直接执行一次完整爬取流程，返回爬取结果
    - 方便快速验证：是否能爬取到数据、结果是否存入容器
    """
    test_result = crawler_state.test_single_crawl()
    if test_result["success"]:
        return {
            "code": 200,
            "message": test_result["message"],
            "data": {
                "crawled_count": test_result["crawled_count"],
                "posts": test_result["posts"],
                "container_total": len(crawler_state.crawled_results)  # 容器当前总条数
            }
        }
    raise HTTPException(status_code=500, detail=test_result["message"])


@app.post("/api/crawler/stop", tags=["1. 爬虫状态管理"])
def stop_crawler_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """停止爬虫（安全退出，自动保存已处理记录）"""
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
    """
    获取爬虫日志（支持时间范围筛选）
    - 日志格式与 notification.py 的 log_post_info 一致
    - 按时间倒序返回（最新日志在前）
    """
    limit = min(limit, 100)
    log_file = API_CONFIG["log_file_path"]

    # 检查日志文件是否存在
    if not os.path.exists(log_file):
        return {
            "code": 200,
            "message": "日志文件不存在",
            "data": {"total": 0, "logs": []}
        }

    # 读取日志文件
    logs = []
    with open (log_file, "r", encoding="utf-8") as f:
        current_log = ""
        for line in f:
            # 日志边界识别：以 "[YYYY-MM-DD HH:MM:SS]" 开头的行为新日志
            if line.startswith("[") and len(line) >=20:
                if current_log:
                    logs.append(current_log.strip())
                current_log = line # 把 current_log重置为当前行（新日志的开头）
            else:
                current_log += line
        if current_log:
            logs.append(current_log.strip())
    
    # 时间范围筛选
    filtered_logs = []
    for log in logs:
        # 提取日志时间戳 格式：[2024-10-01 12:00:00]
        if not log.startswith("["):
            continue
        log_time_str = log[1:20] 
        try:
            log_time = datetime.strptime(log_time_str,"%Y-%m-%d %H:%M:%S")
        except:
            continue  # 跳过格式异常的日志

        # 应用筛选条件
        if start_time and log_time < start_time: # 早于用户需求开始前的去除
            continue
        if end_time and log_time > end_time : # 晚于用户结束时间的去除
            continue
        filtered_logs.append(
            {
                "log_time": log_time_str,
                "content": log
            }
        )

    # 按时间倒叙,取前limit条
    filtered_logs.sort(key=lambda x: x["log_time"], reverse=True)
    return {
        "code": 200,
        "message": "success",
        "data": {
            "total": len(filtered_logs),
            "limit": limit,
            "logs": filtered_logs[:limit] # 这里添加[::-1]可以将limit新到最新按时间顺序
        }
    }

@app.get("/api/anti-crawl/ip-pool", tags=["3. 反爬策略管理-IP池"])
def get_ip_pool_status_api(
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """获取IP池状态（统计信息+IP详情）"""
    return {
        "code": 200,
        "message": "success! 成功获取反爬策略",
        "data": ip_pool.get_pool_status()
    }

@app.post("/api/anti-crawl/ip-pool/add", tags=["3.反爬策略管理-IP池"])  # 添加新ip到IP池
def add_ip_api(
    req: IPAddRequest,
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """添加IP到IP池（自动检测有效性，重复/无效IP会返回失败）"""
    # 校验IP格式（必须包含 ":"，且端口为数字）
    if ":" not in req.ip: # 还有protocol
        raise HTTPException(status_code=400, detail="IP格式错误，需为 'ip:port'（如 1.2.3.4:7891）")
    ip_parts = req.ip.split(":")
    if not ip_parts[1].isdigit():
        raise HTTPException(status_code=400, detail="IP端口必须为数字（如 1.2.3.4:7891）")
    # 调用反爬模块添加IP
    success = ip_pool.add_ip(req.ip, req.protocol) #
    if success:
        return {
            "code":200,
            "message": f"IP {req.ip} 添加成功（已验证有效）", # 反爬模块的add_ip函数包含验证ip有效部分
            "data": {}
        }
    raise HTTPException(status_code=400, detail=f"IP {req.ip} 无效或已在池中")

@app.post("/api/anti-crawl/ip-pool/remove", tags=["3. 反爬策略管理-IP池"])
def remove_ip_api(
    ip: str = Query(..., description="待删除IP(格式： ip:port)"),
    api_key: str = Depends(verify_api_key)
) ->  Dict[str, Any]:
    """从IP池删除指定IP"""
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
    """获取当前生效的智能反爬策略（并发/间隔/IP切换等参数）"""
    return {
        "code": 200,
        "message": "success!成功获取模型智能反爬策略",
        "data": smart_strategy.get_current_strategy()
    }

@app.post("/api/anti-crawl/strategy/update", tags=["3. 反爬策略管理-智能参数"])
def update_strategy_api(
    req: StrategyUpdateRequest, # FastAPI 会自动将 用户通过API发送的JSON 请求数据解析到 StrategyUpdateRequest 模型
    api_key: str = Depends(verify_api_key)
) -> Dict[str, Any]:
    """
    更新智能反爬策略（仅允许修改预设字段）
    - 参数会自动校验合法性（如并发限制 1~100，间隔 1~60秒）
    """
    new_params = req.model_dump(exclude_none=True) # 排除空值 
    if not new_params:
        raise HTTPException(status_code=400, detail= "需至少传入一个待更新的策略参数")
    
    # 调用反爬模块更新策略
    updated_strategy = smart_strategy.update_strategy(new_params)
    return {
        "code": 200,
        "message": "策略更新成功",
        "data": updated_strategy
    }

# ---------------------- 6. API服务启动入口 ----------------------
if __name__ == "__main__":
    print("="*60)
    print("         Reddit爬虫API服务启动中         ")
    print("="*60)
    print(f" API地址：http://{API_CONFIG['host']}:{API_CONFIG['port']}")
    print(f" 接口文档：http://{API_CONFIG['host']}:{API_CONFIG['port']}/docs")
    print(f" API密钥：{API_CONFIG['api_key']}（请求时需携带）")
    print(f" 反爬模块：已加载 IP池（初始有效IP：{ip_pool.get_pool_status()['statistics']['valid_ip_count']}）")
    print("="*60)
    
    # 启动FastAPI服务 （使用Uvicorn，支持高并发）
    uvicorn.run(
        app="__main__:app",
        host=API_CONFIG["host"],
        port=API_CONFIG["port"],
        workers=1, # 工作进程数（建议为CPU核心数的2倍，生产环境可调整）
        reload=False # 生产环境关闭热重载（避免性能损耗）
    )