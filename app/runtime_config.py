"""界面里保存的模型连接。

密钥写入 data/runtime_settings.json（该目录不入库），并覆盖同名环境变量。
接口只返回是否已配置和末四位，不把完整密钥送回浏览器。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from app.config import resolve_path, settings

logger = logging.getLogger(__name__)

RUNTIME_PATH = resolve_path("data/runtime_settings.json")


def hint(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 4:
        return "••••"
    return f"••••{secret[-4:]}"


def public_providers() -> dict[str, Any]:
    normal = settings.llm.normal
    fast = settings.llm.fast
    embedding = settings.embedding
    reranker = settings.reranker
    return {
        "ready": bool(normal.api_key),
        "normal": _public_endpoint(normal.model, normal.base_url, normal.api_key),
        "fast": _public_endpoint(fast.model, fast.base_url, fast.api_key),
        "embedding": {
            **_public_endpoint(embedding.model, embedding.base_url, embedding.api_key),
            "provider": embedding.provider,
        },
        "reranker": {
            **_public_endpoint(reranker.model, reranker.base_url, reranker.api_key),
            "enabled": reranker.enabled,
        },
    }


def apply_stored_settings() -> None:
    data = _read()
    if data:
        _overlay(data)


def save_providers(payload: dict[str, Any]) -> dict[str, Any]:
    normal_key = _next_secret(settings.llm.normal.api_key, payload["normal"])
    fast_key = normal_key if payload.get("share_normal_key") else _next_secret(settings.llm.fast.api_key, payload["fast"])
    data = {
        "normal": {**_endpoint_data(payload["normal"], normal_key)},
        "fast": {**_endpoint_data(payload["fast"], fast_key)},
        "embedding": {
            **_endpoint_data(payload["embedding"], _next_secret(settings.embedding.api_key, payload["embedding"])),
            "provider": payload["embedding"]["provider"],
        },
        "reranker": {
            **_endpoint_data(payload["reranker"], _next_secret(settings.reranker.api_key, payload["reranker"])),
            "enabled": bool(payload["reranker"]["enabled"]),
        },
    }
    _write(data)
    _overlay(data)
    return public_providers()


def _public_endpoint(model: str, base_url: str, secret: str) -> dict[str, Any]:
    return {"model": model, "base_url": base_url, "api_key_set": bool(secret), "api_key_hint": hint(secret)}


def _endpoint_data(submitted: dict[str, Any], secret: str) -> dict[str, str]:
    return {"model": submitted["model"].strip(), "base_url": submitted["base_url"].strip(), "api_key": secret}


def _next_secret(current: str, submitted: dict[str, Any]) -> str:
    if submitted.get("clear_api_key"):
        return ""
    typed = submitted.get("api_key")
    if typed is None or str(typed).strip() == "":
        return current
    return str(typed).strip()


def _read() -> dict[str, Any]:
    if not RUNTIME_PATH.is_file():
        return {}
    try:
        data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("运行时连接设置无法读取，继续使用配置文件中的值")
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict[str, Any]) -> None:
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RUNTIME_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(RUNTIME_PATH)


def _overlay(data: dict[str, Any]) -> None:
    _apply_llm("normal", data.get("normal") or {})
    _apply_llm("fast", data.get("fast") or {})
    embedding = data.get("embedding") or {}
    if embedding.get("provider") in {"openai", "local"}:
        settings.embedding.provider = embedding["provider"]
    if embedding.get("model"):
        settings.embedding.model = embedding["model"]
    if embedding.get("base_url"):
        settings.embedding.base_url = embedding["base_url"]
    if "api_key" in embedding:
        os.environ[settings.embedding.api_key_env] = str(embedding.get("api_key") or "")
    reranker = data.get("reranker") or {}
    if "enabled" in reranker:
        settings.reranker.enabled = bool(reranker["enabled"])
    if reranker.get("model"):
        settings.reranker.model = reranker["model"]
    if reranker.get("base_url"):
        settings.reranker.base_url = reranker["base_url"]
    if "api_key" in reranker:
        os.environ[settings.reranker.api_key_env] = str(reranker.get("api_key") or "")


def _apply_llm(tier: str, block: dict[str, Any]) -> None:
    profile = getattr(settings.llm, tier)
    if block.get("model"):
        profile.model = block["model"]
    if block.get("base_url"):
        profile.base_url = block["base_url"]
    if "api_key" in block:
        os.environ[profile.api_key_env] = str(block.get("api_key") or "")
