# 专注反爬核心逻辑，不依赖任何API和爬虫代码，可独立复用
import time
import requests
import threading
from datetime import datetime
from typing import List, Dict, Optional, Any
import json

class IPPool:
    """IP代理池核心类（独立封装IP管理逻辑：添加/删除/随机获取/有效性检测）"""
    def __init__(self):
        # IP池存储格式：{"ip": "1.2.3.4:8080", "protocol": "socks5", "valid": True, "last_used": datetime} 
        self.pool: List[Dict[str, Any]] = []
        self.lock = threading.Lock()  # 线程安全锁（并发场景下保护IP池操作）
        self.test_url = "https://www.reddit.com/"  # 有效性检测目标地址
        self.test_timeout = 5  # 检测超时时间（秒）
        self.test_user_agent = "script:anti_crawl_ip_test:0.1"  # 检测用UA
        self.current_ip = None  # 记录正在使用的IP

    def get_current_ip(self) -> Optional[Dict[str, Any]]:
        """新增：获取当前正在使用的IP（如果有效）"""
        with self.lock:
            if self.current_ip and self.current_ip["valid"]:
                return self.current_ip.copy()
            return None

    def add_ip(self, ip:str, protocol: str = "http")-> bool:
        with self.lock:
            # 1. 避免重复添加
            if any(item["ip"] == ip for item in self.pool):
                print(f" IP {ip} 已在池中，无需重复添加")
                return False
            # 2.检测IP有效性
            is_valid = self._check_ip_validity(ip, protocol)

            # 3.加入IP池
            self.pool.append({
                "ip": ip,
                "protocol": protocol.lower(), # 统一小写（避免大小写不一致）
                "valid": is_valid,
                "last_used": None # 首次添加无使用时间
            })

            print(f"IP {ip} 添加{'' if is_valid else '(无效)'}到池， 协议 :{protocol}")
            return is_valid

    def remove_ip(self, ip:str) -> bool:
        """
        从IP池删除指定IP
        :param ip: 待删除IP（格式 "ip:port"）
        :return: True=删除成功，False=IP不存在
        """  
        with self.lock:
            original_count = len(self.pool)
            self.pool = [item for item in self.pool if item["ip"]!= ip]
            is_removed = len(self.pool) < original_count
            if is_removed:
                print(f"📤 IP {ip} 已从池中删除")
            else:
                print(f"⚠️ IP {ip} 不在池中，删除失败")
            return is_removed
            
    def get_random_valid_ip(self)-> Optional[Dict[str, Any]]:
        """
        随机获取一个有效IP（优先选择最近未使用的IP，避免IP被频繁封禁）
        :return: 有效IP字典（含ip/protocol），无有效IP时返回None
        """
        with self.lock:
            # 1. 筛选当前有效IP
            valid_ips = [item for item in self.pool if item["valid"]]
            if not valid_ips:
                print(" IP池中无有效IP")
                return None
            # 2. 按「最近使用时间」排序（未使用的在前，减少IP重复使用）
            valid_ips.sort(key=lambda x: x["last_used"] or datetime.min)
            selected_ip = valid_ips[0] # 一个包含ip的key的字典

            # 3. 更新IP最近使用时间
            selected_ip["last_used"] = datetime.now()
            self.current_ip = selected_ip # 更新当前IP
            print(f" 选中有效IP：{selected_ip['ip']}（协议：{selected_ip['protocol']}）")
            return selected_ip.copy() # 返回副本， 避免外部修改池内数据
    
    def get_pool_status(self) -> Dict[str, Any]:
        """
        获取IP池当前状态（总数/有效数/IP详情）
        :return: 状态字典（含统计信息和IP列表）
        """
        with self.lock:
            total = len(self.pool)
            valid = len([item for item in self.pool if item["valid"]])
            invalid = total - valid

            # 格式化IP详情（隐藏内部锁等铭感字段）
            ip_details = [{
                "ip": item["ip"],
                "protocol": item["protocol"],
                "valid": item["valid"],
                "last_used": item["last_used"].strftime("%Y-%m-%d %H:%M:%S") if item["last_used"] else "未使用"

            } for item in self.pool]
            return {
                "statistics": {
                    "total_ip_count": total,
                    "valid_ip_count": valid,
                    "invalid_ip_count": invalid,
                    "valid_rate": round(valid/total*100, 2) if total >0 else 0.0 # ip有效率
                },
                "ip_details": ip_details
            }

    def _check_ip_validity(self, ip:str, protocol: str) -> bool:
        """
        内部方法：检测IP是否能正常访问Reddit
        :return: True=有效，False=无效
        """
        proxy_url = f"{protocol.lower()}://{ip}"
        proxies = {"http": proxy_url, "https": proxy_url}

        try:
            response = requests.get(
                url = self.test_url,
                proxies=proxies,
                timeout=self.test_timeout,
                headers={"User-Agent": self.test_user_agent},
                allow_redirects=True
            )
            # Reddit返回200（成功）或302（重定向）均视为IP有效
            is_valid = response.status_code in [200, 302]
            print(f" IP {ip} 检测{'' if is_valid else '不'}通过，状态码：{response.status_code}")
            return is_valid
        except Exception as e:
            print(f" IP {ip} 检测失败：{str(e)[:50]}（协议：{protocol}）")
            return False

