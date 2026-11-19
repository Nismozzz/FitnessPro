import os

import httpx
import pytest

from app.config import settings
from app.llm.provider import get_llm
from app.main import app
from app.rag.retriever import refresh_provider_clients
from app.runtime_config import hint
from tests.test_integration import make_user
from tests.test_training import client_for


@pytest.fixture
async def provider_sandbox(tmp_path, monkeypatch):
    import app.runtime_config as runtime_config

    monkeypatch.setattr(runtime_config, "RUNTIME_PATH", tmp_path / "runtime_settings.json")
    before = {
        "normal": (settings.llm.normal.model, settings.llm.normal.base_url, os.environ.get("LLM_API_KEY")),
        "fast": (settings.llm.fast.model, settings.llm.fast.base_url, os.environ.get("FAST_LLM_API_KEY")),
        "embedding": (settings.embedding.provider, settings.embedding.model, settings.embedding.base_url, os.environ.get("EMBEDDING_API_KEY")),
        "reranker": (settings.reranker.enabled, settings.reranker.model, settings.reranker.base_url, os.environ.get("RERANKER_API_KEY")),
    }
    yield
    settings.llm.normal.model, settings.llm.normal.base_url = before["normal"][:2]
    settings.llm.fast.model, settings.llm.fast.base_url = before["fast"][:2]
    settings.embedding.provider, settings.embedding.model, settings.embedding.base_url = before["embedding"][:3]
    settings.reranker.enabled, settings.reranker.model, settings.reranker.base_url = before["reranker"][:3]
    for name, value in (
        ("LLM_API_KEY", before["normal"][2]),
        ("FAST_LLM_API_KEY", before["fast"][2]),
        ("EMBEDDING_API_KEY", before["embedding"][3]),
        ("RERANKER_API_KEY", before["reranker"][3]),
    ):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_llm.cache_clear()
    await refresh_provider_clients()


def form(view, **keys):
    def endpoint(name):
        item = {"model": view[name]["model"], "base_url": view[name]["base_url"], "api_key": keys.get(name), "clear_api_key": keys.get(f"clear_{name}", False)}
        if name == "embedding":
            item["provider"] = view["embedding"]["provider"]
        if name == "reranker":
            item["enabled"] = view["reranker"]["enabled"]
        return item
    return {"normal": endpoint("normal"), "fast": endpoint("fast"), "embedding": endpoint("embedding"), "reranker": endpoint("reranker"), "share_normal_key": keys.get("share", False)}


async def test_provider_settings_keep_secrets_out_of_the_response(db, provider_sandbox):
    secret = "sk-test-secret-value-1234"
    uid = await make_user()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
        assert (await anon.get("/api/v1/settings/providers")).status_code == 401
    async with await client_for(uid) as client:
        current = (await client.get("/api/v1/settings/providers")).json()
        assert secret not in str(current)
        saved = await client.put("/api/v1/settings/providers", json=form(current, normal=secret, share=True))
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert secret not in saved.text
        assert body["normal"]["api_key_set"] is True
        assert body["normal"]["api_key_hint"] == hint(secret) == "••••1234"
        assert body["fast"]["api_key_hint"] == "••••1234"
        assert "api_key" not in body["normal"]
        kept = await client.put("/api/v1/settings/providers", json=form(body, share=True))
        assert secret not in kept.text
        assert os.environ["LLM_API_KEY"] == os.environ["FAST_LLM_API_KEY"] == secret
        get_llm.cache_clear()
        assert get_llm("normal")._client.api_key == secret
        import app.runtime_config as runtime_config
        assert secret in runtime_config.RUNTIME_PATH.read_text(encoding="utf-8")
        cleared = await client.put("/api/v1/settings/providers", json=form(body, clear_normal=True, share=True))
        assert cleared.json()["normal"]["api_key_set"] is False
        assert os.environ["LLM_API_KEY"] == ""
        invalid = await client.put("/api/v1/settings/providers", json=form({**body, "normal": {**body["normal"], "base_url": "ftp://example.com"}}))
        assert invalid.status_code == 422
