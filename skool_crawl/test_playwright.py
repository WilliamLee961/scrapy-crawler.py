from playwright.sync_api import sync_playwright
import time

def crawl_skool():
    url = "https://www.skool.com/ai-automation-society/"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # 若想后台执行，可设为 True
        page = browser.new_page()

        # 模拟浏览器 headers，防止被识别为爬虫
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })

        print(f"访问 {url} ...")
        page.goto(url, timeout=60000)
        time.sleep(5)  # 等待 JS 加载内容
        print("页面标题：", page.title())
        input("按回车关闭...")
        # # 获取 HTML
        # html = page.content()
        # with open("skool_ai_automation.html", "w", encoding="utf-8") as f:
        #     f.write(html)
        # print("✅ 已保存页面 HTML 到 skool_ai_automation.html")

        browser.close()

if __name__ == "__main__":
    crawl_skool()