"""BM25 稀疏检索：jieba 分词，兼容中英文混合文本。"""

from __future__ import annotations

import logging
import re

import jieba
import numpy as np
from rank_bm25 import BM25Okapi

jieba.setLogLevel(logging.WARNING)

_WORD = re.compile(r"\w", re.UNICODE)

# 查询中高频但没有区分度的词
STOPWORDS = {
    "的", "了", "和", "与", "及", "是", "在", "我", "你", "他", "吗", "呢", "啊", "吧",
    "一个", "一些", "什么", "怎么", "如何", "哪些", "哪个", "推荐", "几个", "动作", "练习",
    "训练", "想", "要", "可以", "适合", "有没有", "帮", "给", "请",
    "a", "an", "the", "of", "to", "and", "with", "for", "on", "in", "your", "you", "is",
    "this", "be", "it", "as", "at", "or", "by", "from", "will", "should",
}


def tokenize(text: str) -> list[str]:
    tokens = []
    for tok in jieba.lcut_for_search(text.lower()):
        tok = tok.strip()
        if not tok or tok in STOPWORDS or not _WORD.search(tok):
            continue
        tokens.append(tok)
    return tokens


class BM25Index:
    def __init__(self, ids: list[str], texts: list[str]):
        self.ids = list(ids)
        corpus = [tokenize(t) for t in texts]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(
        self, query: str, top_k: int, allowed: set[str] | None = None
    ) -> list[tuple[str, float]]:
        q_tokens = tokenize(query)
        if not q_tokens or self._bm25 is None:
            return []
        scores = self._bm25.get_scores(q_tokens)
        results: list[tuple[str, float]] = []
        for idx in np.argsort(-scores):
            score = float(scores[idx])
            if score <= 0:
                break
            doc_id = self.ids[idx]
            if allowed is not None and doc_id not in allowed:
                continue
            results.append((doc_id, score))
            if len(results) >= top_k:
                break
        return results
