import unittest
import time
import traceback
import threading  # 全局导入，解决所有函数的NameError
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime
from reddit_crawl.reddit_crawler_api import CrawlerState
from reddit_crawl.reddit_crawler import RedditCrawler
from reddit_crawl.anti_crawl_core import IPPool, SmartStrategy
from reddit_crawl.notification import load_processed_posts, save_processed_posts, log_post_info


class TestCrawlerMainLoop(unittest.TestCase):
    def setUp(self):
        """测试前初始化：创建CrawlerState实例，Mock所有外部依赖"""
        print("="*50)
        print("测试用例初始化：开始")  # 新增显式打印
        self.crawler_state = CrawlerState()
        self.test_posts = [
        {"id": "test1", "title": "Test Post 1", "created_utc": 1730280000, 
         "author": "test", "score": 10, "url": "test1.url", "comment_count": 0, 
         "content_excerpt": "test1", "top_comments": []},
        {"id": "test2", "title": "Test Post 2", "created_utc": 1730280100, 
         "author": "test", "score": 20, "url": "test2.url", "comment_count": 0, 
         "content_excerpt": "test2", "top_comments": []},
        {"id": "test3", "title": "Test Post 3", "created_utc": 1730280200, 
         "author": "test", "score": 30, "url": "test3.url", "comment_count": 0, 
         "content_excerpt": "test3", "top_comments": []},
        ]

        # 3. Mock反爬模块（ip_pool和smart_strategy）
        self.mock_ip_pool = Mock(spec=IPPool)
        self.mock_ip_pool.get_pool_status.return_value = {
            "statistics": {"valid_ip_count": 1, "total_ip_count": 1}
        }
        self.mock_ip_pool.get_current_ip.return_value = {
            "ip": "127.0.0.1:7897", "protocol": "http", "valid": True
        }
        self.mock_ip_pool.get_random_valid_ip.return_value = {
            "ip": "127.0.0.1:7897", "protocol": "http", "valid": True
        }
        
        self.mock_smart_strategy = Mock(spec=SmartStrategy)
        self.mock_smart_strategy.get_current_strategy.return_value = {
            "concurrent_limit": 50, "crawl_interval": 0.1, "ip_switch_interval": 300,
            "target_subreddit": "python", "max_posts_per_crawl": 3,
            "fail_threshold": 3, "delay_threshold": 8
        }
        self.mock_smart_strategy.need_auto_switch_ip.return_value = False
        
        # 4. Mock其他依赖（RedditCrawler、notification）
        self.mock_reddit_crawler = Mock(spec=RedditCrawler)
        self.mock_reddit_crawler.get_new_posts.return_value = self.test_posts  # 这里用self.test_posts
        
        self.mock_load_processed = Mock(return_value=["old_id1", "old_id2"])
        self.mock_save_processed = Mock()
        self.mock_log_post = Mock()
        
        print("测试用例初始化：结束")
        # ... 其他原有初始化逻辑不变 ...

    # 2. 修复test_main_loop_normal_flow的断言和Mock配置
    @patch("reddit_crawler_api.ip_pool", new_callable=lambda: Mock())
    @patch("reddit_crawler_api.smart_strategy", new_callable=lambda: Mock())
    @patch("reddit_crawler_api.RedditCrawler", new_callable=lambda: Mock())
    @patch("reddit_crawler_api.load_processed_posts")
    @patch("reddit_crawler_api.save_processed_posts")
    @patch("reddit_crawler_api.log_post_info")
    def test_main_loop_normal_flow(self, mock_log, mock_save, mock_load, mock_reddit, mock_strategy, mock_ip):
        """测试1：正常爬取流程（有IP、爬取到帖子）"""
        # 修复：配置mock_ip返回字典（而非默认Mock）
        mock_ip.get_pool_status.return_value = { "statistics": {"valid_ip_count": 1, "total_ip_count": 1}}
        mock_ip.get_current_ip.return_value = {"ip": "127.0.0.1:7897", "protocol": "http", "valid": True}
        mock_ip.get_random_valid_ip.return_value = {"ip": "127.0.0.1:7897", "protocol": "http", "valid": True}
        mock_strategy.get_current_strategy.return_value = {
            "concurrent_limit": 50, "crawl_interval": 1, "target_subreddit": "python", "max_posts_per_crawl": 3
        }
        mock_strategy.need_auto_switch_ip.return_value = False
        mock_reddit.return_value.get_new_posts.return_value = self.test_posts
        mock_load.return_value = ["old_id1"]
        mock_save.side_effect = self.mock_save_processed

        # 启动爬虫线程
        self.crawler_state.state["is_running"] = True
        loop_thread = threading.Thread(
            target=self.crawler_state._crawler_main_loop,
            daemon=True
        )
        loop_thread.start()
        time.sleep(0.5)  # 等待循环执行
        self.crawler_state.state["is_running"] = False
        loop_thread.join(timeout=2)

        # 修复：去掉basic_status层级，直接访问扁平键
        self.assertEqual(self.crawler_state.state["current_concurrent"], 0)  # 正确访问
        self.assertEqual(self.crawler_state.state["total_crawled_posts"], 3)  # 正确访问
        saved_ids = self.mock_save_processed.call_args[0][0]
        self.assertIsInstance(saved_ids, list, "save_processed_posts的参数必须是列表！")
        self.assertIn("test1", saved_ids)
        self.assertEqual(mock_log.call_count, 3)

    # 3. 修复其他测试用例的threading和state访问（以test_concurrent_limit为例）
    @patch("reddit_crawler_api.smart_strategy", new_callable=lambda: Mock())
    @patch("reddit_crawler_api.ip_pool", new_callable=lambda: Mock())
    def test_concurrent_limit(self, mock_ip, mock_strategy):
        """测试4：验证并发限制"""
        print("=== 开始执行【并发限制】测试 ===")
        mock_ip.get_random_valid_ip.return_value = {
        "ip": "127.0.0.1:7897", 
        "protocol": "http", 
        "valid": True
        }
        mock_ip.get_current_ip.return_value = {
        "ip": "127.0.0.1:7897", 
        "protocol": "http", 
        "valid": True
        }
        mock_ip.get_pool_status.return_value = {"statistics": {"valid_ip_count": 1, "total_ip_count": 1}}
        mock_strategy.need_auto_switch_ip.return_value = False  
        mock_strategy.get_current_strategy.return_value = {
            "concurrent_limit": 0, 
            "crawl_interval": 1, 
            "target_subreddit": "python",
            "max_posts_per_crawl": 3
        }
        
        self.crawler_state.state["is_running"] = True
        # 此处threading已全局导入，不再报错
        loop_thread = threading.Thread(target=self.crawler_state._crawler_main_loop, daemon=True)
        loop_thread.start()

        time.sleep(0.5)
        self.crawler_state.state["is_running"] = False
        loop_thread.join(timeout=2)

        # 修复：直接访问total_crawled_posts
        self.assertEqual(self.crawler_state.state["current_concurrent"], 0)
        self.assertEqual(self.crawler_state.state["total_crawled_posts"], 0)

if __name__ == "__main__":
    # 运行所有测试用例，显示详细日志
    unittest.main(verbosity=2)