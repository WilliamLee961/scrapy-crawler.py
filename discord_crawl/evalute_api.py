import openai
from sklearn.metrics import precision_score, recall_score
from typing import List
import numpy as np

openai.api_key = "YOUR_API_KEY"

######################
# Step 1: 定义评估指标
######################
def evaluate_report(report_text: str, task_desc: str) -> dict:
    prompt = f"""
    你是一个文本评估专家。
    任务描述: {task_desc}
    文本报告: {report_text}

    请从以下维度进行评分（0-100），并解释原因：
    1. 准确性
    2. 完整性
    3. 可读性
    4. 相关性
    5. 简洁性
    
    返回JSON格式，包含每个维度的得分和解释，以及最终加权总分。
    """
    resp = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    return resp.choices[0].message["content"]

######################
# Step 2: RAG 搜索能力评估
######################
def simulate_rag_search(query: str, corpus: List[str], top_k=3) -> List[str]:
    # 这里用简单关键词匹配来模拟检索
    results = [doc for doc in corpus if query.lower() in doc.lower()]
    return results[:top_k]

def search_eval(found_docs: List[str], gold_docs: List[str]) -> dict:
    # 为简单起见，把每篇文档是否匹配视作二分类
    found_set = set(found_docs)
    gold_set = set(gold_docs)
    y_true = [doc in gold_set for doc in found_docs]
    y_pred = [True] * len(found_docs)  # 假设都选中
    return {
        "precision": precision_score(y_true, y_pred),
        "recall": recall_score(y_true, y_pred)
    }

######################
# Step 3: 文本生成
######################
def generate_report(facts: List[str], task_desc: str) -> str:
    prompt = f"""
    使用以下事实为任务“{task_desc}”生成一个结构良好、准确、简洁的报告：
    {facts}

    遵循流程：
    1. Extract: 提取关键信息
    2. Excelsior: 精炼语言
    3. Expand: 增加必要的背景信息
    """
    resp = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7
    )
    return resp.choices[0].message["content"]

######################
# Example Run
######################
corpus_data = [
    "Discord 社群发布了新的 API 更新文档",
    "用户 Alice 在频道中提出关于爬虫合规性的问题",
    "开发者社区讨论了 Playwright 与人工验证的关系"
]

gold_info = ["Discord 社群发布了新的 API 更新文档", "用户 Alice 在频道中提出关于爬虫合规性的问题"]

# Step 2: 搜索
found = simulate_rag_search("爬虫", corpus_data)
search_metrics = search_eval(found, gold_info)
print("搜索能力评估:", search_metrics)

# Step 3: 生成报告
report = generate_report(found, "评估 Discord 爬虫文本报告的好坏")
print("\n生成报告:\n", report)

# Step 1: 评估报告
evaluation = evaluate_report(report, "评估 Discord 爬虫文本报告的好坏")
print("\n报告自动评估:\n", evaluation)


# 评估指标优化：利用人工标注集验证评分指标与人工一致性（参考 APPLS 方法）。
# 多模态扩展：结合图片 / 截屏的信息进行报告生成和评估（参考 FLEUR 的多模态评估）。
# 搜索能力增强：替换简单关键词为向量检索（FAISS / Pinecone），评估准确率、召回率及 RAG 响应质量。
# 可解释自动化 pipeline：结合 LLM 的 Chain-of-Thought 输出，让评分理由更加透明。