class SmartStrategy:
    """智能反爬策略核心类（独立封装策略参数管理： 查询/更新/合法性校验）"""
    def __init__(self):
        # 初始策略参数（含默认值和合法范围，避免外部传入非法值）
        self.base_strategy = {
            "concurrent_limit": 50,          # 并发会话限制（1~100）
            "crawl_interval": 60,             # 采集间隔（秒，10~120）
            "ip_switch_interval": 300,       # IP自动切换间隔（秒，60~3600）
            "retry_count": 3,                # 采集失败重试次数（1~10）
            "target_subreddit": "python",    # 目标子版块（合法Reddit子版块名）
            "max_posts_per_crawl": 10,        # 单次采集最大帖子数（1~20）
            "fail_threshold": 3,             # 新增：连续失败阈值（触发IP切换）
            "delay_threshold": 8             # 新增：延迟阈值（秒，触发IP切换）
        }
        self.strategy = self.base_strategy.copy() # 当前生效策略
        self.lock = threading.Lock()
        self.last_ip_switch_time = datetime.now() # 上次ip切换的时间（用于自动切换判断）
        self.consecutive_fail_count = 0 # 连续失败次数
        self.recent_delays = [] # 最近10次爬取延迟（用于计算平均延迟）

    def get_current_strategy(self) -> Dict[str, Any]:
        """获取当前生效的反爬策略（返回副本，避免外部修改）"""
        with self.lock:
            return self.strategy.copy()

    def need_auto_switch_ip(self) -> bool:
        """判断是否需要自动切换IP（核心逻辑）"""
        with self.lock:
            # 条件1：定时切换（超过设定的切换间隔）
            time_since_last_switch = (datetime.now() - self.last_ip_switch_time).total_seconds()
            time_based_switch = (time_since_last_switch >= self.strategy["ip_switch_interval"]) 

            # 条件2：连续失败次数超过阈值
            fail_based_switch = self.consecutive_fail_count >= self.strategy["fail_threshold"]

            # 条件3：平均延迟超过阈值（至少3次记录才判断）
            delay_based_switch = False
            if len(self.recent_delays) >=3:
                avg_delay = sum(self.recent_delays)/ len(self.recent_delays)
                delay_based_switch = avg_delay >= self.strategy["delay_threshold"]
            
            # 只要三个条件满足一个即需要切换IP, 输出判断日志方便调试
            if time_based_switch:
                print(f" 需要切换IP：距离上次切换已过 {time_since_last_switch:.1f} 秒（阈值：{self.strategy['ip_switch_interval']}秒）")
            if fail_based_switch:
                print(f" 需要切换IP：连续失败 {self.consecutive_fail_count} 次（阈值：{self.strategy['fail_threshold']}次）")
            if delay_based_switch:
                print(f" 需要切换IP：平均延迟 {sum(self.recent_delays)/len(self.recent_delays):.1f} 秒（阈值：{self.strategy['delay_threshold']}秒）")
            return time_based_switch or fail_based_switch or delay_based_switch

    def record_crawl_result(self, success:bool, delay: float=0 ):
        """新增：记录爬取结果（用于更新失败次数和延迟统计）"""
        with self.lock:
            if success:
                # 爬取成功：重置连续失败次数，记录延迟
                self.consecutive_fail_count = 0
                self.recent_delays.append(delay)
                if len(self.recent_delays) > 10:
                    self.recent_delays.pop(0)
            else:
                # 爬取失败，增加连续失败次数
                self.consecutive_fail_count +=1
                print(f"爬取失败，连续失败次数：{self.consecutive_fail_count}")

    def reset_ip_switch_time(self):
        """重置IP切换时间（切换IP后调用）"""
        with self.lock:
            self.last_ip_switch_time = datetime.time()
            print(f"已重置IP切换时间（当前时间：{self.last_ip_switch_time.strftime('%H:%M:%S')}）")

    def update_strategy(self, new_params: Dict[str, Any]) -> Dict[str, Any]:
        """
        更新反爬策略（仅允许修改预设字段，自动校验参数合法性）
        :param new_params: 待更新的参数字典（如 {"concurrent_limit": 60, "crawl_interval": 3}）
        :return: 更新后的完整策略
        """
        with self.lock:
            # 1. 过滤非法字段（仅保留base_strategy中存在的字段）
            allowed_fields = self.base_strategy.keys()
            valid_params = {k: v for k, v in new_params.items() if k in allowed_fields}
            if not valid_params:
                print("无有效参数可更新（仅允许修改：{}）".format(", ".join(allowed_fields)))
                return self.strategy.copy()
            # 2. 按字段类型和合法范围校验参数
            for field, value in valid_params.items():
                if field == "concurrent_limit":
                    # 并发限制：1~100（匹配API服务的最大并发）
                    self.strategy[field] = max(1, min(int(value), 100))
                elif field == "crawl_interval":
                    # 采集间隔：1~60秒（避免过频触发反爬）
                    self.strategy[field] = max(10, min(int(value), 120))
                elif field == "ip_switch_interval":
                    # IP切换间隔：60~3600秒（1分钟~1小时）
                    self.strategy[field] = max(60, min(int(value), 3600))
                elif field == "retry_count":
                    # 重试次数：1~10次
                    self.strategy[field] = max(1, min(int(value), 10))
                elif field == "target_subreddit":
                    # 子版块名：仅允许字母/数字/下划线（Reddit子版块命名规则）
                    if isinstance(value, str) and value.strip().isalnum(): # 去掉首位空格只包含字母或数字
                        self.strategy[field] = value.strip().lower()
                    else:
                        print(f" 子版块名 {value} 非法（仅允许字母/数字），跳过更新")
                elif field == "max_posts_per_crawl":
                    # 单次采集数：1~20条（避免单次请求过多数据）
                    self.strategy[field] = max(1, min(int(value), 20))
                elif field == "fail_threshold":
                    # 连续失败阈值：1~5次
                    self.strategy[field] = max(1, min(int(value), 5))
                elif field == "delay_threshold": # 延迟阈值：3~30秒
                    self.strategy[field] = max(3, min(int(value), 30))
            print(f" 策略更新成功，更新字段：{list(valid_params.keys())}")
            return self.strategy.copy()

