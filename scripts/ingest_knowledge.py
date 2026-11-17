"""python -m scripts.ingest_knowledge [--skip-embed]"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from app.config import resolve_path, settings
from app.knowledge.ingest import read_articles
from app.knowledge.retriever import catalog_hash
from app.rag.embeddings import create_embedder
from app.rag.vector_store import LocalVectorStore


async def ingest(raw_dir: Path, index_dir: Path, *, skip_embed=False, chunk_size=900):
    if index_dir.resolve() == resolve_path(settings.data.index_dir).resolve():
        raise ValueError('文献索引不能覆盖动作索引目录')
    if raw_dir.resolve() == index_dir.resolve() or raw_dir.resolve().is_relative_to(index_dir.resolve()):
        raise ValueError('索引目录不能覆盖原始资料')
    chunks = read_articles(raw_dir, chunk_size)
    # 先完整构建到暂存目录；Embedding 失败不会覆盖现有可用索引。
    index_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=index_dir.parent, prefix='.articles-') as temporary:
        staging = Path(temporary)
        (staging / 'chunks.json').write_text(json.dumps([c.model_dump() for c in chunks], ensure_ascii=False, indent=2), encoding='utf-8')
        manifest = {'articles': len({c.article_id for c in chunks}), 'chunks': len(chunks),
                    'chunk_size': chunk_size, 'catalog_hash': catalog_hash(chunks), 'dense': not skip_embed}
        if not skip_embed:
            embedder = create_embedder(settings.embedding)
            vectors = await embedder.embed_documents([c.search_text() for c in chunks])
            LocalVectorStore([c.id for c in chunks], vectors, {
                'embedding_model': settings.embedding.model, 'catalog_hash': catalog_hash(chunks),
            }).save(staging)
        (staging / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
        # 同一文件系统重命名，保留上一版备份直到切换成功。
        backup = index_dir.with_name(index_dir.name + '.previous')
        if backup.exists():
            raise ValueError(f'存在旧备份 {backup}，请检查上次入库状态后重试')
        if index_dir.exists():
            index_dir.rename(backup)
        try:
            staging.rename(index_dir)
        except BaseException:
            if backup.exists():
                backup.rename(index_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    return manifest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-embed', action='store_true')
    parser.add_argument('--raw-dir', type=Path, default=resolve_path(settings.knowledge.raw_dir))
    parser.add_argument('--index-dir', type=Path, default=resolve_path(settings.knowledge.index_dir))
    parser.add_argument('--chunk-size', type=int, default=settings.knowledge.chunk_size)
    args = parser.parse_args()
    print(json.dumps(await ingest(args.raw_dir, args.index_dir, skip_embed=args.skip_embed, chunk_size=args.chunk_size), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
