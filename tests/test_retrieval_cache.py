import json
import time

import httpx
import numpy as np
from redis.exceptions import ConnectionError

from app.config import RetrievalSettings, settings
from app.rag import cache as cache_module
from app.rag.retriever import ExerciseRetriever
from app.rag.schema import ExerciseFilter
from app.rag.vector_store import LocalVectorStore
from tests.test_rag import FakeEmbedder, make_docs


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, *, ex):
        self.values[key] = value
        self.ttls[key] = ex


class SemanticCollection:
    def __init__(self):
        self.created = False
        self.points = {}
        self.lookups = 0

    def request(self, request):
        path = request.url.path
        body = json.loads(request.content)
        if path.endswith('/points/query'):
            self.lookups += 1
            if not self.created:
                return httpx.Response(404)
            scope, expiry = body['filter']['must']
            points = []
            for point in self.points.values():
                payload = point['payload']
                if payload['scope'] != scope['match']['value'] or payload['expires_at'] <= expiry['range']['gt']:
                    continue
                a, b = np.array(body['query']), np.array(point['vector'])
                score = float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))
                if score >= body['score_threshold']:
                    points.append({'payload': payload, 'score': score})
            points.sort(key=lambda point: point['score'], reverse=True)
            return httpx.Response(200, json={'result': {'points': points[:body['limit']]}})
        if path.endswith('/points/delete'):
            return httpx.Response(200, json={'result': True})
        if path.endswith('/points'):
            assert self.created
            self.points.update({point['id']: point for point in body['points']})
        else:
            assert request.method == 'PUT' and body['vectors'] == {'size': 3, 'distance': 'Cosine'}
            self.created = True
        return httpx.Response(200, json={'result': True})


class CountingEmbedder(FakeEmbedder):
    def __init__(self, docs):
        super().__init__(docs)
        self.queries = []

    async def embed_query(self, query):
        self.queries.append(query)
        return await super().embed_query(query)


async def make_retriever(docs):
    embedder = CountingEmbedder(docs)
    vectors = await embedder.embed_documents([doc.search_text() for doc in docs])
    store = LocalVectorStore([doc.id for doc in docs], vectors)
    return ExerciseRetriever(docs, RetrievalSettings(), store, embedder), embedder


async def test_exact_and_semantic_cache_reuse_and_scope_isolation(monkeypatch):
    redis, qdrant = MemoryRedis(), SemanticCollection()
    docs = make_docs()
    source, source_embedder = await make_retriever(docs)
    other_worker, other_embedder = await make_retriever(docs)
    async with httpx.AsyncClient(base_url='http://qdrant', transport=httpx.MockTransport(qdrant.request)) as client:
        monkeypatch.setattr(cache_module, 'cache', cache_module.RetrievalCache(redis, client))
        first = await source.search('练胸', top_k=2)
        assert first.cache_hit is None
        assert source_embedder.queries == ['练胸']
        assert list(redis.ttls.values()) == [settings.retrieval_cache.l1_ttl_seconds]

        monkeypatch.setattr(cache_module, 'cache', cache_module.RetrievalCache(redis, client))
        repeat = await other_worker.search('练胸', top_k=2)
        assert repeat.cache_hit == 'l1' and repeat.hits == first.hits
        assert not other_embedder.queries
        assert qdrant.lookups == 1

        paraphrase = await other_worker.search('胸部训练', top_k=2)
        assert paraphrase.cache_hit == 'l2' and paraphrase.hits == first.hits
        assert other_embedder.queries == ['胸部训练']
        assert (await other_worker.search('胸部训练', top_k=2)).cache_hit == 'l1'
        assert len(qdrant.points) == 1
        assert min(redis.ttls.values()) < settings.retrieval_cache.l2_ttl_seconds

        filtered = await other_worker.search('胸部训练', ExerciseFilter(equipment=['dumbbell']), top_k=2)
        assert filtered.cache_hit is None
        assert [hit.doc.id for hit in filtered.hits] == ['Dumbbell_Flyes']
        assert (await other_worker.search('胸部训练', top_k=1)).cache_hit is None
        assert (await other_worker.search('练背', top_k=2)).cache_hit is None

        changed = make_docs()
        changed[0].instructions.append('New catalog version')
        newer, _ = await make_retriever(changed)
        assert (await newer.search('练胸', top_k=2)).cache_hit is None

        redis.values.clear()
        for point in qdrant.points.values():
            point['payload']['expires_at'] = int(time.time()) - 1
        assert (await source.search('练胸', top_k=2)).cache_hit is None


async def test_cache_outage_and_degraded_results(monkeypatch):
    class OfflineRedis(MemoryRedis):
        async def get(self, key):
            raise ConnectionError('offline')

        async def set(self, key, value, *, ex):
            raise ConnectionError('offline')

    retriever, embedder = await make_retriever(make_docs())
    offline = httpx.MockTransport(lambda request: httpx.Response(503))
    async with httpx.AsyncClient(base_url='http://qdrant', transport=offline) as client:
        monkeypatch.setattr(cache_module, 'cache', cache_module.RetrievalCache(OfflineRedis(), client))
        result = await retriever.search('练胸')
    assert result.hits and not result.warnings and result.cache_hit is None
    assert embedder.queries == ['练胸']

    def broken_search(*args):
        raise RuntimeError('document index offline')

    redis = MemoryRedis()
    monkeypatch.setattr(cache_module, 'cache', cache_module.RetrievalCache(redis))
    monkeypatch.setattr(retriever.vector_store, 'search', broken_search)
    degraded = await retriever.search('练胸')
    assert degraded.hits and degraded.warnings and degraded.cache_hit is None
    assert not redis.values
