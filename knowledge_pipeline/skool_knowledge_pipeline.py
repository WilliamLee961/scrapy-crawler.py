"""
skool_knowledge_pipeline.py

工程化的知识库构建与服务管线（基于已有 skool_crawler_doubao.py）
功能：
 1) 从 Skool 抓取或读取已抓取的帖子（CSV/JSON），归一化并保存到 MySQL（原始数据层）
 2) 从 MySQL 读取原始帖子，执行“知识提取算法”（幽默命名：memetic_distiller），
    将抽象知识与事件写入 Neo4j 图数据库
 3) 提供 FastAPI 接口：触发整合、触发知识构建、查询知识、查看状态与日志
 4) 基本监控/指标（延迟/响应时间）与日志

说明：
 - 依赖： sqlalchemy, mysqlclient 或 pymysql, neo4j, fastapi, uvicorn
 - 设计遵循模块化、单职责：
     config, storage_mysql, storage_neo4j, extractor, orchestrator, api
 - 使用示例：
     uvicorn skool_knowledge_pipeline:app --host 0.0.0.0 --port 9000

注意：示例中尽量使用轻量依赖和内置方法，便于在你的环境直接运行/调整。
"""

import os
import time
import json
import logging
import threading
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

# ---------- 配置 ----------
from pydantic import BaseSettings

class Settings(BaseSettings):
    # MySQL
    MYSQL_URL: str = "mysql+pymysql://user:pass@127.0.0.1:3306/skool_kg?charset=utf8mb4"
    # Neo4j
    NEO4J_URI: str = "bolt://127.0.0.1:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "password"
    # 数据输入目录（也可以从 skool crawler 直接入库）
    INPUT_CSV_DIR: str = "./data_csv"
    # 服务
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 9000
    # 调度
    AGGREGATION_INTERVAL_SECONDS: int = 60 * 30  # 每30分钟一次（目标：聚合延迟<=1小时）

    class Config:
        env_file = ".env"

settings = Settings()

# ---------- 日志 ----------
LOG_DIR = "./logs"
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "pipeline.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("skool_kg")

# ---------- MySQL 存储层（原始数据） ----------
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Table, MetaData
from sqlalchemy.orm import sessionmaker, declarative_base

