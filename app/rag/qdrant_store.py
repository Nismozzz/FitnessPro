"""Qdrant REST adapter. Immutable generations published with an atomic alias swap."""
import hashlib
import json
import uuid
from pathlib import Path

import httpx
import numpy as np

from app.config import settings
from app.rag.vector_store import LocalVectorStore, l2_normalize


def request(method, path, **kwargs):
    cfg = settings.vector_store
    with httpx.Client(base_url=cfg.url.rstrip('/'), timeout=cfg.timeout) as client:
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()['result']


class QdrantVectorStore:
    def __init__(self, manifest):
        self.collection = manifest['collection']
        self.ids = manifest['ids']
        self.meta = manifest['meta']
        self.dim = manifest['dim']

    def __len__(self):
        return len(self.ids)

    def search(self, query_vector, top_k, allowed=None):
        if top_k <= 0 or allowed == set() or not self.ids:
            return []
        query = l2_normalize(np.asarray(query_vector, dtype=np.float32).reshape(-1))
        if len(query) != self.dim:
            raise ValueError('查询向量维度与 Qdrant 索引不一致')
        body = {'query': query.tolist(), 'limit': top_k, 'with_payload': True}
        if allowed is not None:
            body['filter'] = {'must': [{'key': 'doc_id', 'match': {'any': sorted(allowed)}}]}
        points = request('POST', f'/collections/{self.collection}/points/query', json=body)['points']
        return [(p['payload']['doc_id'], p['score']) for p in points]

    @classmethod
    def load(cls, directory: Path):
        path = directory / 'qdrant.json'
        if not path.exists():
            return None
        manifest = json.loads(path.read_text())
        info = request('GET', f"/collections/{manifest['collection']}")
        if info['points_count'] != len(manifest['ids']):
            raise ValueError('Qdrant 点数与索引清单不一致，请重新迁移索引')
        if info['config']['params']['vectors']['size'] != manifest['dim']:
            raise ValueError('Qdrant 向量维度与索引清单不一致')
        return cls(manifest)


def publish(directory: Path, corpus: str):
    """Import saved vectors without calling embedding. Never delete previous generations."""
    store = LocalVectorStore.load(directory)
    if store is None:
        raise ValueError(f'{directory}: 缺少可迁移的本地向量')
    digest = hashlib.sha256(store.vectors.tobytes() + json.dumps(
        [store.ids, store.meta], sort_keys=True).encode()).hexdigest()[:24]
    alias = f'{settings.vector_store.collection_prefix}_{corpus}'
    collection = f'{alias}_{digest}'
    collections = request('GET', '/collections')['collections']
    if collection not in {c['name'] for c in collections}:
        request('PUT', f'/collections/{collection}', json={
            'vectors': {'size': store.dim, 'distance': 'Cosine'}})
    # Re-upsert a full generation so an interrupted import is safely resumable.
    for start in range(0, len(store.ids), 128):
        points = [{'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{corpus}/{doc_id}')),
                   'vector': store.vectors[i].tolist(), 'payload': {'doc_id': doc_id}}
                  for i, doc_id in enumerate(store.ids[start:start + 128], start)]
        request('PUT', f'/collections/{collection}/points?wait=true', json={'points': points})
    info = request('GET', f'/collections/{collection}')
    if info['points_count'] != len(store.ids):
        raise ValueError('Qdrant 导入数量不一致，保留旧版本')
    aliases = request('GET', '/aliases')['aliases']
    actions = []
    if alias in {a['alias_name'] for a in aliases}:
        actions.append({'delete_alias': {'alias_name': alias}})
    actions.append({'create_alias': {'collection_name': collection, 'alias_name': alias}})
    request('POST', '/collections/aliases', json={'actions': actions})
    # Pin readers to the immutable generation matching their document snapshot.
    manifest = {'collection': collection, 'alias': alias, 'ids': store.ids,
                'meta': store.meta, 'dim': store.dim}
    temporary = directory / 'qdrant.json.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    temporary.replace(directory / 'qdrant.json')
    return QdrantVectorStore(manifest)
