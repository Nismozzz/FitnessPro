from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field


class ArticleChunk(BaseModel):
    id: str
    article_id: str
    title: str
    authors: str
    source_url: str
    source_file: str
    category: str = ""
    section: str = ""
    text: str = Field(min_length=1)
    review_note: str = "用户整理的文献摘录，未核对全文；保留研究条件与局限，不将统计不显著解释为等效。"

    def search_text(self) -> str:
        return f"{self.title}\n{self.category}\n{self.section}\n{self.text}"


def recursive_split(text: str, size: int = 900, separators: tuple[str, ...] = ("\n\n", "\n", "。", "；", " ")) -> list[str]:
    """保留分隔符，优先完整段落，超长段落逐级切分，最终按字符兜底。"""
    if size < 100:
        raise ValueError("chunk_size 必须 >= 100")
    if len(text) <= size:
        return [text] if text else []
    if not separators:
        return [text[i:i + size] for i in range(0, len(text), size) if text[i:i + size]]
    sep, *rest = separators
    pieces = re.split(f"({re.escape(sep)})", text)
    units = ["".join(pieces[i:i + 2]) for i in range(0, len(pieces), 2)]
    output, pending = [], ""
    for unit in units:
        if len(pending) + len(unit) <= size:
            pending += unit
            continue
        if pending:
            output.append(pending)
        pending = ""
        if len(unit) > size:
            output.extend(recursive_split(unit, size, tuple(rest)))
        else:
            pending = unit
    if pending:
        output.append(pending)
    return output


def read_articles(raw_dir: Path, chunk_size: int = 900) -> list[ArticleChunk]:
    metadata_file = raw_dir.parent / 'sources.json'
    supplements = json.loads(metadata_file.read_text(encoding='utf-8')) if metadata_file.exists() else {}
    chunks = []
    for path in sorted(raw_dir.glob('*.md')):
        content = path.read_text(encoding='utf-8')
        match = re.match(r'\A---\s*\n(.*?)\n---\s*\n(.*)\Z', content, re.S)
        if not match:
            raise ValueError(f"{path.name}: 缺少 YAML 元数据")
        meta = yaml.safe_load(match[1])
        if not isinstance(meta, dict):
            raise ValueError(f"{path.name}: 元数据必须为对象")
        meta = {**meta, **supplements.get(path.name, {})}
        for key in ('title', 'authors', 'source_url'):
            if not isinstance(meta.get(key), str) or not meta[key].strip():
                raise ValueError(f"{path.name}: 缺少 {key}")
        parsed = urlsplit(meta['source_url'])
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"{path.name}: 来源须为有效 HTTPS 链接")
        article_id = 'article_' + hashlib.sha256(path.name.encode()).hexdigest()[:16]
        heading = '正文'
        sections = re.split(r'(?m)^(#{1,6} .*)$', match[2])
        for part in sections:
            if re.match(r'^#{1,6} ', part):
                heading = part.lstrip('# ').strip()
                continue
            for text in recursive_split(part.strip(), chunk_size):
                text = text.strip()
                if not text:
                    continue
                digest = hashlib.sha256(f'{heading}\n{text}'.encode()).hexdigest()[:16]
                chunks.append(ArticleChunk(
                    id=f'{article_id}_{digest}', article_id=article_id,
                    title=meta['title'], authors=meta['authors'], source_url=meta['source_url'],
                    source_file=path.name, category=meta.get('category', ''), section=heading, text=text,
                    **({'review_note': meta['review_note']} if meta.get('review_note') else {}),
                ))
    if not chunks:
        raise ValueError("未找到可入库的 Markdown 文献")
    if len({c.id for c in chunks}) != len(chunks):
        raise ValueError("出现重复文献片段，请去重后入库")
    return chunks
