"""Build the nutrition recipe index: python -m scripts.ingest_meals [--input meals.json]."""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from app.config import resolve_path, settings
from app.nutrition.ingest import catalog_hash, read_meals
from app.rag.embeddings import create_embedder
from app.rag.vector_store import LocalVectorStore


async def ingest(input_path: Path, index_dir: Path, *, skip_embed: bool = False) -> dict:
    meals = read_meals(input_path)
    index_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=index_dir.parent, prefix=".meals-") as tmp:
        staging = Path(tmp)
        (staging / "meals.json").write_text(json.dumps([m.model_dump() for m in meals], ensure_ascii=False, indent=2), encoding="utf-8")
        digest = catalog_hash(meals)
        if not skip_embed:
            embedder = create_embedder(settings.embedding)
            vectors = await embedder.embed_documents([m.search_text() for m in meals])
            LocalVectorStore([m.id for m in meals], vectors, {"embedding_model": settings.embedding.model, "catalog_hash": digest}).save(staging)
            from app.rag.storage import publish_store
            await asyncio.to_thread(publish_store, staging, "meals")
        manifest = {"meals": len(meals), "catalog_hash": digest, "dense": not skip_embed}
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        backup = index_dir.with_name(index_dir.name + ".previous")
        if backup.exists():
            raise ValueError(f"存在旧备份 {backup}，请检查后重试: {backup}")
        if index_dir.exists():
            index_dir.rename(backup)
        try:
            staging.rename(index_dir)
        except BaseException:
            if backup.exists(): backup.rename(index_dir)
            raise
        if backup.exists(): shutil.rmtree(backup)
    return manifest


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=resolve_path(settings.nutrition.source_file))
    parser.add_argument("--index-dir", type=Path, default=resolve_path(settings.nutrition.index_dir))
    parser.add_argument("--skip-embed", action="store_true")
    args = parser.parse_args()
    print(json.dumps(await ingest(args.input, args.index_dir, skip_embed=args.skip_embed), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
