"""Discover and probe OpenAI-compatible model endpoints."""

from __future__ import annotations

import os

import httpx

from api.desktop_settings import _get_password


def normalize_openai_base_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        raise ValueError("API 地址不能为空")
    if cleaned.endswith("/v1"):
        return cleaned
    return f"{cleaned}/v1"


def resolve_api_key(provider: str, api_key: str | None) -> str:
    if api_key and api_key.strip():
        return api_key.strip()
    stored = _get_password(provider) or os.environ.get("OPENAI_API_KEY")
    if not stored:
        raise ValueError("缺少 API Key")
    return stored


async def discover_openai_models(base_url: str, api_key: str) -> list[dict[str, str]]:
    root = normalize_openai_base_url(base_url)
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{root}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        payload = response.json()
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in payload.get("data") or []:
        model_id = item.get("id") if isinstance(item, dict) else None
        if model_id and model_id not in seen:
            seen.add(model_id)
            models.append({"id": model_id, "name": model_id})
    return models


async def probe_openai_chat(base_url: str, api_key: str, model: str) -> dict[str, str]:
    if not model.strip():
        raise ValueError("请先选择模型")
    root = normalize_openai_base_url(base_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{root}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model.strip(),
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
            },
        )
        response.raise_for_status()
    return {"ok": "true", "model": model.strip()}


async def embeddings_available(base_url: str, api_key: str) -> bool:
    root = normalize_openai_base_url(base_url)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{root}/embeddings",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": "text-embedding-3-small", "input": "ping"},
            )
    except httpx.HTTPError:
        return False
    return response.status_code < 500
