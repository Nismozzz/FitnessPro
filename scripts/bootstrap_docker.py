"""One-shot initialization; restart does not repeat translation or embedding calls."""
import asyncio
import logging
import shutil
from pathlib import Path

from sqlalchemy import func, select

from app.config import resolve_path, settings
from app.database.models import Exercise
from app.database.session import SessionLocal, engine
from app.utils.background import drain
from scripts.ingest_exercises import main as ingest_exercises
from scripts.ingest_knowledge import ingest as ingest_knowledge
from scripts.ingest_meals import ingest as ingest_meals


async def main():
    import argparse

    try:
        if settings.vector_store.type == 'qdrant':
            import httpx
            logging.info('等待 Qdrant 就绪')
            async with httpx.AsyncClient(timeout=5) as client:
                for attempt in range(30):
                    try:
                        response = await client.get(settings.vector_store.url.rstrip('/') + '/readyz')
                        response.raise_for_status()
                        break
                    except httpx.HTTPError:
                        if attempt == 29:
                            raise
                        await asyncio.sleep(2)
        async with SessionLocal() as db:
            count = await db.scalar(select(func.count()).select_from(Exercise))
        if not count:
            await ingest_exercises(argparse.Namespace(
                source=None, refresh=False, limit=0, skip_translate=False,
                skip_embed=False, batch_size=8, concurrency=4,
            ))
        elif not (resolve_path(settings.data.index_dir) / "vectors.npy").exists():
            # A previous job may have saved rows before its embedding request failed.
            from scripts.ingest_exercises import build_index
            from app.rag.schema import ExerciseDoc

            async with SessionLocal() as db:
                rows = (await db.scalars(select(Exercise))).all()
            await build_index([ExerciseDoc.from_model(row) for row in rows])

        from app.rag.storage import publish_store
        logging.info('同步动作向量到配置的向量库（复用已有向量）')
        await asyncio.to_thread(publish_store, resolve_path(settings.data.index_dir), 'exercises')

        raw = resolve_path(settings.knowledge.raw_dir)
        if not raw.exists():
            shutil.copytree(Path('/opt/fitnesspro/knowledge/raw'), raw)
        sources = raw.parent / 'sources.json'
        if not sources.exists():
            shutil.copyfile(Path('/opt/fitnesspro/knowledge/sources.json'), sources)
        index = resolve_path(settings.knowledge.index_dir)
        if not (index / 'manifest.json').exists():
            await ingest_knowledge(raw, index, chunk_size=settings.knowledge.chunk_size)
        logging.info('同步文献向量到配置的向量库（复用已有向量）')
        if (index / 'vectors.npy').exists():
            await asyncio.to_thread(publish_store, index, 'articles')
        elif settings.vector_store.type == 'qdrant':
            await ingest_knowledge(raw, index, chunk_size=settings.knowledge.chunk_size)
        meal_index = resolve_path(settings.nutrition.index_dir)
        if not (meal_index / 'manifest.json').exists():
            logging.info('建立营养菜谱向量索引')
            await ingest_meals(resolve_path(settings.nutrition.source_file), meal_index)
        elif (meal_index / 'vectors.npy').exists():
            from app.rag.storage import publish_store
            await asyncio.to_thread(publish_store, meal_index, 'meals')
        logging.info('知识库初始化完成')
    finally:
        await drain()
        await engine.dispose()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    asyncio.run(main())
