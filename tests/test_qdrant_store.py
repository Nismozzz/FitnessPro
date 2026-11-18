import json

import httpx
import numpy as np
import pytest

from app.config import settings
from app.rag.qdrant_store import QdrantVectorStore, publish
from app.rag.vector_store import LocalVectorStore


def test_qdrant_import_resume_filter_and_snapshot(tmp_path, monkeypatch):
    collections, aliases = {}, {}
    client_class = httpx.Client
    fail_upload = False

    def handle(req):
        nonlocal fail_upload
        path, method = req.url.path, req.method
        body = json.loads(req.content) if req.content else {}
        result = True
        if path == '/collections':
            result = {'collections': [{'name': name} for name in collections]}
        elif path == '/aliases':
            result = {'aliases': [{'alias_name': a, 'collection_name': c} for a, c in aliases.items()]}
        elif path == '/collections/aliases':
            for action in body['actions']:
                if 'delete_alias' in action:
                    aliases.pop(action['delete_alias']['alias_name'])
                else:
                    a = action['create_alias']; aliases[a['alias_name']] = a['collection_name']
        else:
            name = path.split('/')[2]
            if path.endswith('/points/query'):
                data = list(collections[name]['points'].values())
                if 'filter' in body:
                    allowed = body['filter']['must'][0]['match']['any']
                    data = [p for p in data if p['payload']['doc_id'] in allowed]
                result = {'points': [dict(p, score=float(np.dot(p['vector'], body['query']))) for p in data][:body['limit']]}
            elif path.endswith('/points'):
                if fail_upload:
                    return httpx.Response(503, json={'status': 'error'})
                collections[name]['points'].update({p['id']: p for p in body['points']})
            elif method == 'PUT':
                collections[name] = {'points': {}, 'config': {'params': body}}
            else:
                c = collections[name]
                result = {'points_count': len(c['points']), 'config': c['config']}
        return httpx.Response(200, json={'result': result})

    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(settings.vector_store, 'collection_prefix', 'test')
    LocalVectorStore(['非UUID动作', 'b'], np.eye(2), {'embedding_model': 'fake'}).save(tmp_path)
    store = publish(tmp_path, 'exercises')
    assert set(store.ids) == {'非UUID动作', 'b'}
    assert store.search([1, 0], 2, {'非UUID动作'}) == [('非UUID动作', 1.0)]
    assert store.search([1, 0], 2, set()) == []
    with pytest.raises(ValueError, match='维度'):
        store.search([1, 0, 0], 2)
    assert QdrantVectorStore.load(tmp_path).collection == store.collection
    publish(tmp_path, 'exercises')
    assert len(collections) == 1  # Retry is idempotent.
    old_manifest = (tmp_path / 'qdrant.json').read_text()
    LocalVectorStore(['new'], [[0, 1]], {'embedding_model': 'new'}).save(tmp_path)
    fail_upload = True
    with pytest.raises(httpx.HTTPStatusError):
        publish(tmp_path, 'exercises')
    assert (tmp_path / 'qdrant.json').read_text() == old_manifest
    assert aliases['test_exercises'] == store.collection
    fail_upload = False
    latest = publish(tmp_path, 'exercises')
    assert latest.collection != store.collection
    assert store.search([1, 0], 2, {'非UUID动作'}) == [('非UUID动作', 1.0)]
    assert QdrantVectorStore.load(tmp_path).ids == ['new']
