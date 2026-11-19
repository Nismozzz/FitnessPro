from app.config import settings
from app.rag.vector_store import LocalVectorStore


def load_store(directory):
    if settings.vector_store.type == 'qdrant':
        from app.rag.qdrant_store import QdrantVectorStore
        return QdrantVectorStore.load(directory)
    return LocalVectorStore.load(directory)


def publish_store(directory, corpus):
    if settings.vector_store.type == 'qdrant':
        from app.rag.qdrant_store import publish
        return publish(directory, corpus)