engine = create_engine(settings.MYSQL_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

class RawPost(Base):
    __tablename__ = "raw_posts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    post_id = Column(String(255), nullable=True, index=True)  # platform id
    title = Column(String(1024), nullable=True)
    author = Column(String(256), nullable=True, index=True)
    url = Column(String(2048), nullable=True)
    fetched_at = Column(DateTime, nullable=False)
    content = Column(Text, nullable=True)
    metadata = Column(Text, nullable=True)  # json string for other fields

def init_mysql():
    Base.metadata.create_all(bind=engine)
    logger.info("MySQL 表已初始化")

# ---------- Neo4j 存储层（知识库/图数据库） ----------
from neo4j import GraphDatabase

class Neo4jStore:
    def __init__(self, uri, user, password):
        self.driver = GraphDatabase.driver(uri, auth=(user, password), max_connection_lifetime=60*60)

    def close(self):
        self.driver.close()

    def write_knowledge(self, abstracts: List[Dict[str, Any]], events: List[Dict[str, Any]]):
        """把抽象知识（属性/概念）和事件写入图数据库。"""
        with self.driver.session() as sess:
            for a in abstracts:
                # 抽象知识节点：Concept
                sess.write_transaction(self._create_concept_if_not_exists, a)
            for e in events:
                sess.write_transaction(self._create_event, e)

    @staticmethod
    def _create_concept_if_not_exists(tx, concept: Dict[str, Any]):
        # concept: {name, attr: {k:v}, source_count}
        q = (
            "MERGE (c:Concept {name: $name})"
            " ON CREATE SET c.created_at = datetime(), c.source_count = COALESCE($source_count,1)"
            " ON MATCH SET c.source_count = coalesce(c.source_count,0) + COALESCE($source_count,1)"
        )
        tx.run(q, name=concept.get("name"), source_count=concept.get("source_count", 1))
        # 属性为独立节点并建立关系
        attrs = concept.get("attributes") or {}
        for k, v in attrs.items():
            tx.run(
                "MERGE (p:Property {name:$pname}) MERGE (c:Concept {name:$cname}) MERGE (c)-[r:HAS_PROPERTY]->(p) ON CREATE SET r.created_at=datetime()",
                pname=f"{k}:{v}", cname=concept.get("name")
            )

    @staticmethod
    def _create_event(tx, event: Dict[str, Any]):
        # event: {id, title, text, time, related:[entity_names]}
        q = (
            "MERGE (e:Event {event_id:$event_id})"
            " ON CREATE SET e.title=$title, e.text=$text, e.time=$time, e.created_at=datetime()"
            " ON MATCH SET e.text = $text"
        )
        tx.run(q, event_id=event.get("id"), title=event.get("title"), text=event.get("text"), time=event.get("time"))

        # 关联到概念
        for r in event.get("related", []):
            tx.run(
                "MERGE (c:Concept {name:$cname}) MERGE (e:Event {event_id:$event_id}) MERGE (e)-[:ASSOCIATED_WITH]->(c)",
                cname=r, event_id=event.get("id")
            )

neo4j_store = Neo4jStore(settings.NEO4J_URI, settings.NEO4J_USER, settings.NEO4J_PASSWORD)

# ---------- 知识提取器（memetic distiller） ----------
# 一个带点幽默与创新命名的轻量抽取器：
#  - 基础功能：分句、关键词抽取、实体候选（名词片段）、事件化（包含时间/动作）
#  - 新颖点/搞笑点：使用 "meme_score" 概念（词频 * 情感权重 (简单规则)），用于排序候选知识
# 注意：真实大规模场景应接入专用 NLP（spaCy/transformers）与去噪/去重流程

import math
import re

SENTENCE_SPLIT_RE = re.compile(r"(?<=。|！|？|\.|!|\?)\s*")
WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+")

STOPWORDS = set(["的","了","在","是","和","与","我们","你","我"])


def simple_tokenize(text: str) -> List[str]:
    return WORD_RE.findall(text)


def extract_candidates(text: str) -> List[str]:
    tokens = simple_tokenize(text)
    freq = {}
    for t in tokens:
        if len(t) < 2: continue
        if t in STOPWORDS: continue
        freq[t] = freq.get(t, 0) + 1
    # top tokens
    top = sorted(freq.items(), key=lambda x: -x[1])[:30]
    return [t for t, c in top]


def memetic_distiller(posts: List[Dict[str, Any]]) -> (List[Dict[str,Any]], List[Dict[str,Any]]):
    """将若干原始帖子提炼为抽象概念(abstracts)与事件(events).
    返回：(abstracts, events)
    抽象知识样例: {name: '微前端', attributes: {'trend': '高', 'mentions': 12}, source_count: N}
    事件样例: {id: 'post_123', title: 'xxx', text: '...', time: '2025-10-29T...', related: ['微前端']}
    """
    all_text = "\n\n".join([p.get("content","") or p.get("excerpt","") for p in posts])
    candidates = extract_candidates(all_text)

    # 1) 构造抽象知识：将高频词作为概念，计算简单属性
    abstracts = []
    token_counts = {t: all_text.count(t) for t in candidates}
    total_tokens = sum(token_counts.values()) or 1
    for token, cnt in token_counts.items():
        meme_score = cnt * math.log(1 + cnt)
        attr = {
            "mentions": cnt,
            "share": round(cnt/total_tokens, 6),
            "meme_score": round(meme_score, 3)
        }
        abstracts.append({
            "name": token,
            "attributes": attr,
            "source_count": cnt
        })

    # 2) 事件化：把每条 post 当作一个事件，尝试找出相关概念
    events = []
    for p in posts:
        pid = p.get('post_id') or f"p_{hash(p.get('url') or p.get('title') or time.time())}"
        text = (p.get('content') or p.get('excerpt') or '')[:10000]
        related = [c for c in candidates if c in text]
        # 时间解析简单化
        time_str = p.get('fetched_at') or datetime.now().isoformat()
        events.append({
            "id": pid,
            "title": p.get('title') or text[:80],
            "text": text,
            "time": time_str,
            "related": related
        })

    # 简单去重与排序：按 meme_score 降序，限制数量
    abstracts.sort(key=lambda x: -x['attributes']['meme_score'])
    return abstracts[:2000], events  # 上限以防爆炸

# ---------- 管道与编排器 ----------
class PipelineState:
    def __init__(self):
        self.last_aggregate_time: Optional[datetime] = None
        self.last_kg_build_time: Optional[datetime] = None
        self.lock = threading.Lock()
        self.logs: List[str] = []

    def add_log(self, s: str):
        t = datetime.now().isoformat()
        msg = f"[{t}] {s}"
        logger.info(msg)
        with self.lock:
            self.logs.append(msg)
            if len(self.logs) > 2000:
                self.logs = self.logs[-2000:]

    def get_recent_logs(self, n=200):
        with self.lock:
            return list(self.logs[-n:])

pipeline_state = PipelineState()


def ingest_csvs_to_mysql(input_dir: str = settings.INPUT_CSV_DIR, source='skool') -> int:
    """读取目录下 CSV/JSON 文件，将条目写入 MySQL raw_posts。返回写入条数。"""
    import glob, csv
    files = glob.glob(os.path.join(input_dir, "*.csv")) + glob.glob(os.path.join(input_dir, "*.json"))
    if not files:
        pipeline_state.add_log("没有找到输入文件")
        return 0
    cnt = 0
    session = SessionLocal()
    try:
        for fpath in files:
            pipeline_state.add_log(f"开始处理文件: {fpath}")
            if fpath.endswith('.csv'):
                with open(fpath, 'r', encoding='utf-8') as fh:
                    reader = csv.DictReader(fh)
                    for r in reader:
                        post = RawPost(
                            source=source,
                            post_id=r.get('id') or r.get('url'),
                            title=r.get('title'),
                            author=r.get('author'),
                            url=r.get('url'),
                            fetched_at=datetime.fromisoformat(r.get('fetched_at')) if r.get('fetched_at') else datetime.now(),
                            content=r.get('content'),
                            metadata=json.dumps({k: v for k, v in r.items() if k not in ['content']})
                        )
                        session.merge(post)
                        cnt += 1
            else:
                with open(fpath, 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                    items = data if isinstance(data, list) else (data.get('posts') or [data])
                    for r in items:
                        post = RawPost(
                            source=source,
                            post_id=str(r.get('id') or r.get('url') or ''),
                            title=r.get('title'),
                            author=r.get('author'),
                            url=r.get('url'),
                            fetched_at=datetime.fromisoformat(r.get('fetched_at')) if r.get('fetched_at') else datetime.now(),
                            content=r.get('content'),
                            metadata=json.dumps(r)
                        )
                        session.merge(post)
                        cnt += 1
        session.commit()
        pipeline_state.add_log(f"已将 {cnt} 条记录写入 MySQL")
        pipeline_state.last_aggregate_time = datetime.now()
        return cnt
    except Exception as e:
        session.rollback()
        pipeline_state.add_log(f"写入 MySQL 失败: {e}")
        raise
    finally:
        session.close()


def build_knowledge_from_mysql(batch_limit=1000):
    """从 MySQL 读取最新原始数据，执行知识提取并写入 Neo4j。"""
    t0 = time.time()
    session = SessionLocal()
    try:
        rows = session.query(RawPost).order_by(RawPost.fetched_at.desc()).limit(batch_limit).all()
        posts = []
        for r in rows:
            posts.append({
                'post_id': r.post_id,
                'title': r.title,
                'author': r.author,
                'url': r.url,
                'fetched_at': r.fetched_at.isoformat() if r.fetched_at else None,
                'content': r.content,
                'metadata': json.loads(r.metadata) if r.metadata else {}
            })
        pipeline_state.add_log(f"从 MySQL 读取 {len(posts)} 条用于知识构建")
        abstracts, events = memetic_distiller(posts)
        pipeline_state.add_log(f"抽取到 {len(abstracts)} 个抽象概念，{len(events)} 个事件")
        neo4j_store.write_knowledge(abstracts, events)
        pipeline_state.last_kg_build_time = datetime.now()
        dt = time.time() - t0
        pipeline_state.add_log(f"知识构建完成，用时 {dt:.3f}s")
        return {'abstracts': len(abstracts), 'events': len(events), 'duration_s': dt}
    except Exception as e:
        pipeline_state.add_log(f"知识构建失败: {e}")
        raise
    finally:
        session.close()

# ---------- 外部API（FastAPI） ----------
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Skool Knowledge Pipeline", version="0.1")

class IngestRequest(BaseModel):
    input_dir: Optional[str] = None

class BuildRequest(BaseModel):
    batch_limit: Optional[int] = 1000

@app.post("/api/ingest")
def api_ingest(req: IngestRequest):
    d = req.input_dir or settings.INPUT_CSV_DIR
    try:
        n = ingest_csvs_to_mysql(d)
        return {"ok": True, "inserted": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/build_knowledge")
def api_build(req: BuildRequest):
    try:
        res = build_knowledge_from_mysql(batch_limit=req.batch_limit)
        return {"ok": True, "result": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/status")
def api_status():
    return {
        "last_aggregate_time": pipeline_state.last_aggregate_time.isoformat() if pipeline_state.last_aggregate_time else None,
        "last_kg_build_time": pipeline_state.last_kg_build_time.isoformat() if pipeline_state.last_kg_build_time else None,
        "recent_logs": pipeline_state.get_recent_logs(50)
    }

@app.get("/api/query_concept/{name}")
def api_query_concept(name: str):
    # 查询 Neo4j 中的概念与简单邻居
    try:
        q = (
            "MATCH (c:Concept {name:$name}) OPTIONAL MATCH (c)-[r]-(n) RETURN c, collect(distinct n) as neigh LIMIT 1"
        )
        with neo4j_store.driver.session() as sess:
            r = sess.run(q, name=name).single()
            if not r:
                raise HTTPException(status_code=404, detail="未找到概念")
            c = r["c"]
            neigh = r["neigh"]
            return {"concept": dict(c), "neighbors": [dict(n) for n in neigh]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ---------- 简单调度线程 ----------
_stop_event = threading.Event()

def scheduler_loop():
    """定期执行：CSV->MySQL（聚合）与知识构建。目标聚合延迟 <= 1 小时。
    """
    logger.info("调度器已启动")
    while not _stop_event.is_set():
        try:
            t0 = time.time()
            n = ingest_csvs_to_mysql(settings.INPUT_CSV_DIR)
            if n > 0:
                build_knowledge_from_mysql(batch_limit=2000)
            # sleep until next run
            dt = time.time() - t0
            sleep_for = max(5, settings.AGGREGATION_INTERVAL_SECONDS - dt)
            logger.info(f"本轮完成，sleep {sleep_for}s")
            _stop_event.wait(timeout=sleep_for)
        except Exception as e:
            logger.exception("调度器异常")
            _stop_event.wait(timeout=60)

@app.on_event("startup")
def startup_event():
    init_mysql()
    # 启动后台调度
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()
    pipeline_state.add_log("后台调度已启动")

@app.on_event("shutdown")
def shutdown_event():
    _stop_event.set()
    neo4j_store.close()
    pipeline_state.add_log("服务关闭，资源已释放")

# ---------- 工程提示 ----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.API_HOST, port=settings.API_PORT)
