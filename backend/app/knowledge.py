"""知识库：内置 BM25 检索，零外部依赖，数据持久化在 SQLite。

可扩展到向量检索（pgvector / milvus），检索接口保持不变。
"""
from __future__ import annotations

import math
import re
from collections import Counter

from . import db

SEED_DOCS = [
    {
        "title": "CSV 数据清洗规范 v1.2",
        "source": "数据工程规范",
        "tags": "清洗,数据质量",
        "content": (
            "数据清洗通用规则：一、主键为空的行直接丢弃；二、数值字段无法解析时按脏数据丢弃，"
            "不得用 0 兜底；三、金额出现负数视为异常数据，需剔除并记录数量；"
            "四、字符串字段统一 strip 去除首尾空白；五、空值统一填充为「未知」而不是 None。"
        ),
    },
    {
        "title": "统计聚合口径说明",
        "source": "业务口径文档",
        "tags": "统计,聚合",
        "content": (
            "聚合统计口径：总量为有效行金额之和，均值为「总量 / 有效行数」，必须支持空集合场景，"
            "空集合时均值取 0.0 且不得抛异常。按城市分组统计需要同时给出 count、sum、avg 三项，"
            "数值统一保留两位小数（四舍五入）。"
        ),
    },
    {
        "title": "编码规范（Python）",
        "source": "工程规范",
        "tags": "编码,规范",
        "content": (
            "Python 编码规范：模块顶部需要包含文档字符串；公共函数必须有简短注释；"
            "禁止使用裸 except；文件读写必须指定 encoding='utf-8'；"
            "边界条件（空输入、非法类型、极值）必须显式处理。"
        ),
    },
    {
        "title": "验收标准编写要求",
        "source": "测试规范",
        "tags": "验收,测试",
        "content": (
            "验收标准必须可执行、可判定：每条标准对应至少一个自动化测试用例，覆盖正常流、异常流与边界流。"
            "验收结论只能为通过或不通过，不允许模糊表述。"
        ),
    },
]

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


class KnowledgeBase:
    def __init__(self):
        self.docs: list[dict] = []
        self.df: Counter = Counter()
        self.avg_len = 0.0
        self.dirty = True

    def seed_if_empty(self, extra_docs: list[dict] | None = None):
        """按标题补齐内置知识，已存在的不重复入库。"""
        docs = list(SEED_DOCS) + list(extra_docs or [])
        for doc in docs:
            exists = db.one("SELECT id FROM kb WHERE title=?", (doc["title"],))
            if exists:
                continue
            db.execute(
                "INSERT INTO kb(title, source, content, tags, created_at) VALUES(?,?,?,?,?)",
                (doc["title"], doc["source"], doc["content"], doc["tags"], db.now()),
            )
        self.dirty = True

    def reload(self):
        rows = db.query("SELECT * FROM kb ORDER BY id")
        self.docs = []
        self.df = Counter()
        total = 0
        for row in rows:
            toks = tokenize(f"{row['title']} {row['content']} {row['tags']}")
            row["_tokens"] = toks
            row["_tf"] = Counter(toks)
            total += len(toks)
            for t in set(toks):
                self.df[t] += 1
            self.docs.append(row)
        self.avg_len = (total / len(self.docs)) if self.docs else 0.0
        self.dirty = False

    def ingest(self, title: str, content: str, source: str = "手动录入", tags: str = ""):
        db.execute(
            "INSERT INTO kb(title, source, content, tags, created_at) VALUES(?,?,?,?,?)",
            (title, source, content, tags, db.now()),
        )
        self.dirty = True

    def search(self, query: str, k: int = 3) -> list[dict]:
        if self.dirty:
            self.reload()
        if not self.docs:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        n = len(self.docs)
        k1, b = 1.5, 0.75
        scored = []
        for doc in self.docs:
            dl = len(doc["_tokens"]) or 1
            score = 0.0
            for t in set(q_tokens):
                if t not in doc["_tf"]:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                tf = doc["_tf"][t]
                score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / self.avg_len))
            if score > 0:
                scored.append((score, doc))
        scored.sort(key=lambda x: -x[0])
        return [
            {
                "id": d["id"],
                "title": d["title"],
                "source": d["source"],
                "content": d["content"],
                "score": round(s, 3),
            }
            for s, d in scored[:k]
        ]


kb = KnowledgeBase()
