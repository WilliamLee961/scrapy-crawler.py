skool_crawler.py功能：
- 交互式登录并保存 storage_state.json（Playwright）
- 抓取 group 列表页（滚动加载）并保存渲染后的 HTML
- 解析列表页为帖子（title, url, author, time, comments, likes, excerpt, preview_url, post_id）
- 抓取每条帖子的详情页（正文 + 评论）
- 为最新评论生成摘要（可选调用 OpenAI）
- 保存为 CSV / SQLite

用法示例：
# 先交互登录并保存 session（在浏览器里手动完成登录）
python skool_crawler.py login --storage_state skool_state.json

# 抓取列表页 HTML（保存到 skool_{group}.html）
python skool_crawler.py fetch-list --group ai-automation-society --storage_state skool_state.json

# 解析已抓取的 HTML 并保存 CSV + SQLite
python skool_crawler.py parse-list --group ai-automation-society --html skool_ai_automation.html

# 抓取详情与评论（并保存）
python skool_crawler.py fetch-details --group ai-automation-society --storage_state skool_state.json --limit 20

# 对某个帖子的评论摘要（使用 OPENAI_API_KEY 或 fallback）
python skool_crawler.py summarize --group ai-automation-society --limit 10 --openai_key $OPENAI_KEY

# 一键全部运行（登录 -> fetch list -> parse -> fetch details -> summarize）
python skool_crawler.py run_all --group ai-automation-society --storage_state skool_state.json --comment_limit 40 --openai_key None --debug
"""