# 反爬核心模块单例（全局唯一，避免多实例冲突）
ip_pool = IPPool()
smart_strategy = SmartStrategy()

if __name__ == "__main__":
    # 测试反爬核心模块（单独运行该文件时执行）
    print("="*50)
    print("         反爬核心模块测试         ")
    print("="*50)

    # 1. 测试IP池添加
    # ip_pool.add_ip("127.0.0.1:7897", "socks5")  # 替换为你的测试IP
    ip_pool.add_ip("127.0.0.1:7897", "http")     # 替换为你的测试IP

    # 2. 测试IP池状态
    print("\n IP池状态：")
    print(json.dumps(ip_pool.get_pool_status(), ensure_ascii=False, indent=2))

    # 3. 测试策略更新
    print("\n 策略更新测试：")
    new_strategy = smart_strategy.update_strategy({
        "concurrent_limit": 60,
        "crawl_interval": 60,
        "target_subreddit": "Python" , # 自动转为小写
        "ip_switch_interval": 10  # 测试用：10秒切换一次
    })
    print("更新后策略：", new_strategy)

    # 4. 测试IP获取
    print("\n 随机获取有效IP：")
    print("首次判断（刚重置时间）：", smart_strategy.need_auto_switch_ip())
    time.sleep(11)  # 超过10秒切换间隔
    print("11秒后判断（应触发定时切换）：", smart_strategy.need_auto_switch_ip())
    
