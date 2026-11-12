import numpy as np
import pytest

from app.config import RetrievalSettings
from app.rag.bm25 import tokenize
from app.rag.retriever import ExerciseRetriever, rrf_fuse
from app.rag.schema import ExerciseDoc, ExerciseFilter
from app.rag.vector_store import LocalVectorStore


def make_docs():
    return [
        ExerciseDoc(
            id="Barbell_Bench_Press", name="Barbell Bench Press", level="beginner", category="strength",
            primary_muscles=["chest"], secondary_muscles=["triceps", "shoulders"],
            instructions=["Lie on a flat bench."], mechanic="compound", equipment="barbell", force="push",
            name_zh="杠铃卧推", aliases_zh=["平板卧推"], instructions_zh=["仰卧在平板凳上。"],
        ),
        ExerciseDoc(
            id="Dumbbell_Flyes", name="Dumbbell Flyes", level="beginner", category="strength",
            primary_muscles=["chest"], secondary_muscles=[], instructions=["Lie down."],
            mechanic="isolation", equipment="dumbbell", force="push", name_zh="哑铃飞鸟",
        ),
        ExerciseDoc(
            id="Pullups", name="Pullups", level="intermediate", category="strength",
            primary_muscles=["lats"], secondary_muscles=["biceps"], instructions=["Grab the bar."],
            mechanic="compound", equipment="body only", force="pull", name_zh="引体向上",
        ),
        ExerciseDoc(
            id="Quad_Stretch", name="Quad Stretch", level="beginner", category="stretching",
            primary_muscles=["quadriceps"], secondary_muscles=[], instructions=["Stand."],
            equipment=None, name_zh="股四头肌拉伸",
        ),
    ]


def test_tokenize_mixed():
    toks = tokenize("推荐几个哑铃练胸的动作 dumbbell")
    assert "哑铃" in toks and "dumbbell" in toks
    assert "推荐" not in toks and "动作" not in toks


def test_filter_normalizes_chinese_and_matches():
    f = ExerciseFilter(primary_muscles=["胸部"], equipment=["哑铃", "徒手"])
    assert f.primary_muscles == ["chest"]
    assert f.equipment == ["dumbbell", "body only"]
    docs = make_docs()
    assert [d.id for d in docs if f.matches(d)] == ["Dumbbell_Flyes"]
    # 未标注器械的动作在指定器械条件时不返回
    assert not ExerciseFilter(equipment=["body only"]).matches(docs[3])


def test_filter_rejects_unknown_value():
    with pytest.raises(Exception):
        ExerciseFilter(primary_muscles=["翅膀"])


def test_rrf_fuse():
    fused = rrf_fuse([[("a", 0.9), ("b", 0.8)], [("b", 10.0), ("c", 5.0)]], k=60)
    assert fused[0][0] == "b"
    assert {x[0] for x in fused} == {"a", "b", "c"}


def test_vector_store_roundtrip_and_mask(tmp_path):
    store = LocalVectorStore(["a", "b", "c"], np.eye(3), meta={"embedding_model": "m"})
    assert store.search(np.array([0, 1, 0]), 1)[0][0] == "b"
    assert store.search(np.array([0, 1, 0]), 2, allowed={"a", "c"})[0][0] in {"a", "c"}
    store.save(tmp_path)
    loaded = LocalVectorStore.load(tmp_path)
    assert loaded is not None and loaded.ids == ["a", "b", "c"] and loaded.meta["embedding_model"] == "m"


class FakeEmbedder:
    model_name = "fake"

    def __init__(self, docs):
        self.vocab = ["胸", "背", "腿"]

    def _vec(self, text):
        return np.array([text.count(w) + 1e-3 for w in self.vocab], dtype=np.float32)

    async def embed_documents(self, texts):
        return np.stack([self._vec(t) for t in texts])

    async def embed_query(self, text):
        return self._vec(text)


async def test_hybrid_search_with_filters():
    docs = make_docs()
    embedder = FakeEmbedder(docs)
    vectors = await embedder.embed_documents([d.search_text() for d in docs])
    store = LocalVectorStore([d.id for d in docs], vectors, {"embedding_model": "fake"})
    retriever = ExerciseRetriever(docs, RetrievalSettings(), vector_store=store, embedder=embedder)

    res = await retriever.search("卧推 练胸", ExerciseFilter(), top_k=2)
    assert res.mode == "hybrid"
    assert res.hits[0].doc.id == "Barbell_Bench_Press"

    res = await retriever.search("练胸", ExerciseFilter(equipment=["dumbbell"]), top_k=5)
    assert [h.doc.id for h in res.hits] == ["Dumbbell_Flyes"]

    res = await retriever.search("", ExerciseFilter(categories=["strength"]), top_k=5)
    assert res.mode == "filter_only" and res.hits[0].doc.mechanic == "compound"

    res = await retriever.search("胸", ExerciseFilter(primary_muscles=["neck"]))
    assert res.hits == [] and res.warnings


async def test_dense_failure_degrades_to_bm25():
    docs = make_docs()

    class Broken(FakeEmbedder):
        async def embed_query(self, text):
            raise RuntimeError("down")

    embedder = Broken(docs)
    vectors = await embedder.embed_documents([d.search_text() for d in docs])
    store = LocalVectorStore([d.id for d in docs], vectors, {})
    retriever = ExerciseRetriever(docs, RetrievalSettings(), vector_store=store, embedder=embedder)
    res = await retriever.search("引体向上")
    assert res.mode == "sparse_only" and res.warnings
    assert res.hits[0].doc.id == "Pullups"
