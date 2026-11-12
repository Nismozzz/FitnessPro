"""
Token 估算。

不同模型的分词器不同，这里用保守估算：中日韩字符按 1 token，其余按 4 字符 1 token。
用于预算分配和截断，不用于计费。
"""

import json
import math
import re

_CJK = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")


def estimate_tokens(text: str | None) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return cjk + math.ceil((len(text) - cjk) / 4)


def message_tokens(message: dict) -> int:
    total = 4  # 角色与分隔符开销
    content = message.get("content")
    if isinstance(content, str):
        total += estimate_tokens(content)
    elif content is not None:
        total += estimate_tokens(json.dumps(content, ensure_ascii=False))
    if message.get("tool_calls"):
        total += estimate_tokens(json.dumps(message["tool_calls"], ensure_ascii=False))
    return total


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """按估算 token 截断，并标注原始长度，让模型知道内容不完整。"""
    total = estimate_tokens(text)
    if total <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + f"…[已截断，原文约 {total} tokens]"
