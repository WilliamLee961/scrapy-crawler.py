# 可以从已保存的 HTML 中自动判断最可能的排序策略
# 会计算几种候选排序与当前 DOM 顺序的一致度评分
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import re

PATH = "skool_ai_automation.html"
now = datetime.now()

def parse_relative_time(text):
    if not text: return None
    s = text.lower()
    # 常见形式： "4h ago", "2d", "New comment 4h ago", "2d •"
    m = re.search(r"(\d+)\s*(s|sec|seconds?|m|min|minutes?|h|hr|hours?|d|day|days?|w|week|weeks?|mo|month|months?|y|year|years?)", s)
    if m:
        n = int(m.group(1))
        u = m.group(2)[0]
        if u == 's': return now - timedelta(seconds=n)
        if u == 'm': return now - timedelta(minutes=n)
        if u == 'h': return now - timedelta(hours=n)
        if u == 'd': return now - timedelta(days=n)
        if u == 'w': return now - timedelta(weeks=n)
        if u == 'y': return now - timedelta(days=365*n)
        # month fallback ~30 days
        if u == 'o': return now - timedelta(days=30*n)
    # 试 ISO / 年月日
    try:
        if re.match(r"\d{4}-\d{1,2}-\d{1,2}", s):
            return datetime.fromisoformat(re.search(r"\d{4}-\d{1,2}-\d{1,2}", s).group(0))
    except Exception:
        pass
    return None

with open(PATH, "r", encoding="utf-8") as f:
    soup = BeautifulSoup(f, "lxml")

# 抽出所有帖子容器（回退到以 href 为准）
posts = soup.select("div[class*='PostItemWrapper']")
if not posts:
    posts = soup.select("div[class*='PostItemCardWrapper'], div[class*='PostItemCardContent']")
if not posts:
    # fallback 用 /group-slug/ 开头的链接当作帖子索引
    anchors = [a for a in soup.find_all("a", href=True) if a["href"].startswith("/ai-automation-society/") and len(a.get_text(strip=True)) > 3]
    posts = anchors

items = []
for i, p in enumerate(posts):
    # p 可能是 <div> 或 <a>
    parent = p if p.name == "div" else p.find_parent("div") or p
    # pinned?
    pinned = bool(parent.select_one(".Pinned") or parent.select_one("[class*='PinnedOverlay']") or (parent.get_text().lower().find("pinned")!=-1))
    # created time
    created_node = parent.select_one("[class*='PostTimeContent'], [class*='PostTime']")
    created = parse_relative_time(created_node.get_text(" ", strip=True)) if created_node else None
    # recent activity label
    recent_node = parent.select_one("[class*='RecentActivityLabel']")
    recent = parse_relative_time(recent_node.get_text(" ", strip=True)) if recent_node else None
    # id (用 href 或 title 组合)
    a = parent.find("a", href=True)
    pid = a["href"] if a else f"idx_{i}"
    items.append({"idx": i, "pid": pid, "pinned": pinned, "created": created, "recent": recent})

# 现在对比几种排序策略与 DOM 顺序的一致性（用简单的 footrule 距离衡量）
def score_order(key_func):
    sorted_items = sorted(items, key=key_func, reverse=True)
    order_map = {it['pid']: pos for pos, it in enumerate(sorted_items)}
    n = len(items)
    # footrule: sum |orig_pos - new_pos|
    s = 0
    for it in items:
        s += abs(it['idx'] - order_map[it['pid']])
    max_possible = n*(n-1)  # loose upper bound
    score = 1 - (s / max_possible)
    return score, sorted_items

candidates = {}
# 1 pinned first (True>False), then recent (recent time)
candidates['pinned_then_recent'] = lambda it: (1 if it['pinned'] else 0, it['recent'] or datetime.min)
candidates['recent'] = lambda it: (it['recent'] or datetime.min)
candidates['created'] = lambda it: (it['created'] or datetime.min)
candidates['dom'] = lambda it: -it['idx']  # dom original

for name, func in candidates.items():
    sc, sorted_list = score_order(func)
    print(f"{name}: score={sc:.4f}")

# 结果里 score 越接近 1 表示该排序与页面 DOM 越一致
