"""
动作知识库入库脚本（free-exercise-db）。

步骤：
1. 下载 / 读取 dist/exercises.json 并做字段校验
2. 用 fast LLM 批量生成中文名、别名和中文动作要点（结果缓存在 data/exercises_zh.json，可断点续跑）
3. 写入数据库 exercises 表（原文 / 父文档）
4. 生成检索文本 → Embedding → 保存本地向量索引

用法（在项目根目录）：
    python -m scripts.ingest_exercises                 # 完整流程
    python -m scripts.ingest_exercises --limit 20      # 先用少量数据试跑
    python -m scripts.ingest_exercises --skip-translate --skip-embed   # 仅英文 + BM25
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.config import resolve_path, settings
from app.database.models import Exercise
from app.database.session import SessionLocal, init_db
from app.llm.provider import get_llm
from app.rag.embeddings import create_embedder
from app.rag.schema import ExerciseDoc
from app.rag.vector_store import LocalVectorStore
from app.utils.time import utcnow

logger = logging.getLogger("ingest")


class RawExercise(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    force: str | None = None
    level: str
    mechanic: str | None = None
    equipment: str | None = None
    primary_muscles: list[str] = Field(default_factory=list, alias="primaryMuscles")
    secondary_muscles: list[str] = Field(default_factory=list, alias="secondaryMuscles")
    instructions: list[str] = Field(default_factory=list)
    category: str
    images: list[str] = Field(default_factory=list)


class Translation(BaseModel):
    id: str
    name_zh: str = Field(min_length=1, max_length=100)
    aliases_zh: list[str] = Field(default_factory=list, max_length=5)
    instructions_zh: list[str] = Field(default_factory=list)


TRANSLATE_SYSTEM = """你是专业健身教练兼中英翻译。请把给定的英文健身动作数据翻译成简体中文：
1. name_zh：使用中文健身圈的常用叫法（例如 Barbell Bench Press → 杠铃卧推），不确定时准确直译；
2. aliases_zh：0~3 个常见中文别名或俗称，没有则返回空数组；
3. instructions_zh：逐条翻译动作步骤，条数与原文一致，语言简洁准确，保留关键的安全提示；
4. id 原样保留。只输出 JSON，格式：
{"items": [{"id": "...", "name_zh": "...", "aliases_zh": ["..."], "instructions_zh": ["..."]}]}"""


async def load_raw(source: str | None, refresh: bool) -> list[dict[str, Any]]:
    if source and not source.startswith("http"):
        return json.loads(Path(source).read_text(encoding="utf-8"))

    raw_path = resolve_path(settings.data.raw_path)
    if raw_path.exists() and not refresh:
        logger.info("使用本地数据 %s", raw_path)
        return json.loads(raw_path.read_text(encoding="utf-8"))

    url = source or settings.data.exercises_url
    logger.info("下载 %s", url)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
    data = resp.json()
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def parse_raw(items: list[dict[str, Any]]) -> list[RawExercise]:
    parsed, seen = [], set()
    for item in items:
        try:
            ex = RawExercise.model_validate(item)
        except ValidationError as exc:
            logger.warning("跳过无效数据 %s：%s", item.get("id"), exc.errors()[:1])
            continue
        if ex.id in seen:
            continue
        seen.add(ex.id)
        parsed.append(ex)
    return parsed


async def translate_batch(batch: list[RawExercise]) -> dict[str, Translation]:
    payload = [{"id": e.id, "name": e.name, "instructions": e.instructions} for e in batch]
    expected = {e.id for e in batch}
    llm = get_llm("fast")
    last_error: Exception | None = None
    for _attempt in range(3):
        try:
            data = await llm.complete_json(
                [
                    {"role": "system", "content": TRANSLATE_SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ]
            )
            result: dict[str, Translation] = {}
            for item in data.get("items", []):
                try:
                    t = Translation.model_validate(item)
                except ValidationError:
                    continue
                if t.id in expected:
                    result[t.id] = t
            if result:
                return result
            last_error = ValueError("翻译结果为空")
        except Exception as exc:  # 网络错误 / JSON 解析失败
            last_error = exc
        await asyncio.sleep(2)
    logger.warning("批次翻译失败（%s...）：%s", batch[0].id, last_error)
    return {}


async def translate_missing(
    exercises: list[RawExercise], cache_path: Path, batch_size: int, concurrency: int
) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    missing = [e for e in exercises if e.id not in cache]
    if not missing:
        return cache
    logger.info("需要翻译 %d 个动作（已缓存 %d）", len(missing), len(cache))

    sem = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()
    batches = [missing[i : i + batch_size] for i in range(0, len(missing), batch_size)]
    done = 0

    async def worker(batch: list[RawExercise]) -> None:
        nonlocal done
        async with sem:
            result = await translate_batch(batch)
        async with write_lock:
            for doc_id, t in result.items():
                cache[doc_id] = t.model_dump(exclude={"id"})
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            done += 1
            logger.info("翻译进度 %d/%d 批", done, len(batches))

    await asyncio.gather(*(worker(b) for b in batches))
    return cache


def build_docs(exercises: list[RawExercise], translations: dict[str, dict]) -> list[ExerciseDoc]:
    docs = []
    for e in exercises:
        t = translations.get(e.id, {})
        docs.append(
            ExerciseDoc(
                id=e.id,
                name=e.name,
                level=e.level,
                category=e.category,
                primary_muscles=e.primary_muscles,
                secondary_muscles=e.secondary_muscles,
                instructions=e.instructions,
                force=e.force,
                mechanic=e.mechanic,
                equipment=e.equipment,
                images=e.images,
                name_zh=t.get("name_zh"),
                aliases_zh=t.get("aliases_zh", []),
                instructions_zh=t.get("instructions_zh", []),
            )
        )
    return docs


async def upsert_exercises(docs: list[ExerciseDoc]) -> None:
    now = utcnow()
    async with SessionLocal() as db:
        for d in docs:
            row = await db.get(Exercise, d.id)
            if row is None:
                row = Exercise(id=d.id)
                db.add(row)
            row.name = d.name
            row.name_zh = d.name_zh
            row.aliases_zh = d.aliases_zh
            row.force = d.force
            row.level = d.level
            row.mechanic = d.mechanic
            row.equipment = d.equipment
            row.category = d.category
            row.primary_muscles = d.primary_muscles
            row.secondary_muscles = d.secondary_muscles
            row.instructions = d.instructions
            row.instructions_zh = d.instructions_zh
            row.images = d.images
            row.updated_at = now
        await db.commit()
    logger.info("已写入 %d 个动作到数据库", len(docs))


async def build_index(docs: list[ExerciseDoc]) -> None:
    embedder = create_embedder(settings.embedding)
    texts = [d.search_text() for d in docs]
    logger.info("生成向量：%d 条，模型 %s", len(texts), embedder.model_name)
    vectors = await embedder.embed_documents(texts)
    content_hash = hashlib.sha256("\n\n".join(texts).encode("utf-8")).hexdigest()[:16]
    store = LocalVectorStore(
        [d.id for d in docs],
        vectors,
        meta={
            "embedding_model": embedder.model_name,
            "content_hash": content_hash,
            "built_at": utcnow().isoformat(),
        },
    )
    index_dir = resolve_path(settings.data.index_dir)
    store.save(index_dir)
    logger.info("向量索引已保存到 %s（维度 %d）", index_dir, store.dim)


async def main(args: argparse.Namespace) -> None:
    await init_db()
    exercises = parse_raw(await load_raw(args.source, args.refresh))
    if args.limit:
        exercises = exercises[: args.limit]
    logger.info("有效动作 %d 个", len(exercises))

    cache_path = resolve_path(settings.data.translation_cache_path)
    if args.skip_translate:
        translations = (
            json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        )
    else:
        translations = await translate_missing(
            exercises, cache_path, args.batch_size, args.concurrency
        )
    translated = sum(1 for e in exercises if e.id in translations)
    logger.info("中文覆盖 %d/%d", translated, len(exercises))

    docs = build_docs(exercises, translations)
    await upsert_exercises(docs)

    if not args.skip_embed:
        # 向量索引必须覆盖数据库中的全部动作，所以从数据库读取完整集合
        from sqlalchemy import select

        async with SessionLocal() as db:
            rows = (await db.execute(select(Exercise))).scalars().all()
        await build_index([ExerciseDoc.from_model(r) for r in rows])
    logger.info("完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入 free-exercise-db 动作知识库")
    parser.add_argument("--source", help="本地 JSON 路径或 URL，默认使用 config.yml 中的地址")
    parser.add_argument("--refresh", action="store_true", help="重新下载原始数据")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（试跑用）")
    parser.add_argument("--skip-translate", action="store_true", help="跳过中文翻译")
    parser.add_argument("--skip-embed", action="store_true", help="跳过向量索引")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=4)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main(parser.parse_args()))
