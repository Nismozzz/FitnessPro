"""本轮字段级证据。快照随工具消息落库，不依赖当前知识库版本。"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

from app.llm.tokens import estimate_tokens

MARKER = re.compile(r"\[(E[A-Za-z0-9_]+)\]")
KNOWLEDGE_TOOLS = {"search_exercises", "get_exercise_detail", "search_knowledge_articles", "search_meals"}
FIELDS = {
    "name": "英文名称", "name_zh": "中文名称", "equipment": "器械",
    "level": "难度", "primary_muscles": "主要肌群", "secondary_muscles": "次要肌群",
    "mechanic": "动作类型", "category": "类别", "force": "发力方式", "aliases_zh": "中文别名",
}


def encode(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class EvidenceRegistry:
    def __init__(self) -> None:
        self.prefix = uuid.uuid4().hex[:12]
        self.evidence: dict[str, dict[str, Any]] = {}

    def restore(self, state: dict[str, Any] | None) -> None:
        if state:
            self.prefix = state.get("prefix", self.prefix)
            self.evidence = state.get("evidence", {})

    def snapshot(self) -> dict[str, Any]:
        return {"prefix": self.prefix, "evidence": self.evidence}

    def tool_content(self, data: dict, max_tokens: int) -> str:
        if "articles" in data:
            return self.article_content(data, max_tokens)
        if "meals" in data:
            return self.meal_content(data, max_tokens)
        # 按完整字段/步骤准入，永不截断 JSON 或证据原文。
        payload = {"ok": True, "data": {k: v for k, v in data.items() if k != "exercises"}}
        payload["data"].update(exercises=[], evidence=[], evidence_note="事实后引用 [证据id]；未返回的字段不可补写。")
        output = payload["data"]
        omitted = False
        docs = data.get("exercises", [])
        output["exercises"] = [{"id": doc["id"]} for doc in docs]
        # 先覆盖所有候选的名称，再逐字段补齐，避免第一条详情耗尽预算。
        for field, label in [*FIELDS.items(), ("instructions", "动作步骤")]:
            for doc, row in zip(docs, output["exercises"]):
                value = doc.get(field)
                if not value or value == "未标注":
                    continue
                values = list(enumerate(value)) if field == "instructions" else [(None, value)]
                for index, item in values:
                    if not item:
                        continue
                    evidence_id = f"E{self.prefix}_{len(self.evidence) + 1}"
                    source_field = "instructions_zh" if field == "instructions" and doc.get("instructions_language") == "zh" else field
                    location = f"{source_field}[{index}]" if index is not None else field
                    snapshot = {
                        "id": evidence_id, "exercise_id": doc["id"],
                        "title": doc.get("name_zh") or doc.get("name") or doc["id"],
                        "field": location,
                        "label": f"动作步骤第 {index + 1} 条" if index is not None else label,
                        "text": "、".join(item) if isinstance(item, list) else str(item),
                        "language": doc.get("instructions_language", "en") if index is not None else "zh" if field != "name" else "en",
                    }
                    if index is not None:
                        row.setdefault(field, []).append(item)
                        row["instructions_language"] = doc.get("instructions_language", "en")
                    else:
                        row[field] = item
                    output["evidence"].append(snapshot)
                    if estimate_tokens(encode(payload)) > max_tokens:
                        output["evidence"].pop()
                        if index is not None:
                            row[field].pop()
                        else:
                            del row[field]
                        omitted = True
                        # 步骤只保留连续前缀，保证数组索引对应原文。
                        if index is not None:
                            break
                    else:
                        self.evidence[evidence_id] = snapshot
        for doc, row in zip(docs, output["exercises"]):
            if doc.get("images"):
                row["images"] = doc["images"]
                if estimate_tokens(encode(payload)) > max_tokens:
                    del row["images"]
                    omitted = True
        if any(len(row) == 1 for row in output["exercises"]):
            omitted = True
            output["exercises"] = [row for row in output["exercises"] if len(row) > 1]
        if omitted:
            output["evidence_note"] = "部分字段因预算省略；只可引用实际返回的证据id。"
        # 极小预算下宁可返回空结果，也不留下半个证据。
        if estimate_tokens(encode(payload)) > max_tokens:
            admitted = {e["id"] for e in output["evidence"]}
            for key in admitted:
                self.evidence.pop(key, None)
            return encode({"ok": True, "data": {"exercises": [], "evidence": []}})
        return encode(payload)

    def meal_content(self, data: dict, max_tokens: int) -> str:
        payload = {"ok": True, "data": {"meals": [], "evidence": [],
                   "warnings": data.get("warnings", []), "notice": data.get("notice", ""),
                   "evidence_note": "菜谱字段引用 [证据id]；未返回的字段不可补写。"}}
        output = payload["data"]
        fields = (("name", "菜名"), ("category", "分类"), ("kcal", "热量"),
                  ("nutrition", "营养素"), ("per100g", "每100克营养素"),
                  ("servingSize", "份量"), ("mealType", "餐次"),
                  ("ingredients", "食材"), ("steps", "步骤"))
        for meal in data.get("meals", []):
            row = {"id": meal["id"]}
            for field, label in fields:
                value = meal.get(field)
                if value in (None, "", [], {}):
                    continue
                evidence_id = f"E{self.prefix}_{len(self.evidence) + 1}"
                snapshot = {"id": evidence_id, "source_type": "meal", "meal_id": meal["id"],
                            "title": meal.get("name", meal["id"]), "field": field,
                            "label": label, "text": json.dumps(value, ensure_ascii=False),
                            "language": "zh"}
                row[field] = value
                output["evidence"].append(snapshot)
                if estimate_tokens(encode(payload)) > max_tokens:
                    output["evidence"].pop(); del row[field]
                    break
                self.evidence[evidence_id] = snapshot
            if len(row) > 1:
                row["evidence_ids"] = [e["id"] for e in output["evidence"] if e["meal_id"] == meal["id"]]
                output["meals"].append(row)
        return encode(payload)

    def article_content(self, data: dict, max_tokens: int) -> str:
        payload = {"ok": True, "data": {"articles": [], "evidence": [],
                   "warnings": data.get("warnings", []), "notice": data.get("notice", ""),
                   "evidence_note": "文献结论引用 [证据id]，须保留条件、局限与待核对说明。"}}
        output = payload["data"]
        for article in data.get("articles", []):
            evidence_id = f"E{self.prefix}_{len(self.evidence) + 1}"
            snapshot = {
                "id": evidence_id, "source_type": "article", "article_id": article["article_id"],
                "chunk_id": article["id"], "title": article["title"], "authors": article["authors"],
                "source_url": article["source_url"], "field": article["id"],
                "label": article["section"], "text": article["text"], "language": "zh",
                "review_note": article["review_note"],
            }
            # 原文只存一份，降低重复元数据造成的证据预算占用。
            output["evidence"].append(snapshot)
            output["articles"].append({"id": article["id"], "evidence_id": evidence_id})
            if estimate_tokens(encode(payload)) > max_tokens:
                output["evidence"].pop()
                output["articles"].pop()
            else:
                self.evidence[evidence_id] = snapshot
        if estimate_tokens(encode(payload)) > max_tokens:
            return encode({"ok": True, "data": {"articles": [], "evidence": []}})
        return encode(payload)

    def resolve(self, content: str) -> tuple[str, list[dict], list[str]]:
        invalid: list[str] = []
        used: dict[str, dict] = {}
        def replace(match: re.Match) -> str:
            key = match[1]
            if key not in self.evidence:
                invalid.append(key)
                return ""
            used[key] = self.evidence[key]
            return match[0]
        return MARKER.sub(replace, content), list(used.values()), list(dict.fromkeys(invalid))


def history_citations(rows: list) -> dict[int, list[dict]]:
    """仅从同一轮中、回答之前的成功知识工具快照恢复引用。"""
    result = {}
    registry = EvidenceRegistry()
    turn_id = None
    for row in rows:
        if row.turn_id != turn_id:
            registry = EvidenceRegistry()
            turn_id = row.turn_id
        if row.role == "tool" and row.name in KNOWLEDGE_TOOLS:
            try:
                payload = json.loads(row.content or "{}")
                if payload.get("ok") is True:
                    for evidence in payload.get("data", {}).get("evidence", []):
                        registry.evidence[evidence["id"]] = evidence
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        elif row.role == "assistant" and not row.tool_calls:
            result[row.seq] = registry.resolve(row.content or "")[1]
    return result
