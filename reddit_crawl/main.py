from reddit_crawl.reddit_crawler import RedditCrawler
from reddit_crawl.notification import load_processed_posts, save_processed_posts, push_email,log_post_info
import time
import traceback

def main():
    try:
        # 1. 初始化爬虫
        crawler = RedditCrawler(proxy_host="127.0.0.1", proxy_port=7897)
        print("🎉 Reddit API 连接成功，开始监控新内容...")
    
        # 2. 配置推送（以邮件为例，替换为实际SMTP信息）
        # smtp_config = (
        #     "smtp.example.com", 587,
        #     "williamlee2002@sjtu.edu.cn", "Leejh20020916",
        #     "williamlee2002@sjtu.edu.cn", "18016097061@163.com"
        # )

        # 3. 加载已处理的帖子ID
        processed_ids = load_processed_posts()
        print(f"ℹ️ 加载到已处理ID共 {len(processed_ids)} 条，前20个ID：{processed_ids[:20]}")
        interval = 60 # 检测间隔（秒），控制延迟
        max_log = 10   # 单次最多推送条数

        # 4. 持续监控循环
        while True:
            new_posts = crawler.get_new_posts("python", limit=10)
            # 新增：打印本次获取的帖子数量和ID
            print(f"🔍 本次获取到 {len(new_posts)} 条帖子，ID列表：{[post.get('id', '未知') for post in new_posts]}")
            new_undetected = []

            # 筛选“未推送过的新帖子”
            for post in new_posts: 
                post_id = str(post.get("id", ""))
                if post_id  and post_id not in processed_ids: # 说明是新的帖子
                    new_undetected.append(post)
                    processed_ids.append(post_id)
                    if len(new_undetected) >= max_log:
                        break
            
            # 推送新内容
            if new_undetected:
                print(f"🚨 检测到 {len(new_undetected)} 条新帖子，开始记录日志...")
                for post in new_undetected:
                    # push_email(post, smtp_config)
                    log_post_info(post) # 调用日志参数·
                save_processed_posts(processed_ids)
            else:
                print(f"⏳ 暂无新内容，{interval}秒后再次检测...")

            time.sleep(interval)
    except KeyboardInterrupt:
        print("🛑 用户中断程序")
    except Exception as e:
        print(f"❌ 运行出错：{e}")
        traceback.print_exc()     


    
if __name__ == "__main__":
    main()